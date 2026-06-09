import logging
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import List, Optional

from ..config import MemoryConfig
from ..usage import RunUsage, UsageTracker, record_usage_from_response
from . import MemoryBackend, MemoryResult
from .model import content_hash, memory_item_from_raw, to_metadata

logger = logging.getLogger(__name__)

_CHROMA_BLOB_SEQ_ID_FIX_MARKER = ".seq_id_blob_fix_v2"


def _get_openrouter_api_key() -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("mem0 requires OPENROUTER_API_KEY")
    return api_key


def _get_openrouter_base_url() -> str:
    return os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")


def _extract_metadata(raw: dict) -> dict:
    """Pull our custom fields out of a mem0 result's metadata."""
    meta = raw.get("metadata", {}) or {}
    extracted = dict(meta)
    for key in ("created_at", "user_id", "agent_id", "run_id"):
        if raw.get(key) not in {"", None} and extracted.get(key) in {"", None}:
            extracted[key] = raw[key]
    text = str(raw.get("memory") or raw.get("text") or "")
    item = memory_item_from_raw(text, extracted)
    normalized = to_metadata(item)
    extracted.update(
        {
            k: v
            for k, v in normalized.items()
            if k not in extracted or extracted[k] in {"", None}
        }
    )
    return extracted


def _raw_results(results) -> list[dict]:
    return results.get("results", []) if isinstance(results, dict) else results


def _mem0_get_all_compat(mem, **kwargs):
    """Call mem0.get_all across versions that use either top_k or limit."""
    try:
        return mem.get_all(**kwargs)
    except TypeError as e:
        if "top_k" not in kwargs or "top_k" not in str(e):
            raise
        fallback = dict(kwargs)
        fallback["limit"] = fallback.pop("top_k")
        filters = dict(fallback.get("filters") or {})
        for namespace_key in ("user_id", "agent_id", "run_id"):
            if namespace_key in filters and namespace_key not in fallback:
                fallback[namespace_key] = filters.pop(namespace_key)
        fallback["filters"] = filters
        return mem.get_all(**fallback)


def _build_filters(
    *,
    category: Optional[str] = None,
    subject_type: Optional[str] = None,
    subject_id: Optional[str] = None,
    mode: Optional[str] = None,
) -> dict:
    filters: dict = {}
    if category:
        filters["category"] = category
    if subject_type:
        filters["subject_type"] = subject_type
    if subject_id:
        filters["subject_id"] = subject_id
    if mode:
        filters["mode"] = mode
    return filters


