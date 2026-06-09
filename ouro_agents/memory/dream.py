"""Dream mode: memory maintenance, compaction, promotion, decay, and review.

The agent's "dream" cycle runs on a schedule (default nightly) to keep the
memory system healthy and prevent stale learnings from persisting:

1. Working memory compaction — rewrite when over token budget to merge/prune
2. Period log promotion — promote the previous period's important entries to working memory
3. Importance decay — reduce importance of old unaccessed memories
4. Confidence decay — reduce confidence on volatile memories not recently verified
5. Dream review — LLM re-evaluation of stale volatile memories
6. Comment consolidation — merge comments into owned USER:* posts
"""

import importlib.util
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Optional

from . import MemoryBackend, MemoryResult
from ..config import MemoryConfig
from ..constants import CHARS_PER_TOKEN
from .naming import period_key, period_key_offset, store_rhythm

logger = logging.getLogger(__name__)

_IMPORTANCE_DECAY_PERIOD_KEY = "last_importance_decay_period"
_CONFIDENCE_DECAY_PERIOD_KEY = "last_confidence_decay_period"
_REVIEW_PERIOD_KEY = "last_review_period"
_OPTIONAL_DREAM_PACKAGES = ("scipy", "ase", "pymatgen", "spacy")


def _summary_template() -> dict[str, Any]:
    return {
        "compacted": False,
        "promoted": 0,
        "importance_decayed": 0,
        "confidence_decayed": 0,
        "dream_review": {
            "reviewed": 0,
            "confirmed": 0,
            "contradicted": 0,
            "uncertain": 0,
        },
        "comments_merged": 0,
    }


@dataclass
class DreamOperation:
    """A planned or applied dream mutation, safe to persist in audit logs."""

    kind: str
    status: str
    target: str = ""
    memory_id: str = ""
    reason: str = ""
    old_metadata: dict[str, Any] = field(default_factory=dict)
    new_metadata: dict[str, Any] = field(default_factory=dict)
    excerpt: str = ""


@dataclass
class DreamPlan:
    scope: str
    agent_id: str
    team_id: str | None
    rhythm: str
    period: str
    dry_run: bool = False
    mode: str = "manual"
    operations: list[DreamOperation] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    source_logs: list[dict[str, Any]] = field(default_factory=list)

    def add_operation(self, operation: DreamOperation) -> None:
        self.operations.append(operation)

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_skip(self, kind: str, reason: str, **extra: Any) -> None:
        self.skipped.append({"kind": kind, "reason": reason, **extra})


@dataclass
class DreamResult:
    plan: DreamPlan
    summary: dict[str, Any]
    timings: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    audit_path: str = ""

    def to_summary(self) -> dict[str, Any]:
        out = dict(self.summary)
        out["dry_run"] = self.plan.dry_run
        out["warnings"] = len(self.plan.warnings)
        out["skipped"] = len(self.plan.skipped)
        out["planned_operations"] = len(
            [op for op in self.plan.operations if op.status == "planned"]
        )
        out["applied_operations"] = len(
            [op for op in self.plan.operations if op.status == "applied"]
        )
        if self.audit_path:
            out["audit_log"] = self.audit_path
        return out

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "mode": self.plan.mode,
            "dry_run": self.plan.dry_run,
            "scope": self.plan.scope,
            "agent_id": self.plan.agent_id,
            "team_id": self.plan.team_id,
            "rhythm": self.plan.rhythm,
            "period": self.plan.period,
            "summary": self.summary,
            "timings": self.timings,
            "health": self.plan.health,
            "source_logs": self.plan.source_logs,
            "operations": [asdict(op) for op in self.plan.operations],
            "skipped": self.plan.skipped,
            "warnings": self.plan.warnings,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Rhythm rollover marker
#
# The dream tick fires daily but only runs when a new rhythm period has begun
# since the last successful run. The last-processed period key is persisted so
# the gate survives restarts and recovers (runs once) after downtime.
# ---------------------------------------------------------------------------


def _dream_marker_path(workspace: Path) -> Path:
    return workspace / "data" / "last_dream_period"


def read_dream_marker(workspace: Path) -> str:
    """Return the last period key the dream cycle completed (or "")."""
    path = _dream_marker_path(workspace)
    try:
        return path.read_text().strip() if path.exists() else ""
    except OSError:
        return ""


def write_dream_marker(workspace: Path, period: str) -> None:
    """Record the period key the dream cycle just completed."""
    path = _dream_marker_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(period)
    except OSError as e:
        logger.warning("Failed to write dream marker: %s", e)


def _safe_scope(scope: str) -> str:
    return "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in scope)


