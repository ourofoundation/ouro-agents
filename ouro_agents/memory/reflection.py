"""Turn-based reflection for curated memory storage.

Instead of storing every turn pair in mem0 (noisy), reflection runs every N
turns during a conversation and extracts only what's worth keeping: important
facts, user preferences, and a daily log entry.

This replaces the old idle-timer approach with a turn-count trigger that
integrates naturally with the conversation state tracker.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..subagents.reflector import ReflectionResult, normalize_daily_log_entry
from .conversation_state import ConversationState

logger = logging.getLogger(__name__)


_LIST_ITEM_RE = re.compile(r"^\s*[-*] ")


def _append_markdown_list_item(existing: str, addition: str) -> str:
    """Merge a markdown list item into the current trailing list."""
    existing = existing.rstrip()
    addition = addition.strip()
    if not existing:
        return addition
    if not addition:
        return existing

    separator = "\n"
    if not _LIST_ITEM_RE.match(addition):
        separator = "\n\n"

    return f"{existing}{separator}{addition}"


def write_daily_log(
    workspace: Path,
    entry_text: str,
    doc_store=None,
    agent_name: str = "",
) -> None:
    """Append a timestamped entry to today's daily log via doc_store."""
    today = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%H:%M")
    entry = f"- {ts} — {entry_text}\n"
    post_name = f"DAILY:{agent_name}:{today}"

    if not doc_store:
        logger.warning("write_daily_log called without doc_store")
        return

    if doc_store.exists(post_name):
        current = doc_store.read(post_name)
        ok = doc_store.write(post_name, _append_markdown_list_item(current, entry))
    else:
        ok = doc_store.write(post_name, f"# Daily Log {today}\n\n{entry}")

    if not ok:
        logger.warning("Failed to write daily log to %s", post_name)


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
) -> None:
    """Apply reflection results: store facts, update user model, write daily log."""
    # TODO: Support reflector-driven updates/merges for existing memories once
    # memory IDs and end-to-end update semantics are exposed to the reflector.
    for fact in result.facts_to_store:
        text = fact.get("text", "")
        if not text:
            continue
        try:
            metadata = {
                "category": fact.get("category", "fact"),
                "importance": fact.get("importance", 0.5),
                "source": f"reflection:{conversation_id}",
            }
            asset_refs = fact.get("asset_refs", [])
            if asset_refs:
                metadata["asset_refs"] = ",".join(asset_refs)
            memory_backend.add(
                text,
                agent_id=agent_id,
                user_id=user_id,
                run_id=conversation_id,
                metadata=metadata,
            )
            logger.info(
                "Reflection stored fact [%s]: %s", fact.get("category"), text[:80]
            )
        except Exception as e:
            logger.warning("Failed to store reflected fact: %s", e)

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
