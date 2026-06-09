"""Turn-based reflection for curated memory storage.

Instead of storing every turn pair in mem0 (noisy), reflection runs every N
turns during a conversation and extracts only what's worth keeping: important
facts, user preferences, and a daily log entry.

This replaces the old idle-timer approach with a turn-count trigger that
integrates naturally with the conversation state tracker.
"""

import logging
from pathlib import Path
from typing import Optional

from ..subagents.reflector import ReflectionResult, normalize_daily_log_entry
from .conversation_state import ConversationState
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


def should_reflect(
    conversation_state: Optional[ConversationState],
    reflection_interval: int = 10,
    last_reflected_turn: int = 0,
) -> bool:
    """Check if enough turns have passed to trigger reflection.

    Returns True if the conversation has advanced by at least
    `reflection_interval` turns since the last reflection.
    """
    if not conversation_state:
        return False
    if conversation_state.turn_count < 1:
        return False
    turns_since = conversation_state.turn_count - last_reflected_turn
    return turns_since >= reflection_interval


def _load_reflected_turn(conversations_dir: Path, conversation_id: str) -> int:
    """Load the turn count at which the last reflection occurred."""
    marker = conversations_dir / f"{conversation_id}.reflected"
    if not marker.exists():
        return 0
    try:
        content = marker.read_text().strip()
        return int(content) if content else 0
    except (ValueError, OSError):
        return 0


def _save_reflected_turn(
    conversations_dir: Path, conversation_id: str, turn_count: int
) -> None:
    """Record the turn count at which reflection occurred."""
    marker = conversations_dir / f"{conversation_id}.reflected"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(turn_count))


def _reflection_candidates(result: ReflectionResult) -> list[dict]:
    candidates = list(result.facts_to_store)
    for preference in result.user_preferences:
        if isinstance(preference, str) and preference.strip():
            candidates.append(
                {
                    "text": preference.strip(),
                    "subject_type": "user",
                    "category": "preference",
                    "team_ids": [],
                    "importance": 0.6,
                    "confidence": 0.8,
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

    stored = 0
    for item in items:
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
    return stored


def should_reflect_for_conversation(
    conversations_dir: Path,
    conversation_id: str,
    conversation_state: Optional[ConversationState],
    reflection_interval: int = 10,
) -> bool:
    """Full check: load last reflected turn and compare to current state."""
    last_turn = _load_reflected_turn(conversations_dir, conversation_id)
    return should_reflect(conversation_state, reflection_interval, last_turn)


def apply_reflection(
    result: ReflectionResult,
    memory_backend,
    agent_id: str,
    user_id: Optional[str],
    conversation_id: str,
    workspace: Path,
    conversations_dir: Path,
    conversation_state: Optional[ConversationState] = None,
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

    turn_count = conversation_state.turn_count if conversation_state else 0
    _save_reflected_turn(conversations_dir, conversation_id, turn_count)