def _short_excerpt(text: str, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _memory_id(mem: MemoryResult) -> str:
    return getattr(mem, "id", "") or mem.source


def _metadata_subset(mem: MemoryResult, keys: tuple[str, ...]) -> dict[str, Any]:
    metadata = dict(mem.metadata or {})
    for key in keys:
        value = getattr(mem, key, None)
        if value not in {None, ""} and key not in metadata:
            metadata[key] = value
    return {key: metadata.get(key) for key in keys if metadata.get(key) not in {None, ""}}


def _has_period_marker(mem: MemoryResult, marker: str, period: str) -> bool:
    return str((mem.metadata or {}).get(marker) or "") == period


def _set_memory_metadata(mem: MemoryResult, metadata: dict[str, Any]) -> None:
    mem.metadata = {**(mem.metadata or {}), **metadata}
    if "importance" in metadata:
        mem.importance = float(metadata["importance"])
    if "confidence" in metadata:
        mem.confidence = float(metadata["confidence"])
    if "last_verified" in metadata:
        mem.last_verified = str(metadata["last_verified"])


def _review_evidence(verdict: dict[str, Any]) -> str:
    evidence = str(verdict.get("evidence") or "none").strip().lower()
    allowed = {
        "none",
        "newer_memory",
        "recent_log",
        "route_probe",
        "doc_schema",
        "user_correction",
        "other_explicit",
    }
    return evidence if evidence in allowed else "none"


def _review_action(verdict: dict[str, Any]) -> str:
    action = str(verdict.get("action") or "").strip().lower()
    allowed = {"keep", "lower_confidence", "mark_stale", "delete", "replace"}
    return action if action in allowed else ""


def _has_explicit_review_evidence(evidence: str) -> bool:
    return evidence not in {"", "none"}


def _review_metadata(
    *,
    period: str,
    requested_status: str,
    effective_status: str,
    evidence: str,
    action: str,
    reason: str,
    replacement: Any,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        _REVIEW_PERIOD_KEY: period,
        "last_review_status": effective_status,
        "last_review_evidence": evidence,
        "last_review_action": action or "keep",
        "last_review_requested_status": requested_status,
    }
    if reason:
        metadata["last_review_reason"] = _short_excerpt(reason, 240)
    if isinstance(replacement, str) and replacement.strip():
        metadata["last_review_replacement"] = _short_excerpt(replacement, 240)
    return metadata


def _write_dream_audit(workspace: Path, result: DreamResult) -> str:
    audit_dir = workspace / "data" / "dream_runs"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = audit_dir / f"{timestamp}_{_safe_scope(result.plan.scope)}.json"
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_audit_dict(), indent=2, sort_keys=True))
        return str(path)
    except OSError as e:
        logger.warning("Failed to write dream audit log: %s", e)
        result.errors.append(f"audit_write_failed: {e}")
        return ""


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _doc_store_path_for(doc_store, name: str) -> Path | None:
    mapper = getattr(doc_store, "_name_to_path", None)
    if callable(mapper):
        try:
            return mapper(name)
        except Exception:
            return None
    backend_for = getattr(doc_store, "_backend", None)
    if callable(backend_for):
        try:
            backend = backend_for(name)
        except Exception:
            return None
        return _doc_store_path_for(backend, name)
    return None


def _preflight_checks(
    workspace: Path,
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    doc_store,
    team_id: str | None,
    plan: DreamPlan,
) -> None:
    health: dict[str, Any] = {
        "doc_store": {"available": doc_store is not None},
        "memory_backend": {
            "provider": config.provider,
            "has_get_all": hasattr(backend, "get_all"),
            "has_update_metadata": hasattr(backend, "update_metadata"),
            "has_delete": hasattr(backend, "delete"),
        },
        "optional_packages": {},
    }

    if doc_store:
        try:
            memory_name = doc_store.memory_name(agent_id)
            doc_store.read(memory_name)
            health["doc_store"]["readable"] = True
            health["doc_store"]["memory_name"] = memory_name
            path = _doc_store_path_for(doc_store, memory_name)
            if path:
                parent = path.parent if path.parent.exists() else _nearest_existing_parent(path.parent)
                health["doc_store"]["writable"] = os.access(parent, os.W_OK)
                health["doc_store"]["path"] = str(path)
            else:
                health["doc_store"]["writable"] = "not_probed"
        except Exception as e:
            health["doc_store"]["readable"] = False
            plan.add_warning(f"Doc store preflight failed: {e}")
    else:
        plan.add_warning("No doc store available; doc maintenance skipped.")

    if team_id:
        try:
            backend.get_all(agent_id=agent_id, limit=1, team_id=team_id)
            health["memory_backend"]["read_probe"] = "ok"
        except Exception as e:
            health["memory_backend"]["read_probe"] = "failed"
            plan.add_warning(f"Memory backend read probe failed: {e}")
    else:
        health["memory_backend"]["read_probe"] = "skipped_unscoped"

    if config.provider == "mem0":
        chroma_path = config.path / "chroma"
        health["chroma"] = {
            "path": str(chroma_path),
            "sqlite_exists": (chroma_path / "chroma.sqlite3").exists(),
            "seq_id_repair_marker": (
                chroma_path / ".seq_id_blob_fix_v2"
            ).exists(),
        }

    for package in _OPTIONAL_DREAM_PACKAGES:
        present = importlib.util.find_spec(package) is not None
        health["optional_packages"][package] = "available" if present else "missing"
        if not present:
            plan.add_warning(f"Optional package missing: {package}")

    plan.health = health


