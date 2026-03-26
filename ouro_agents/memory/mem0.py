import logging
import os
from functools import wraps
from datetime import datetime, timezone
from typing import List, Optional

from ..config import MemoryConfig
from ..usage import RunUsage, UsageTracker, record_usage_from_response
from . import MemoryBackend, MemoryResult

logger = logging.getLogger(__name__)


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
    return {
        "category": meta.get("category", "general"),
        "importance": meta.get("importance", 0.5),
        "created_at": meta.get("created_at", ""),
        "source": meta.get("source", ""),
        "last_accessed": meta.get("last_accessed", ""),
    }


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
    ) -> List[MemoryResult]:
        kwargs: dict = {"query": query, "agent_id": agent_id, "limit": limit}
        if user_id:
            kwargs["user_id"] = user_id
        results = self._mem.search(**kwargs)
        res_list = results.get("results", []) if isinstance(results, dict) else results

        out: list[MemoryResult] = []
        for r in res_list:
            meta = _extract_metadata(r)
            out.append(
                MemoryResult(
                    text=r["memory"],
                    score=r.get("score", 0),
                    **meta,
                )
            )
        return out

    def add(
        self,
        content: str | list[dict],
        agent_id: str,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        meta = dict(metadata or {})
        meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        meta.setdefault("category", "general")
        meta.setdefault("importance", 0.5)

        kwargs: dict = {"agent_id": agent_id, "metadata": meta}
        if user_id:
            kwargs["user_id"] = user_id
        if run_id:
            kwargs["run_id"] = run_id
        self._mem.add(content, **kwargs)

    def get_all(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryResult]:
        kwargs: dict = {"agent_id": agent_id, "limit": limit}
        if user_id:
            kwargs["user_id"] = user_id
        results = self._mem.get_all(**kwargs)
        res_list = results.get("results", []) if isinstance(results, dict) else results
        out: list[MemoryResult] = []
        for r in res_list:
            meta = _extract_metadata(r)
            out.append(MemoryResult(text=r["memory"], score=0, **meta))
        return out

    def update_metadata(self, memory_id: str, metadata: dict) -> None:
        try:
            self._mem.update(memory_id, metadata=metadata)
        except Exception as e:
            logger.warning("Failed to update memory metadata %s: %s", memory_id, e)