def _after_since(result: MemoryResult, since: Optional[datetime]) -> bool:
    if since is None:
        return True
    if not result.created_at:
        return False
    try:
        created = datetime.fromisoformat(result.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return created >= since


def _to_result(raw: dict, score: float = 0.0) -> MemoryResult:
    text = raw.get("memory") or raw.get("text") or raw.get("data") or ""
    meta = _extract_metadata(raw)
    item = memory_item_from_raw(str(text), meta)
    return MemoryResult(
        id=str(raw.get("id") or ""),
        text=str(text),
        score=raw.get("score", score),
        category=item.category,
        importance=item.importance,
        created_at=item.created_at.isoformat(),
        source=item.source,
        last_accessed=item.last_accessed.isoformat() if item.last_accessed else "",
        team_id=item.team_ids[0] if item.team_ids else "",
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        team_ids=item.team_ids,
        asset_ids=item.asset_ids,
        user_id=item.user_id,
        mode=item.mode,
        confidence=item.confidence,
        volatility=item.volatility,
        last_verified=item.last_verified.isoformat() if item.last_verified else "",
        verification_hint=item.verification_hint,
        content_hash=item.content_hash,
        schema_version=item.schema_version,
        metadata=meta,
    )


def _matches_associations(
    result: MemoryResult,
    *,
    team_id: Optional[str] = None,
    asset_id: Optional[str] = None,
) -> bool:
    if team_id and team_id not in (result.team_ids or []):
        return False
    if asset_id and asset_id not in (result.asset_ids or []):
        return False
    return True


def _expanded_limit(limit: int, has_post_filters: bool) -> int:
    if not has_post_filters:
        return limit
    return max(limit * 5, min(100, limit + 25))


def _repair_chroma_blob_seq_ids(chroma_path: Path) -> int:
    """Repair legacy Chroma rows before the 1.x Rust compactor initializes.

    Chroma 0.6-era stores can leave ``seq_id`` values as big-endian BLOBs.
    Chroma 1.x expects INTEGER there and crashes during compaction otherwise.
    """
    db_path = chroma_path / "chroma.sqlite3"
    marker_path = chroma_path / _CHROMA_BLOB_SEQ_ID_FIX_MARKER
    if marker_path.exists() or not db_path.exists():
        return 0

    repaired = 0
    try:
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table in ("embeddings", "max_seq_id"):
                if table not in tables:
                    continue

                columns = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                if "seq_id" not in columns:
                    continue

                rows = conn.execute(
                    f"SELECT rowid, seq_id FROM {table} WHERE typeof(seq_id) = 'blob'"
                ).fetchall()
                updates = []
                for rowid, seq_id in rows:
                    if not isinstance(seq_id, (bytes, bytearray)):
                        continue
                    # Chroma 1.5 can use sentinel-prefixed max_seq_id BLOBs;
                    # legacy 0.6 values are plain big-endian u64s.
                    if len(seq_id) != 8 or bytes(seq_id).startswith(b"\x11\x11"):
                        logger.warning(
                            "Skipping unexpected Chroma seq_id BLOB in %s at rowid %s",
                            table,
                            rowid,
                        )
                        continue
                    updates.append((int.from_bytes(seq_id, byteorder="big"), rowid))

                if updates:
                    conn.executemany(
                        f"UPDATE {table} SET seq_id = ? WHERE rowid = ?", updates
                    )
                    repaired += len(updates)
            conn.commit()

        marker_path.write_text(datetime.now(timezone.utc).isoformat())
        if repaired:
            logger.info("Repaired %d legacy Chroma embeddings seq_id values", repaired)
    except Exception as e:
        logger.warning("Failed to repair legacy Chroma seq_id values: %s", e)
    return repaired


class Mem0Backend:
    def __init__(
        self,
        config: MemoryConfig,
        usage_tracker: Optional[UsageTracker] = None,
    ):
        from mem0 import Memory

        self._extraction_model = config.extraction_model
        self._embedding_model = config.embedder
        self._shared_usage_tracker = usage_tracker
        self._extraction_tracker = UsageTracker()
        self._embedding_tracker = UsageTracker()

        chroma_path = config.path / "chroma"
        chroma_path.mkdir(parents=True, exist_ok=True)
        _repair_chroma_blob_seq_ids(chroma_path)
        openrouter_api_key = _get_openrouter_api_key()
        openrouter_base_url = _get_openrouter_base_url()

        def extraction_response_callback(_llm, response, _params) -> None:
            self._record_usage(
                response,
                self._extraction_tracker,
                gen_id_prefix="mem0-extract",
            )

        mem0_config = {
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": "ouro_agent_memory",
                    "path": str(chroma_path),
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": config.extraction_model,
                    "api_key": openrouter_api_key,
                    "openrouter_base_url": openrouter_base_url,
                    "response_callback": extraction_response_callback,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": config.embedder,
                    "api_key": openrouter_api_key,
                    "openai_base_url": openrouter_base_url,
                },
            },
        }

        if config.graph and config.graph.enabled:
            mem0_config["graph_store"] = {
                "provider": config.graph.provider,
                "config": config.graph.config,
            }

        self._mem = Memory.from_config(mem0_config)
        self._wrap_embedding_client()

    def _record_usage(
        self,
        response,
        tracker: UsageTracker,
        *,
        gen_id_prefix: str,
    ) -> None:
        record_usage_from_response(response, tracker, gen_id_prefix=gen_id_prefix)
        if self._shared_usage_tracker is not None:
            record_usage_from_response(
                response,
                self._shared_usage_tracker,
                gen_id_prefix=gen_id_prefix,
            )

    def _wrap_embedding_client(self) -> None:
        client = getattr(getattr(self._mem, "embedding_model", None), "client", None)
        embeddings = getattr(client, "embeddings", None)
        original_create = getattr(embeddings, "create", None)
        if original_create is None:
            logger.warning("mem0 embedding client does not expose embeddings.create")
            return

        @wraps(original_create)
        def tracked_create(*args, **kwargs):
            response = original_create(*args, **kwargs)
            self._record_usage(
                response,
                self._embedding_tracker,
                gen_id_prefix="mem0-embed",
            )
            return response

        embeddings.create = tracked_create

    def reset_usage(self) -> None:
        self._extraction_tracker.reset()
        self._embedding_tracker.reset()

    def usage_ledger(self) -> list[tuple[str, RunUsage]]:
        ledger: list[tuple[str, RunUsage]] = []
        if self._extraction_tracker.num_calls:
            ledger.append(
                (
                    "extraction",
                    RunUsage.from_tracker(
                        self._extraction_tracker,
                        model_id=self._extraction_model,
                    ),
                )
            )
        if self._embedding_tracker.num_calls:
            ledger.append(
                (
                    "embeddings",
                    RunUsage.from_tracker(
                        self._embedding_tracker,
                        model_id=self._embedding_model,
                    ),
                )
            )
        return ledger

    def search(
        self,
        query: str,
        agent_id: str,
        user_id: Optional[str] = None,
        limit: int = 10,
        team_id: Optional[str] = None,
        scope: str = "team",
        category: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        asset_id: Optional[str] = None,
        mode: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[MemoryResult]:
        effective_team_id = team_id if scope in {"personal", "team"} else None
        has_post_filters = bool(effective_team_id or asset_id)
        filters = _build_filters(
            category=category,
            subject_type=subject_type,
            subject_id=subject_id,
            mode=mode,
        )
        filters["agent_id"] = agent_id
        if user_id:
            filters["user_id"] = user_id
        kwargs: dict = {
            "query": query,
            "top_k": _expanded_limit(limit, has_post_filters),
            "filters": filters,
        }

        results = self._mem.search(**kwargs)
        res_list = _raw_results(results)

        out: list[MemoryResult] = []
        for r in res_list:
            result = _to_result(r, score=r.get("score", 0))
            if _after_since(result, since) and _matches_associations(
                result,
                team_id=effective_team_id,
                asset_id=asset_id,
            ):
                out.append(result)
                if len(out) >= limit:
                    break
        return out

    def add(
        self,
        content: str | list[dict],
        agent_id: str,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        team_id: Optional[str] = None,
        infer: bool = True,
    ) -> None:
        meta = dict(metadata or {})
        meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        meta.setdefault("category", "general")
        meta.setdefault("importance", 0.5)
        if user_id:
            meta.setdefault("user_id", user_id)
        if run_id:
            meta.setdefault("run_id", run_id)

        if team_id:
            meta["team_id"] = team_id

        text_for_hash = content if isinstance(content, str) else str(content)
        meta.setdefault("content_hash", content_hash(text_for_hash))
        item = memory_item_from_raw(text_for_hash, meta)
        if team_id and team_id not in item.team_ids:
            item.team_ids = [team_id, *item.team_ids]
        meta.update(to_metadata(item))

        if isinstance(content, str) and self._has_content_hash(
            agent_id=agent_id,
            content_hash_value=str(meta.get("content_hash") or ""),
            user_id=user_id,
        ):
            logger.debug(
                "Skipping duplicate memory write for hash %s", meta["content_hash"]
            )
            return

        kwargs: dict = {"agent_id": agent_id, "metadata": meta}
        if user_id:
            kwargs["user_id"] = user_id
        if run_id:
            kwargs["run_id"] = run_id
        self._mem.add(content, infer=infer, **kwargs)

    def _has_content_hash(
        self,
        *,
        agent_id: str,
        content_hash_value: str,
        user_id: Optional[str] = None,
    ) -> bool:
        if not content_hash_value:
            return False
        filters = {"agent_id": agent_id, "content_hash": content_hash_value}
        if user_id:
            filters["user_id"] = user_id
        kwargs: dict = {
            "top_k": 1,
            "filters": filters,
        }
        try:
            results = _mem0_get_all_compat(self._mem, **kwargs)
            return bool(_raw_results(results))
        except Exception as e:
            logger.debug("Memory dedupe check failed: %s", e)
            return False

    def get_all(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        limit: int = 100,
        team_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        asset_id: Optional[str] = None,
        category: Optional[str] = None,
        mode: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[MemoryResult]:
        has_post_filters = bool(team_id or asset_id)
        filters = _build_filters(
            category=category,
            subject_type=subject_type,
            subject_id=subject_id,
            mode=mode,
        )
        filters["agent_id"] = agent_id
        if user_id:
            filters["user_id"] = user_id
        kwargs: dict = {
            "top_k": _expanded_limit(limit, has_post_filters),
            "filters": filters,
        }
        results = _mem0_get_all_compat(self._mem, **kwargs)
        res_list = _raw_results(results)
        out: list[MemoryResult] = []
        for r in res_list:
            result = _to_result(r)
            if _after_since(result, since) and _matches_associations(
                result, team_id=team_id, asset_id=asset_id
            ):
                out.append(result)
                if len(out) >= limit:
                    break
        return out

    def update_metadata(self, memory_id: str, metadata: dict) -> None:
        try:
            raw = self._mem.get(memory_id)
            if not raw:
                raise ValueError(f"Memory with id {memory_id} not found")
            text = str(raw.get("memory") or raw.get("text") or raw.get("data") or "")
            merged_metadata = _extract_metadata(raw)
            merged_metadata.update(metadata)
            self._mem.update(memory_id, text, metadata=merged_metadata)
        except Exception as e:
            logger.warning("Failed to update memory metadata %s: %s", memory_id, e)

    def delete(self, memory_id: str) -> None:
        if not memory_id:
            return
        try:
            self._mem.delete(memory_id)
        except Exception as e:
            logger.warning("Failed to delete memory %s: %s", memory_id, e)

    def find_by_asset(
        self,
        asset_id: str,
        agent_id: str,
        team_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[MemoryResult]:
        if not asset_id:
            return []
        return self.get_all(
            agent_id=agent_id,
            limit=limit,
            team_id=team_id,
            asset_id=asset_id,
        )
