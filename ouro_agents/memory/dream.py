"""Reusable maintenance operations for evidence-driven dream runs."""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import MemoryConfig
from ..constants import CHARS_PER_TOKEN, parse_llm_json, strip_markdown_fence
from ..tools.workspace_paths import protected_data
from . import MemoryBackend, MemoryResult
from .naming import period_key, period_key_offset, store_rhythm

logger = logging.getLogger(__name__)

_STRENGTH_DECAY_PERIOD_KEY = "last_strength_decay_period"
_AUDIT_EXCERPT_LIMIT = 800
_LLM_EXCERPT_LIMIT = 1500


@dataclass
class DreamOperation:
    """A planned or applied maintenance mutation."""

    kind: str
    status: str
    target: str = ""
    memory_id: str = ""
    reason: str = ""
    old_metadata: dict[str, Any] = field(default_factory=dict)
    new_metadata: dict[str, Any] = field(default_factory=dict)
    excerpt: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DreamPlan:
    """Maintenance activity collected for an agentic dream run."""

    scope: str
    agent_id: str
    team_id: str | None
    rhythm: str
    period: str
    dry_run: bool = False
    mode: str = "manual"
    run_id: str = ""
    operations: list[DreamOperation] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    source_logs: list[dict[str, Any]] = field(default_factory=list)

    def add_operation(self, operation: DreamOperation) -> None:
        self.operations.append(operation)

    def add_llm_call(
        self,
        phase: str,
        *,
        system: str = "",
        user: str = "",
        response: str = "",
        **extra: Any,
    ) -> None:
        """Record a truncated maintenance-model prompt and response."""
        self.llm_calls.append(
            _llm_audit(
                phase=phase,
                system=system,
                user=user,
                response=response,
                **extra,
            )
        )

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_skip(self, kind: str, reason: str, **extra: Any) -> None:
        self.skipped.append({"kind": kind, "reason": reason, **extra})