def _time_phase(result: DreamResult, name: str):
    class _PhaseTimer:
        def __enter__(self):
            self._start = perf_counter()
            return self

        def __exit__(self, *_exc):
            result.timings[name] = round(perf_counter() - self._start, 6)

    return _PhaseTimer()


COMPACTION_PROMPT = """\
You are a memory curator. Given the current contents of the agent's persistent \
working memory, rewrite it to be more concise and useful.

Rules:
- Remove duplicate or near-duplicate entries
- Remove stale entries that are no longer relevant (outdated facts, completed one-off tasks)
- Merge related entries into single concise statements
- Keep the same section structure: ## Facts, ## Preferences, ## Learnings
- Keep entries that represent durable knowledge, ongoing preferences, or hard-won learnings
- ALWAYS preserve [label](asset:<uuid>) links — these are direct references to Ouro assets and must not be stripped or rewritten
- Target: under {max_tokens} tokens (~{max_chars} characters)
- Preserve the YAML frontmatter header exactly as-is

Output the complete rewritten working memory content, nothing else."""

PROMOTION_PROMPT = """\
You are a memory curator. Given the previous period's log and the agent's current \
working memory, decide which log entries (if any) should be promoted to working \
memory as durable knowledge.

Rules:
- Only promote facts, patterns, or learnings that will be useful in FUTURE sessions
- Do NOT promote entries that duplicate or closely overlap with content already in the working memory. Check the existing memory below before deciding what to promote.
- Do NOT promote one-off task completions ("Published X post") unless they reveal a reusable pattern
- ALWAYS preserve [label](asset:<uuid>) links from log entries — these are direct references to Ouro assets
- Output a JSON array of objects: [{"section": "Facts"|"Preferences"|"Learnings", "entry": "text"}]
- If nothing is worth promoting, return an empty array: []
- Output ONLY the JSON array, no markdown fences, no explanation."""

