"""Turn-based reflection for curated memory storage.

Instead of storing every turn pair in mem0 (noisy), reflection runs every N
turns during a conversation and extracts only what's worth keeping: important
facts, user preferences, and a daily log entry.

This replaces the old idle-timer approach with a turn-count trigger that
integrates naturally with the conversation state tracker.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..subagents.reflector import ReflectionResult, normalize_daily_log_entry
from .conversation_state import ConversationState
from .model import to_metadata
from .validator import MemoryRunContext, validate_memory_candidates

logger = logging.getLogger(__name__)


def write_daily_log(
    workspace: Path,
    entry_text: str,
    doc_store=None,
    agent_name: str = "",
) -> None:
    """Append a timestamped entry to today's daily log."""
    if not doc_store:
        logger.warning("write_daily_log called without a doc_store")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%H:%M")
    entry = f"- {ts} — {entry_text}\n"

    post_name = doc_store.daily_name(agent_name, today)
    initial_content = f"# Daily Log {today}\n\n{entry}"
    ok = doc_store.append_list_item(post_name, entry, initial_md=initial_content)
    if not ok:
        logger.warning(
            "Failed to write daily log to %s via %s",
            post_name,
            type(doc_store).__name__,
        )


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
        available_team_ids=set(available_team_ids or ([] if not team_id else [team_id])),
    )
    items, errors = validate_memory_candidates(
        _reflection_candidates(result),
        ctx,
        source=source,
    )
    for error in errors:
        logger.warning("Rejected reflected memory (%s): %s", error.reason, error.text[:80])

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
            )
            stored += 1
            logger.info("Reflection stored memory [%s]: %s", item.category, item.text[:80])
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

    if result.daily_log_entry:
        entry = normalize_daily_log_entry(result.daily_log_entry, run_mode="chat")
        write_daily_log(
            workspace,
            entry,
            doc_store=doc_store,
            agent_name=agent_id,
        )
        logger.info("Reflection logged to daily: %s", entry[:80])

    turn_count = conversation_state.turn_count if conversation_state else 0
    _save_reflected_turn(conversations_dir, conversation_id, turn_count)