def _short_excerpt(text: str, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _llm_audit(
    *,
    phase: str,
    system: str = "",
    user: str = "",
    response: str = "",
    limit: int = _LLM_EXCERPT_LIMIT,
    **extra: Any,
) -> dict[str, Any]:
    system_text = system or ""
    user_text = user or ""
    response_text = response or ""
    audit: dict[str, Any] = {
        "phase": phase,
        "system": _short_excerpt(system_text, limit),
        "user": _short_excerpt(user_text, limit),
        "response": _short_excerpt(response_text, limit),
        "system_chars": len(system_text),
        "user_chars": len(user_text),
        "response_chars": len(response_text),
    }
    audit.update(extra)
    return audit


def _model_text(result: Any) -> str:
    if result is None:
        return ""
    if hasattr(result, "content"):
        return str(result.content or "")
    return str(result)


def _memory_id(memory: MemoryResult) -> str:
    return getattr(memory, "id", "") or memory.source


def _metadata_subset(
    memory: MemoryResult,
    keys: tuple[str, ...],
) -> dict[str, Any]:
    metadata = dict(memory.metadata or {})
    for key in keys:
        value = getattr(memory, key, None)
        if value not in {None, ""} and key not in metadata:
            metadata[key] = value
    return {
        key: metadata.get(key)
        for key in keys
        if metadata.get(key) not in {None, ""}
    }


def _has_period_marker(memory: MemoryResult, marker: str, period: str) -> bool:
    return str((memory.metadata or {}).get(marker) or "") == period


def _set_memory_metadata(
    memory: MemoryResult,
    metadata: dict[str, Any],
) -> None:
    memory.metadata = {**(memory.metadata or {}), **metadata}
    if "strength" in metadata:
        memory.strength = float(metadata["strength"])
    if "stability" in metadata:
        memory.stability = str(metadata["stability"])
    if "last_verified" in metadata:
        memory.last_verified = str(metadata["last_verified"])


COMPACTION_PROMPT = """\
You are a memory curator. Given the current contents of the agent's persistent \
working memory, rewrite it to be more concise and useful.

Rules:
- Remove duplicate or near-duplicate entries
- Remove stale entries that are no longer relevant (outdated facts, completed one-off tasks)
- When one entry records an explicit prohibition, ban, or reversal (e.g. "X is \
blacklisted", "stop doing Y"), the latest stated directive wins: remove or rewrite \
any other entries that recommend, promote, or give how-to guidance for the banned \
thing, keeping only the prohibition itself
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


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def compact_memory_md(
    workspace: Path,
    config: MemoryConfig,
    model,
    doc_store=None,
    agent_name: str = "",
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> bool:
    """Rewrite working memory when it exceeds the configured token budget."""
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
        logger.debug(
            "Working memory is %d tokens, under %d budget",
            tokens,
            config.memory_md_max_tokens,
        )
        if plan:
            plan.add_skip(
                "compaction",
                "under_token_budget",
                target=post_name,
                tokens=tokens,
                max_tokens=config.memory_md_max_tokens,
            )
        return False

    logger.info(
        "Working memory is %d tokens, compacting to %d",
        tokens,
        config.memory_md_max_tokens,
    )
    max_chars = config.memory_md_max_tokens * CHARS_PER_TOKEN
    system_prompt = COMPACTION_PROMPT.format(
        max_tokens=config.memory_md_max_tokens,
        max_chars=max_chars,
    )
    try:
        result = model(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
        )
        text = strip_markdown_fence(_model_text(result))
        new_tokens = _estimate_tokens(text)
        if plan:
            plan.add_llm_call(
                "compaction",
                system=system_prompt,
                user=content,
                response=text,
            )
            plan.add_operation(
                DreamOperation(
                    kind="compaction",
                    status="planned" if dry_run else "applied",
                    target=post_name,
                    old_metadata={"tokens": tokens},
                    new_metadata={"tokens": new_tokens},
                    excerpt=_short_excerpt(text, _AUDIT_EXCERPT_LIMIT),
                    detail={
                        "before_excerpt": _short_excerpt(
                            content,
                            _AUDIT_EXCERPT_LIMIT,
                        ),
                        "after_excerpt": _short_excerpt(
                            text,
                            _AUDIT_EXCERPT_LIMIT,
                        ),
                    },
                )
            )
        if dry_run:
            logger.info(
                "Dry-run: would compact working memory: %d -> %d tokens",
                tokens,
                new_tokens,
            )
            return True

        if not doc_store.write(post_name, text):
            raise RuntimeError(f"Failed to write {post_name}")

        logger.info(
            "Compacted working memory: %d -> %d tokens",
            tokens,
            new_tokens,
        )
        return True
    except Exception as exc:
        if plan:
            plan.add_warning(f"Working memory compaction failed: {exc}")
        logger.warning("Working memory compaction failed: %s", exc)
        return False


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
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range(days)
    ]


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


def promote_log_entries(
    workspace: Path,
    model,
    doc_store=None,
    agent_name: str = "",
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> int:
    """Promote durable entries from the previous period into working memory."""
    if not doc_store:
        if plan:
            plan.add_skip("promotion", "no_doc_store")
        return 0

    previous_period, log_content, sources = _promotion_log_content(
        doc_store,
        agent_name,
    )
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
        user_prompt = (
            f"Previous period's log:\n{log_content}\n\n"
            f"Current working memory:\n{memory_content}"
        )
        result = model(
            [
                {"role": "system", "content": PROMOTION_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = _model_text(result)
        if plan:
            plan.add_llm_call(
                "promotion",
                system=PROMOTION_PROMPT,
                user=user_prompt,
                response=text,
                period=previous_period,
            )
        entries = parse_llm_json(str(text), expect=list)
        if not isinstance(entries, list) or not entries:
            if plan:
                plan.add_skip(
                    "promotion",
                    "model_returned_no_entries",
                    period=previous_period,
                )
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
                index = content.index(header) + len(header)
                line_end = content.find("\n", index)
                if line_end == -1:
                    content += "\n"
                    next_newline = len(content)
                else:
                    next_newline = line_end + 1
                content = (
                    content[:next_newline]
                    + bullet
                    + content[next_newline:]
                )
            else:
                content = content.rstrip() + f"\n\n{header}\n{bullet}"

        if not promoted_entries:
            if plan:
                plan.add_skip(
                    "promotion",
                    "model_entries_empty_after_filter",
                )
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
                    excerpt=_short_excerpt(
                        "; ".join(
                            entry["entry"]
                            for entry in promoted_entries
                        ),
                        _AUDIT_EXCERPT_LIMIT,
                    ),
                    detail={
                        "period": previous_period,
                        "entries": promoted_entries,
                    },
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
    except Exception as exc:
        if plan:
            plan.add_warning(f"Log promotion failed: {exc}")
        logger.warning("Log promotion failed: %s", exc)
        return 0


def decay_memory_strength(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
    period: str | None = None,
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> int:
    """Apply one use-based strength decay law and return the decayed count."""
    if not config.decay_after_days:
        return 0
    if not team_id:
        if plan:
            plan.add_skip("strength_decay", "unscoped_shared_pass")
        return 0

    if all_memories is None:
        try:
            all_memories = backend.get_all(
                agent_id=agent_id,
                limit=300,
                team_id=team_id,
            )
        except Exception as exc:
            logger.warning("Failed to load memories for decay: %s", exc)
            return 0

    current_period = period or period_key(config.rhythm)
    decayed = 0
    deleted = 0
    now = datetime.now(timezone.utc)

    for memory in all_memories:
        memory_id = _memory_id(memory)
        if _has_period_marker(
            memory,
            _STRENGTH_DECAY_PERIOD_KEY,
            current_period,
        ):
            if plan:
                plan.add_skip(
                    "strength_decay",
                    "already_applied_this_period",
                    memory_id=memory_id,
                    period=current_period,
                )
            continue
        if memory.category == "direction":
            continue
        after_days = config.decay_after_days
        try:
            reference_text = memory.last_accessed or memory.created_at
            reference = datetime.fromisoformat(reference_text)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        days_since = (now - reference).total_seconds() / 86400
        if days_since <= after_days:
            continue

        periods_elapsed = max(1.0, days_since / max(1, after_days))
        old_strength = max(
            0.0,
            min(1.0, float(memory.strength or 0.5)),
        )
        new_strength = max(
            0.0,
            old_strength * (0.5**periods_elapsed),
        )
        if abs(new_strength - old_strength) < 0.01:
            continue

        old_metadata = _metadata_subset(
            memory,
            ("strength", _STRENGTH_DECAY_PERIOD_KEY),
        )
        if new_strength < 0.1:
            metadata = {
                "deleted": True,
                "strength": new_strength,
                _STRENGTH_DECAY_PERIOD_KEY: current_period,
            }
            if plan:
                plan.add_operation(
                    DreamOperation(
                        kind="strength_decay",
                        status="planned" if dry_run else "applied",
                        memory_id=memory_id,
                        old_metadata=old_metadata,
                        new_metadata=metadata,
                        reason=f"unused_for_{int(days_since)}_days",
                        excerpt=_short_excerpt(memory.text),
                    )
                )
            if not dry_run:
                backend.delete(memory_id)
            deleted += 1
            continue

        metadata = {
            "strength": new_strength,
            _STRENGTH_DECAY_PERIOD_KEY: current_period,
        }
        if plan:
            plan.add_operation(
                DreamOperation(
                    kind="strength_decay",
                    status="planned" if dry_run else "applied",
                    memory_id=memory_id,
                    old_metadata=old_metadata,
                    new_metadata=metadata,
                    reason=f"unused_for_{int(days_since)}_days",
                    excerpt=_short_excerpt(memory.text),
                )
            )
        try:
            if not dry_run:
                backend.update_metadata(memory_id, metadata)
            _set_memory_metadata(memory, metadata)
            decayed += 1
        except Exception:
            pass

    if plan and deleted:
        plan.health["memories_deleted_by_strength_decay"] = deleted
    if decayed or deleted:
        logger.info(
            "Strength decay updated %d memories and deleted %d",
            decayed,
            deleted,
        )
    return decayed


def run_refinement_phase(
    agent: Any | None,
    *,
    dry_run: bool = False,
    plan: DreamPlan | None = None,
) -> dict[str, int]:
    """Drain the refinement queue before interpretive dream work."""
    summary = {
        "pending": 0,
        "edits": 0,
        "memory_deletes": 0,
        "queue_applied": 0,
    }
    if agent is None:
        if plan:
            plan.add_skip("refinement", "no_agent")
        return summary
    if dry_run:
        if plan:
            plan.add_skip("refinement", "dry_run")
        return summary
    try:
        from ..refinement import ChangeSetQueue, run_refinement

        config = getattr(agent.config, "refinement", None)
        queue = ChangeSetQueue(
            protected_data(agent.config.agent.workspace)
            / "change_queue.jsonl"
        )
        result = run_refinement(
            agent=agent,
            queue=queue,
            max_changes_per_pass=(
                config.max_changes_per_pass if config else 25
            ),
            max_docs_per_pass=(
                config.max_docs_per_pass if config else 15
            ),
            window_lines=config.window_lines if config else 20,
        )
        summary = {
            "pending": result.pending_seen,
            "edits": result.windows_applied,
            "memory_deletes": result.memory_deletes,
            "queue_applied": result.queue_marked_applied,
        }
        if plan:
            for call in result.llm_calls:
                plan.llm_calls.append(call)
            plan.add_operation(
                DreamOperation(
                    kind="refinement",
                    status="applied",
                    reason="drain_change_queue",
                    new_metadata=summary,
                    detail={
                        "docs_inspected": result.docs_inspected,
                        "files_rewritten": list(result.files_rewritten),
                        "per_doc_summaries": list(
                            result.per_doc_summaries
                        ),
                        "errors": list(result.errors),
                    },
                    excerpt=_short_excerpt(
                        "; ".join(result.per_doc_summaries)
                        or (
                            f"{result.windows_applied} edits / "
                            f"{result.pending_seen} pending"
                        ),
                        _AUDIT_EXCERPT_LIMIT,
                    ),
                )
            )
            for path in result.files_rewritten:
                plan.add_operation(
                    DreamOperation(
                        kind="refinement_file",
                        status="applied",
                        target=path,
                        reason="window_rewrite",
                    )
                )
            for error in result.errors:
                plan.add_warning(f"refinement: {error}")
        return summary
    except Exception as exc:
        if plan:
            plan.add_warning(f"Refinement phase failed: {exc}")
        logger.warning("Dream refinement phase failed: %s", exc)
        return summary


def _dream_status_path(workspace: Path) -> Path:
    return protected_data(workspace) / "dream_status.json"


def write_dream_status(
    workspace: Path,
    period: str,
    results_by_scope: dict[str, dict],
) -> dict[str, Any]:
    """Persist a compact health summary of the last agentic dream cycle."""
    scope_failures = {
        scope: summary.get("failures", [])
        for scope, summary in results_by_scope.items()
        if summary.get("failures")
    }
    completed_at = datetime.now(timezone.utc).isoformat()
    status = {
        "period": period,
        "completed_at": completed_at,
        "last_dream_at": completed_at,
        "scopes_run": len(results_by_scope),
        "scopes_with_failures": len(scope_failures),
        "failures": scope_failures,
    }
    from ..memory_lock import memory_write_lock

    with memory_write_lock():
        path = _dream_status_path(workspace)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(status, indent=2, sort_keys=True))
        except OSError as exc:
            logger.warning("Failed to write dream status: %s", exc)
    return status


def read_dream_status(workspace: Path) -> dict[str, Any] | None:
    """Read the last dream health summary when present."""
    path = _dream_status_path(workspace)
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read dream status: %s", exc)
    return None


def dream_health_note(workspace: Path) -> str:
    """Return a human-readable note about dream failures, or empty when healthy."""
    status = read_dream_status(workspace)
    if not status or not status.get("scopes_with_failures"):
        return ""
    lines = [
        f"The last dream (memory maintenance) cycle for period {status.get('period')} "
        f"had failures in {status['scopes_with_failures']} of "
        f"{status.get('scopes_run')} scopes. Memory promotion/review may be "
        "incomplete for those scopes; flag this to a controller if it persists.",
    ]
    for scope, failures in list(status.get("failures", {}).items())[:5]:
        for failure in failures[:2]:
            lines.append(f"- [{scope[:8]}] {failure}")
    return "\n".join(lines)