DREAM_REVIEW_PROMPT = """\
You are reviewing an agent's stored memories for accuracy. Each memory below \
was learned at a specific point in time and may no longer be true — especially \
memories about system behavior, API responses, error states, or resource availability.

For each memory, determine whether it is likely still accurate or has become stale.

Memories to review:
{memories_block}

For each memory, output a JSON verdict:
[{{"id": "memory_id", "status": "confirmed"|"contradicted"|"uncertain", "evidence": "none"|"newer_memory"|"recent_log"|"route_probe"|"doc_schema"|"user_correction"|"other_explicit", "action": "keep"|"lower_confidence"|"mark_stale"|"delete"|"replace", "reason": "brief explanation", "replacement": "corrected fact if contradicted, else null"}}]

Guidelines:
- "confirmed": The fact is likely still true based on general knowledge and context.
- "contradicted": You have explicit newer evidence that the memory is now false.
- "uncertain": Not enough information to judge; leave for natural decay or mark stale.
- For errors/outages/API failures/endpoint behavior: age alone is NOT contradictory evidence. \
  Default to "uncertain" unless a newer memory, recent log, route probe, schema/doc check, \
  or user correction proves recovery or a changed contract.
- Use action "delete" only when status is "contradicted" and evidence is not "none".
- Use action "mark_stale" or "lower_confidence" for old operational claims without explicit evidence.
- Be conservative with "confirmed" — only confirm facts you're confident are durable.

Output ONLY the JSON array, no markdown fences, no explanation."""


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def compact_memory_md(
    workspace: Path,
    config: MemoryConfig,
    model,
    doc_store=None,
    agent_name: str = "",
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> bool:
    """Rewrite working memory if it exceeds the token budget. Returns True if compacted."""
    if not doc_store:
        if plan:
            plan.add_skip("compaction", "no_doc_store")
        return False
    post_name = doc_store.memory_name(agent_name)
    content = doc_store.read(post_name)

    if not content:
        if plan:
            plan.add_skip("compaction", "empty_memory_doc", target=post_name)
        return False

    tokens = _estimate_tokens(content)
    if tokens <= config.memory_md_max_tokens:
        logger.debug("Working memory is %d tokens, under %d budget", tokens, config.memory_md_max_tokens)
        if plan:
            plan.add_skip(
                "compaction",
                "under_token_budget",
                target=post_name,
                tokens=tokens,
                max_tokens=config.memory_md_max_tokens,
            )
        return False

    logger.info("Working memory is %d tokens, compacting to %d", tokens, config.memory_md_max_tokens)
    max_chars = config.memory_md_max_tokens * CHARS_PER_TOKEN

    try:
        result = model(
            [
                {
                    "role": "system",
                    "content": COMPACTION_PROMPT.format(
                        max_tokens=config.memory_md_max_tokens,
                        max_chars=max_chars,
                    ),
                },
                {"role": "user", "content": content},
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        new_tokens = _estimate_tokens(text)
        if plan:
            plan.add_operation(
                DreamOperation(
                    kind="compaction",
                    status="planned" if dry_run else "applied",
                    target=post_name,
                    old_metadata={"tokens": tokens},
                    new_metadata={"tokens": new_tokens},
                    excerpt=_short_excerpt(text),
                )
            )
        if dry_run:
            logger.info("Dry-run: would compact working memory: %d -> %d tokens", tokens, new_tokens)
            return True

        if not doc_store.write(post_name, text):
            raise RuntimeError(f"Failed to write {post_name}")

        logger.info("Compacted working memory: %d -> %d tokens", tokens, new_tokens)
        return True
    except Exception as e:
        if plan:
            plan.add_warning(f"Working memory compaction failed: {e}")
        logger.warning("Working memory compaction failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def _daily_keys_for_previous_period(rhythm: str) -> list[str]:
    previous_period = period_key_offset(rhythm, -1)
    if rhythm == "weekly":
        year_text, week_text = previous_period.split("-W", 1)
        start = date.fromisocalendar(int(year_text), int(week_text), 1)
        days = 7
    elif rhythm == "biweekly" and previous_period.endswith("-2w"):
        start = date.fromisoformat(previous_period[:-3])
        days = 14
    else:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days)]


def _promotion_log_content(
    doc_store,
    agent_name: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    rhythm = store_rhythm(doc_store)
    previous_period = period_key_offset(rhythm, -1)
    log_name = doc_store.log_name(agent_name, previous_period)
    log_content = doc_store.read(log_name).strip()
    sources = [
        {
            "name": log_name,
            "period": previous_period,
            "rhythm": rhythm,
            "chars": len(log_content),
            "used": bool(log_content),
        }
    ]

    if log_content or rhythm not in {"weekly", "biweekly"}:
        return previous_period, log_content, sources

    daily_parts: list[str] = []
    for daily_key in _daily_keys_for_previous_period(rhythm):
        daily_name = doc_store.log_name(agent_name, daily_key)
        daily_content = doc_store.read(daily_name).strip()
        sources.append(
            {
                "name": daily_name,
                "period": daily_key,
                "rhythm": "daily",
                "chars": len(daily_content),
                "used": bool(daily_content),
            }
        )
        if daily_content:
            daily_parts.append(f"## {daily_key}\n{daily_content}")

    return previous_period, "\n\n".join(daily_parts).strip(), sources


def has_recent_dream_activity(
    doc_store,
    agent_name: str,
    rhythm: str,
    current_period: str | None = None,
    previous_period: str | None = None,
) -> bool:
    """Return True if this scope has logs the dream cycle can use."""
    current_period = current_period or period_key(rhythm)
    previous_period = previous_period or period_key_offset(rhythm, -1)
    if doc_store.exists(doc_store.log_name(agent_name, current_period)):
        return True
    if doc_store.exists(doc_store.log_name(agent_name, previous_period)):
        return True
    if rhythm in {"weekly", "biweekly"}:
        return any(
            doc_store.exists(doc_store.log_name(agent_name, daily_key))
            for daily_key in _daily_keys_for_previous_period(rhythm)
        )
    return False


def promote_log_entries(
    workspace: Path,
    model,
    doc_store=None,
    agent_name: str = "",
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> int:
    """Promote worthy entries from the previous period's log to working memory.

    "Previous period" follows the doc store's rhythm: the prior day (daily),
    the prior ISO week (weekly), or the prior 2-week window (biweekly). Returns
    the number of promoted entries.
    """
    if not doc_store:
        if plan:
            plan.add_skip("promotion", "no_doc_store")
        return 0

    previous_period, log_content, sources = _promotion_log_content(doc_store, agent_name)
    if plan:
        plan.source_logs.extend(sources)
    memory_name = doc_store.memory_name(agent_name)
    memory_content = doc_store.read(memory_name)

    if not log_content or len(log_content) < 20:
        if plan:
            plan.add_skip(
                "promotion",
                "previous_period_log_empty",
                period=previous_period,
                sources=sources,
            )
        return 0

    try:
        result = model(
            [
                {"role": "system", "content": PROMOTION_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Previous period's log:\n{log_content}\n\n"
                        f"Current working memory:\n{memory_content}"
                    ),
                },
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        entries = json.loads(text)
        if not isinstance(entries, list) or not entries:
            if plan:
                plan.add_skip("promotion", "model_returned_no_entries", period=previous_period)
            return 0

        content = memory_content
        promoted_entries = []
        for entry in entries:
            section = entry.get("section", "Facts")
            entry_text = entry.get("entry", "").strip()
            if not entry_text:
                continue
            promoted_entries.append({"section": section, "entry": entry_text})

            header = f"## {section}"
            bullet = f"- {entry_text}\n"
            if header in content:
                idx = content.index(header) + len(header)
                line_end = content.find("\n", idx)
                if line_end == -1:
                    content += "\n"
                    next_newline = len(content)
                else:
                    next_newline = line_end + 1
                content = content[:next_newline] + bullet + content[next_newline:]
            else:
                content = content.rstrip() + f"\n\n{header}\n{bullet}"

        if not promoted_entries:
            if plan:
                plan.add_skip("promotion", "model_entries_empty_after_filter")
            return 0

        if plan:
            plan.add_operation(
                DreamOperation(
                    kind="promotion",
                    status="planned" if dry_run else "applied",
                    target=memory_name,
                    reason=f"promote_from_period:{previous_period}",
                    old_metadata={"entries": 0},
                    new_metadata={"entries": len(promoted_entries)},
                    excerpt=_short_excerpt("; ".join(e["entry"] for e in promoted_entries)),
                )
            )
        if dry_run:
            logger.info(
                "Dry-run: would promote %d entries from %s log to working memory",
                len(promoted_entries),
                previous_period,
            )
            return len(promoted_entries)

        if not doc_store.write(memory_name, content):
            raise RuntimeError(f"Failed to write {memory_name}")

        logger.info(
            "Promoted %d entries from %s log to working memory",
            len(promoted_entries),
            previous_period,
        )
        return len(promoted_entries)
    except Exception as e:
        if plan:
            plan.add_warning(f"Log promotion failed: {e}")
        logger.warning("Log promotion failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Importance decay (existing behavior)
# ---------------------------------------------------------------------------


def decay_old_memories(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
    period: str | None = None,
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> int:
    """Apply category-specific importance decay to old memories. Returns count."""
    decay_rules = config.decay_rules or {}
    if not decay_rules and not config.decay_after_days:
        return 0
    if not team_id:
        if plan:
            plan.add_skip("importance_decay", "unscoped_shared_pass")
        return 0

    if all_memories is None:
        try:
            all_memories = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
        except Exception as e:
            logger.warning("Failed to load memories for decay: %s", e)
            return 0

    current_period = period or period_key(config.rhythm)
    decayed = 0

    for mem in all_memories:
        memory_id = _memory_id(mem)
        if _has_period_marker(mem, _IMPORTANCE_DECAY_PERIOD_KEY, current_period):
            if plan:
                plan.add_skip(
                    "importance_decay",
                    "already_applied_this_period",
                    memory_id=memory_id,
                    period=current_period,
                )
            continue
        if not mem.created_at:
            continue
        rule = decay_rules.get(mem.category)
        if rule:
            after_days = rule.get("after_days")
            factor = float(rule.get("factor", 0.5))
        else:
            after_days = config.decay_after_days
            factor = 0.5
        if after_days is None:
            continue
        try:
            created = datetime.fromisoformat(mem.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        cutoff = datetime.now(timezone.utc) - timedelta(days=int(after_days))
        if created < cutoff and mem.importance > 0.1:
            new_importance = max(0.1, mem.importance * factor)
            metadata = {
                "importance": new_importance,
                _IMPORTANCE_DECAY_PERIOD_KEY: current_period,
            }
            old_metadata = _metadata_subset(
                mem,
                ("importance", _IMPORTANCE_DECAY_PERIOD_KEY),
            )
            if plan:
                plan.add_operation(
                    DreamOperation(
                        kind="importance_decay",
                        status="planned" if dry_run else "applied",
                        memory_id=memory_id,
                        old_metadata=old_metadata,
                        new_metadata=metadata,
                        reason=f"older_than_{after_days}_days",
                        excerpt=_short_excerpt(mem.text),
                    )
                )
            try:
                if not dry_run:
                    backend.update_metadata(memory_id, metadata)
                _set_memory_metadata(mem, metadata)
                decayed += 1
            except Exception:
                pass

    if decayed:
        logger.info("Decayed importance on %d old memories", decayed)
    return decayed


# ---------------------------------------------------------------------------
# Confidence decay (volatility-based)
# ---------------------------------------------------------------------------


def decay_stale_confidence(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
    period: str | None = None,
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> int:
    """Reduce confidence on volatile memories that haven't been re-verified.

    Higher volatility means faster confidence decay. A memory with volatility=0.9
    has a half-life of ~2 days; volatility=0.5 has ~7 days. Memories below the
    volatility threshold (0.2) are considered stable and never decay in confidence.
    """
    if not config.confidence_decay_enabled:
        return 0
    if not team_id:
        if plan:
            plan.add_skip("confidence_decay", "unscoped_shared_pass")
        return 0

    if all_memories is None:
        try:
            all_memories = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
        except Exception as e:
            logger.warning("Failed to load memories for confidence decay: %s", e)
            return 0

    now = datetime.now(timezone.utc)
    current_period = period or period_key(config.rhythm)
    decayed = 0

    for mem in all_memories:
        memory_id = _memory_id(mem)
        if _has_period_marker(mem, _CONFIDENCE_DECAY_PERIOD_KEY, current_period):
            if plan:
                plan.add_skip(
                    "confidence_decay",
                    "already_applied_this_period",
                    memory_id=memory_id,
                    period=current_period,
                )
            continue
        if mem.volatility <= 0.2:
            continue
        if mem.confidence <= 0.1:
            continue

        # Determine the reference point: last_verified or created_at
        reference = None
        if mem.last_verified:
            try:
                reference = datetime.fromisoformat(mem.last_verified)
                if reference.tzinfo is None:
                    reference = reference.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                reference = None
        if reference is None and mem.created_at:
            try:
                reference = datetime.fromisoformat(mem.created_at)
                if reference.tzinfo is None:
                    reference = reference.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

        if reference is None:
            continue

        days_since = (now - reference).total_seconds() / 86400
        # Half-life: volatility 0.9 → 2 days, 0.5 → 7 days, 0.3 → 10 days
        half_life_days = max(2, int(14 * (1 - mem.volatility)))

        if days_since <= half_life_days:
            continue

        decay_factor = 0.5 ** (days_since / half_life_days)
        new_confidence = max(0.1, mem.confidence * decay_factor)

        if abs(new_confidence - mem.confidence) < 0.01:
            continue

        metadata = {
            "confidence": new_confidence,
            _CONFIDENCE_DECAY_PERIOD_KEY: current_period,
        }
        old_metadata = _metadata_subset(
            mem,
            ("confidence", _CONFIDENCE_DECAY_PERIOD_KEY),
        )
        if plan:
            plan.add_operation(
                DreamOperation(
                    kind="confidence_decay",
                    status="planned" if dry_run else "applied",
                    memory_id=memory_id,
                    old_metadata=old_metadata,
                    new_metadata=metadata,
                    reason=f"volatile_half_life_{half_life_days}_days",
                    excerpt=_short_excerpt(mem.text),
                )
            )
        try:
            if not dry_run:
                backend.update_metadata(memory_id, metadata)
            _set_memory_metadata(mem, metadata)
            decayed += 1
        except Exception:
            pass

    if decayed:
        logger.info("Decayed confidence on %d volatile memories", decayed)
    return decayed


# ---------------------------------------------------------------------------
# Dream review (LLM re-evaluation of stale memories)
# ---------------------------------------------------------------------------


def _select_review_candidates(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
    period: str | None = None,
    plan: DreamPlan | None = None,
) -> list[MemoryResult]:
    """Select memories that are candidates for dream review.

    Targets volatile memories whose confidence has decayed into the uncertain
    zone but that still have enough importance to matter.
    """
    if not team_id:
        if plan:
            plan.add_skip("dream_review", "unscoped_shared_pass")
        return []

    if all_memories is None:
        try:
            all_memories = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
        except Exception as e:
            logger.warning("Failed to load memories for dream review: %s", e)
            return []

    current_period = period or period_key(config.rhythm)
    candidates = [
        mem for mem in all_memories
        if mem.volatility > 0.4
        and mem.confidence < 0.5
        and mem.importance > 0.2
        and not _has_period_marker(mem, _REVIEW_PERIOD_KEY, current_period)
    ]

    # Sort by importance descending — review the most impactful stale memories first
    candidates.sort(key=lambda m: m.importance, reverse=True)
    return candidates[:config.dream_review_max_per_run]


def review_stale_memories(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    model,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
    period: str | None = None,
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> dict:
    """LLM-review memories that have decayed into the uncertain zone.

    Returns a summary: {"reviewed": int, "confirmed": int, "contradicted": int, "uncertain": int}
    """
    result = {"reviewed": 0, "confirmed": 0, "contradicted": 0, "uncertain": 0}

    if not config.dream_review_enabled:
        return result

    current_period = period or period_key(config.rhythm)
    candidates = _select_review_candidates(
        backend,
        agent_id,
        config,
        team_id=team_id,
        all_memories=all_memories,
        period=current_period,
        plan=plan,
    )
    if not candidates:
        return result

    # Build the review block
    memory_lines = []
    for mem in candidates:
        age_days = 0
        if mem.created_at:
            try:
                created = datetime.fromisoformat(mem.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = int((datetime.now(timezone.utc) - created).total_seconds() / 86400)
            except (ValueError, TypeError):
                pass
        memory_lines.append(
            f"- ID: {mem.id}\n"
            f"  Text: {mem.text}\n"
            f"  Category: {mem.category}\n"
            f"  Stored: {age_days} days ago\n"
            f"  Volatility: {mem.volatility:.1f}\n"
            f"  Current confidence: {mem.confidence:.2f}"
        )

    memories_block = "\n".join(memory_lines)

    try:
        llm_result = model(
            [
                {
                    "role": "system",
                    "content": DREAM_REVIEW_PROMPT.format(memories_block=memories_block),
                },
                {
                    "role": "user",
                    "content": "Review the memories above and return your verdicts as JSON.",
                },
            ],
        )
        text = llm_result.content if hasattr(llm_result, "content") else str(llm_result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        verdicts = json.loads(text)
        if not isinstance(verdicts, list):
            return result

        now_iso = datetime.now(timezone.utc).isoformat()
        candidates_by_id = {_memory_id(mem): mem for mem in candidates}
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            memory_id = str(verdict.get("id") or "")
            requested_status = str(verdict.get("status") or "uncertain").strip().lower()
            if requested_status not in {"confirmed", "contradicted", "uncertain"}:
                requested_status = "uncertain"
            if not memory_id:
                continue

            result["reviewed"] += 1
            evidence = _review_evidence(verdict)
            action = _review_action(verdict)
            reason = str(verdict.get("reason") or "").strip()
            replacement = verdict.get("replacement")
            effective_status = requested_status
            can_delete = (
                requested_status == "contradicted"
                and action == "delete"
                and _has_explicit_review_evidence(evidence)
            )
            if requested_status == "contradicted" and not _has_explicit_review_evidence(evidence):
                effective_status = "uncertain"
                action = action or "mark_stale"
                reason = (
                    reason
                    or "Contradiction requested without explicit newer evidence; marking stale."
                )
            elif requested_status == "contradicted" and action not in {"delete", "replace"}:
                action = action or "mark_stale"

            mem = candidates_by_id.get(memory_id)
            verdict_metadata = _review_metadata(
                period=current_period,
                requested_status=requested_status,
                effective_status=effective_status,
                evidence=evidence,
                action=action,
                reason=reason,
                replacement=replacement,
            )

            if effective_status == "confirmed":
                result["confirmed"] += 1
                metadata = {
                    "confidence": 0.8,
                    "last_verified": now_iso,
                    **verdict_metadata,
                }
                if plan:
                    plan.add_operation(
                        DreamOperation(
                            kind="dream_review",
                            status="planned" if dry_run else "applied",
                            memory_id=memory_id,
                            old_metadata=(
                                _metadata_subset(
                                    mem,
                                    ("confidence", "last_verified", _REVIEW_PERIOD_KEY),
                                )
                                if mem
                                else {}
                            ),
                            new_metadata=metadata,
                            reason=reason or "confirmed",
                            excerpt=_short_excerpt(mem.text if mem else ""),
                        )
                    )
                if not dry_run:
                    backend.update_metadata(memory_id, metadata)
                if mem:
                    _set_memory_metadata(mem, metadata)
            elif effective_status == "contradicted" and can_delete:
                result["contradicted"] += 1
                if plan:
                    plan.add_operation(
                        DreamOperation(
                            kind="dream_review",
                            status="planned" if dry_run else "applied",
                            memory_id=memory_id,
                            old_metadata=(
                                _metadata_subset(
                                    mem,
                                    ("confidence", "last_verified", _REVIEW_PERIOD_KEY),
                                )
                                if mem
                                else {}
                            ),
                            new_metadata={
                                "deleted": True,
                                **verdict_metadata,
                            },
                            reason=reason or "contradicted",
                            excerpt=_short_excerpt(mem.text if mem else ""),
                        )
                    )
                if not dry_run:
                    backend.delete(memory_id)
            elif effective_status == "contradicted":
                result["contradicted"] += 1
                metadata = {
                    "confidence": 0.1,
                    **verdict_metadata,
                }
                if plan:
                    plan.add_operation(
                        DreamOperation(
                            kind="dream_review",
                            status="planned" if dry_run else "applied",
                            memory_id=memory_id,
                            old_metadata=(
                                _metadata_subset(
                                    mem,
                                    ("confidence", "last_verified", _REVIEW_PERIOD_KEY),
                                )
                                if mem
                                else {}
                            ),
                            new_metadata=metadata,
                            reason=reason or "contradicted",
                            excerpt=_short_excerpt(mem.text if mem else ""),
                        )
                    )
                if not dry_run:
                    backend.update_metadata(memory_id, metadata)
                if mem:
                    _set_memory_metadata(mem, metadata)
            else:
                result["uncertain"] += 1
                metadata = verdict_metadata
                if plan:
                    plan.add_operation(
                        DreamOperation(
                            kind="dream_review",
                            status="planned" if dry_run else "applied",
                            memory_id=memory_id,
                            old_metadata=(
                                _metadata_subset(mem, (_REVIEW_PERIOD_KEY,))
                                if mem
                                else {}
                            ),
                            new_metadata=metadata,
                            reason=reason or "uncertain",
                            excerpt=_short_excerpt(mem.text if mem else ""),
                        )
                    )
                if not dry_run:
                    backend.update_metadata(memory_id, metadata)
                if mem:
                    _set_memory_metadata(mem, metadata)

        logger.info(
            "Dream review complete: %d reviewed, %d confirmed, %d contradicted, %d uncertain",
            result["reviewed"], result["confirmed"], result["contradicted"], result["uncertain"],
        )
    except Exception as e:
        if plan:
            plan.add_warning(f"Dream review failed: {e}")
        logger.warning("Dream review failed: %s", e)

    return result


# ---------------------------------------------------------------------------
# Comment consolidation
# ---------------------------------------------------------------------------


def _consolidate_user_comments(
    doc_store,
    agent_name: str,
    model,
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> int:
    """Merge comments from other agents into USER:* posts this agent owns."""
    if not doc_store:
        if plan:
            plan.add_skip("comment_consolidation", "no_doc_store")
        return 0

    merged = 0
    team_posts = doc_store.search("USER:")
    for post in team_posts:
        name = post.get("name") or ""
        if not name.startswith("USER:"):
            continue
        if not doc_store.is_owner(name):
            continue

        comments = doc_store.read_comments(name)
        if not comments:
            continue

        new_entries = []
        for c in comments:
            content = c.get("content_markdown") or c.get("content", "")
            if content.strip():
                new_entries.append(content.strip())

        if not new_entries:
            continue

        section_md = "## Recent Contributions\n" + "\n".join(f"- {e}" for e in new_entries) + "\n"
        if plan:
            plan.add_operation(
                DreamOperation(
                    kind="comment_consolidation",
                    status="planned" if dry_run else "applied",
                    target=name,
                    old_metadata={"comments": len(new_entries)},
                    new_metadata={"merged_comments": len(new_entries)},
                    excerpt=_short_excerpt("; ".join(new_entries)),
                )
            )
        if dry_run:
            merged += len(new_entries)
            logger.info("Dry-run: would consolidate %d comments into %s", len(new_entries), name)
            continue

        if not doc_store.append(name, section_md):
            logger.warning("Failed to consolidate comments into %s", name)
            if plan:
                plan.add_warning(f"Failed to consolidate comments into {name}")
            continue
        merged += len(new_entries)
        logger.info("Consolidated %d comments into %s", len(new_entries), name)

    return merged


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_dream(
    workspace: Path,
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    model,
    doc_store=None,
    team_id: str | None = None,
    dry_run: bool = False,
    mode: str = "manual",
) -> dict:
    """Run the full dream cycle: maintenance, decay, review. Returns a summary dict."""
    results = _summary_template()
    rhythm = config.rhythm
    current_period = period_key(rhythm)
    scope = team_id or "shared"
    plan = DreamPlan(
        scope=scope,
        agent_id=agent_id,
        team_id=team_id,
        rhythm=rhythm,
        period=current_period,
        dry_run=dry_run,
        mode=mode,
    )
    dream_result = DreamResult(plan=plan, summary=results)
    started = perf_counter()

    if not config.dream_enabled:
        plan.add_skip("dream", "disabled")
        dream_result.timings["total"] = round(perf_counter() - started, 6)
        dream_result.audit_path = _write_dream_audit(workspace, dream_result)
        return dream_result.to_summary()

    with _time_phase(dream_result, "preflight"):
        _preflight_checks(
            workspace,
            backend,
            agent_id,
            config,
            doc_store,
            team_id,
            plan,
        )

    # Doc-based maintenance (works with or without team_id)
    with _time_phase(dream_result, "compaction"):
        results["compacted"] = compact_memory_md(
            workspace,
            config,
            model,
            doc_store=doc_store,
            agent_name=agent_id,
            dry_run=dry_run,
            plan=plan,
        )
    with _time_phase(dream_result, "promotion"):
        results["promoted"] = promote_log_entries(
            workspace,
            model,
            doc_store=doc_store,
            agent_name=agent_id,
            dry_run=dry_run,
            plan=plan,
        )

    # Vector-memory operations: load once, share across all three passes
    all_memories: list[MemoryResult] | None = None
    if team_id:
        with _time_phase(dream_result, "load_vector_memories"):
            try:
                loaded = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
                if dry_run:
                    all_memories = [mem.model_copy(deep=True) for mem in loaded]
                else:
                    all_memories = loaded
            except Exception as e:
                warning = f"Failed to load memories for dream cycle: {e}"
                plan.add_warning(warning)
                logger.warning(warning)
                all_memories = []

    with _time_phase(dream_result, "importance_decay"):
        results["importance_decayed"] = decay_old_memories(
            backend,
            agent_id,
            config,
            team_id=team_id,
            all_memories=all_memories,
            period=current_period,
            dry_run=dry_run,
            plan=plan,
        )
    with _time_phase(dream_result, "confidence_decay"):
        results["confidence_decayed"] = decay_stale_confidence(
            backend,
            agent_id,
            config,
            team_id=team_id,
            all_memories=all_memories,
            period=current_period,
            dry_run=dry_run,
            plan=plan,
        )
    with _time_phase(dream_result, "dream_review"):
        results["dream_review"] = review_stale_memories(
            backend,
            agent_id,
            config,
            model,
            team_id=team_id,
            all_memories=all_memories,
            period=current_period,
            dry_run=dry_run,
            plan=plan,
        )

    with _time_phase(dream_result, "comment_consolidation"):
        results["comments_merged"] = _consolidate_user_comments(
            doc_store,
            agent_id,
            model,
            dry_run=dry_run,
            plan=plan,
        )

    dream_result.timings["total"] = round(perf_counter() - started, 6)
    dream_result.audit_path = _write_dream_audit(workspace, dream_result)
    logger.info("Dream cycle complete: %s", dream_result.to_summary())
    return dream_result.to_summary()
