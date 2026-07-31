"""Post-run reflection for curated memory storage.

Reflection extracts durable facts and daily-log entries from a completed run
and stores them through validation.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from ..subagents.reflector import ReflectionResult, normalize_daily_log_entry
from .model import to_metadata
from .naming import log_entry_timestamp, period_key, period_log_title, store_rhythm
from .validator import MemoryRunContext, validate_memory_candidates

logger = logging.getLogger(__name__)


def write_log(
    workspace: Path,
    entry_text: str,
    doc_store=None,
    agent_name: str = "",
) -> None:
    """Append a timestamped entry to the current period's log.

    The period window (daily/weekly/biweekly) is determined by the doc store's
    configured ``rhythm`` — callers don't need to know the cadence.
    """
    if not doc_store:
        logger.warning("write_log called without a doc_store")
        return

    rhythm = store_rhythm(doc_store)
    period = period_key(rhythm)
    ts = log_entry_timestamp(rhythm)
    entry = f"- {ts} — {entry_text}\n"

    post_name = doc_store.log_name(agent_name, period)
    initial_content = f"# {period_log_title(rhythm)} {period}\n\n{entry}"
    ok = doc_store.append_list_item(post_name, entry, initial_md=initial_content)
    if not ok:
        logger.warning(
            "Failed to write daily log to %s via %s",
            post_name,
            type(doc_store).__name__,
        )


def validated_daily_log_entries(
    result: ReflectionResult,
    *,
    run_team_id: Optional[str] = None,
    available_team_ids: Optional[set[str]] = None,
) -> list[tuple[str, str]]:
    """Return validated ``(team_id, entry)`` pairs from structured reflection logs."""
    valid_ids = set(available_team_ids or ([] if not run_team_id else [run_team_id]))
    entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for daily_entry in getattr(result, "daily_log_entries", []):
        raw_team_id = str(getattr(daily_entry, "team_id", "") or "").strip()
        entry_text = str(getattr(daily_entry, "entry", "") or "").strip()
        if not entry_text:
            continue
        target_team_id = raw_team_id if raw_team_id in valid_ids else ""
        if not target_team_id and run_team_id:
            target_team_id = run_team_id
        if not target_team_id:
            logger.warning(
                "Dropped daily log entry with invalid team_id=%s: %s",
                raw_team_id or "(empty)",
                entry_text[:80],
            )
            continue
        key = (target_team_id, entry_text)
        if key in seen:
            continue
        seen.add(key)
        entries.append(key)
    return entries


# Hard cap on stored memories per reflection. The reflector prompt asks for
# 0-3 candidates; anything past this is over-extraction, keep the strongest.
MAX_REFLECTION_MEMORIES = 5

# Token-overlap threshold above which two memory texts are treated as
# restatements of the same fact.
NEAR_DUPLICATE_JACCARD = 0.6

_WORD_RE = re.compile(r"[a-z0-9]+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _memory_tokens(text: str) -> set[str]:
    normalized = _MD_LINK_RE.sub(r"\1", text.lower())
    return set(_WORD_RE.findall(normalized))


def is_near_duplicate_text(a: str, b: str) -> bool:
    """True when two memory texts are near-restatements of each other."""
    ta, tb = _memory_tokens(a), _memory_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / len(ta | tb)
    return overlap >= NEAR_DUPLICATE_JACCARD


def collapse_near_duplicates(items: list) -> list:
    """Collapse near-duplicate candidates within one reflection batch.

    When two candidates restate the same fact, keep the one with more content
    (higher strength wins ties on the side of explicit human guidance).
    """
    kept: list = []
    for item in items:
        replaced = False
        for i, existing in enumerate(kept):
            if not is_near_duplicate_text(item.text, existing.text):
                continue
            better = (item.strength, len(item.text)) > (
                existing.strength,
                len(existing.text),
            )
            if better:
                # Carry over any supersede instructions from the dropped twin.
                item.supersedes = list(
                    dict.fromkeys([*existing.supersedes, *item.supersedes])
                )
                kept[i] = item
            else:
                existing.supersedes = list(
                    dict.fromkeys([*existing.supersedes, *item.supersedes])
                )
            replaced = True
            logger.info(
                "Collapsed near-duplicate reflection candidates: %r ~ %r",
                item.text[:60],
                existing.text[:60],
            )
            break
        if not replaced:
            kept.append(item)
    return kept


def _existing_near_duplicate(item, memory_backend, agent_id: str) -> Optional[str]:
    """Return the text of a stored memory that already covers ``item``, if any.

    ``supersedes`` wins over the duplicate check: if the candidate explicitly
    retires memories, it is an update, not a duplicate.
    """
    if item.supersedes:
        return None
    try:
        results = memory_backend.search(
            query=item.text,
            agent_id=agent_id,
            limit=3,
            scope="global",
        )
    except Exception as e:
        logger.debug("Cross-run duplicate check failed: %s", e)
        return None
    for result in results or []:
        if is_near_duplicate_text(item.text, result.text):
            return result.text
    return None


def _reflection_candidates(result: ReflectionResult) -> list[dict]:
    candidates = [
        candidate
        for candidate in result.facts_to_store
        if str(candidate.get("category", "fact")) != "episode"
    ]
    for preference in result.user_preferences:
        if isinstance(preference, str) and preference.strip():
            candidates.append(
                {
                    "text": preference.strip(),
                    "subject_type": "user",
                    "category": "preference",
                    "basis": "observed",
                    "stability": "stable",
                    "team_ids": [],
                    "strength": 0.5,
                }
            )
    return candidates


def store_reflection_memories(
    result: ReflectionResult,
    memory_backend,
    *,
    agent_id: str,
    user_id: Optional[str],
    run_id: str,
    conversation_id: str = "",
    team_id: Optional[str] = None,
    available_team_ids: Optional[set[str]] = None,
    org_id: str = "",
    mode: str = "chat",
    event_type: str = "",
    source: str = "",
) -> int:
    """Validate and store durable memories extracted by reflection."""
    source = source or f"reflection:{run_id or conversation_id}"
    ctx = MemoryRunContext(
        agent_id=agent_id,
        user_id=user_id or "",
        org_id=org_id,
        conversation_id=conversation_id,
        run_id=run_id,
        mode=mode,
        event_type=event_type,
        team_id=team_id or "",
        available_team_ids=set(
            available_team_ids or ([] if not team_id else [team_id])
        ),
    )
    items, errors = validate_memory_candidates(
        _reflection_candidates(result),
        ctx,
        source=source,
    )
    for error in errors:
        logger.warning(
            "Rejected reflected memory (%s): %s", error.reason, error.text[:80]
        )

    items = collapse_near_duplicates(items)
    if len(items) > MAX_REFLECTION_MEMORIES:
        logger.info(
            "Reflection over-extracted %d candidates; keeping the %d strongest",
            len(items),
            MAX_REFLECTION_MEMORIES,
        )
        items = sorted(
            items, key=lambda i: (i.strength, len(i.text)), reverse=True
        )[:MAX_REFLECTION_MEMORIES]

    stored = 0
    for item in items:
        duplicate_of = _existing_near_duplicate(item, memory_backend, agent_id)
        if duplicate_of:
            logger.info(
                "Skipped reflected memory already covered by stored memory: %s ~ %s",
                item.text[:60],
                duplicate_of[:60],
            )
            continue
        try:
            memory_backend.add(
                item.text,
                agent_id=agent_id,
                user_id=user_id,
                run_id=run_id or conversation_id,
                metadata=to_metadata(item),
                team_id=item.team_ids[0] if item.team_ids else None,
                infer=False,
            )
            stored += 1
            logger.info(
                "Reflection stored memory [%s]: %s", item.category, item.text[:80]
            )
        except Exception as e:
            logger.warning("Failed to store reflected memory: %s", e)
            continue
        for superseded_id in item.supersedes:
            try:
                memory_backend.delete(superseded_id)
                logger.info(
                    "Reflection retired superseded memory %s (replaced by: %s)",
                    superseded_id,
                    item.text[:80],
                )
            except Exception as e:
                logger.warning(
                    "Failed to retire superseded memory %s: %s", superseded_id, e
                )
    return stored


def apply_reflection(
    result: ReflectionResult,
    memory_backend,
    agent_id: str,
    user_id: Optional[str],
    conversation_id: str,
    workspace: Path,
    conversations_dir: Path,
    doc_store=None,
    team_id: Optional[str] = None,
    available_team_ids: Optional[set[str]] = None,
    org_id: str = "",
    mode: str = "chat",
    event_type: str = "",
) -> None:
    """Apply reflection results: store facts, update user model, write daily log."""
    store_reflection_memories(
        result,
        memory_backend,
        agent_id=agent_id,
        user_id=user_id,
        run_id=conversation_id,
        conversation_id=conversation_id,
        team_id=team_id,
        available_team_ids=available_team_ids,
        org_id=org_id,
        mode=mode,
        event_type=event_type,
        source=f"reflection:{conversation_id}",
    )

    if result.user_preferences and user_id:
        from .user_model import append_to_user_model

        try:
            append_to_user_model(
                workspace,
                user_id,
                "Preferences",
                result.user_preferences,
                doc_store=doc_store,
            )
        except Exception as e:
            logger.warning("Failed to update user model: %s", e)

    daily_entries = validated_daily_log_entries(
        result,
        run_team_id=team_id,
        available_team_ids={team_id} if team_id else set(),
    )
    for _target_team_id, raw_entry in daily_entries:
        entry = normalize_daily_log_entry(
            raw_entry,
            run_mode=mode,
            event_type=event_type,
        )
        write_log(
            workspace,
            entry,
            doc_store=doc_store,
            agent_name=agent_id,
        )
        logger.info("Reflection logged: %s", entry[:80])
