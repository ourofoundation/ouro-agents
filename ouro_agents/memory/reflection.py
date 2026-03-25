"""Turn-based reflection for curated memory storage.

Instead of storing every turn pair in mem0 (noisy), reflection runs every N
turns during a conversation and extracts only what's worth keeping: important
facts, user preferences, and a daily log entry.

This replaces the old idle-timer approach with a turn-count trigger that
integrates naturally with the conversation state tracker.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .conversation_state import ConversationState

logger = logging.getLogger(__name__)


def write_daily_log(workspace: Path, entry_text: str) -> None:
    """Append a timestamped entry to today's daily log file."""
    today = datetime.now().strftime("%Y-%m-%d")
    daily_path = workspace / "memory" / "daily" / f"{today}.md"
    daily_path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%H:%M")
    entry = f"- {ts} — {entry_text}\n"

    if not daily_path.exists():
        daily_path.write_text(f"# {today}\n\n{entry}")
    else:
        with open(daily_path, "a") as f:
            f.write(entry)

REFLECTION_PROMPT = """\
You are a memory curator. Given a conversation state and the last few messages,
extract what is worth remembering long-term. Be selective — only include things
that would be useful in FUTURE conversations.

Output ONLY valid JSON matching this schema, no markdown fences:
{
  "facts_to_store": [{"text": "string", "category": "fact"|"decision"|"learning"|"observation", "importance": 0.0-1.0}],
  "user_preferences": ["string"],
  "daily_log_entry": "string"
}

Rules:
- facts_to_store: Important facts, decisions, or knowledge gained. NOT conversation mechanics.
  Assign a category and importance (0.3=minor, 0.5=normal, 0.7=significant, 0.9=critical).
- user_preferences: Communication style, interests, or workflow patterns observed.
  Only include clear, repeated signals.
- daily_log_entry: One-line summary of what was accomplished.
- If nothing is worth remembering, return empty lists and an empty string.
- Be concise. Each fact/preference should be one sentence.
"""


@dataclass
class ReflectionResult:
    facts_to_store: list[dict] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)
    daily_log_entry: str = ""


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


def _load_recent_turns(
    conversations_dir: Path, conversation_id: str, limit: int = 20
) -> list[dict]:
    """Load the most recent turns from a conversation JSONL."""
    jsonl_path = conversations_dir / f"{conversation_id}.jsonl"
    if not jsonl_path.exists():
        return []
    lines = jsonl_path.read_text().strip().split("\n")
    turns = []
    for line in lines[-limit:]:
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return turns


def parse_reflection_result(text: str) -> ReflectionResult:
    """Parse an LLM response string into a ReflectionResult.

    Handles markdown fences, and normalizes facts that come as plain strings
    into the expected dict format. Returns an empty result on parse failure.
    """
    try:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)

        facts_raw = data.get("facts_to_store", [])
        facts = []
        for f in facts_raw:
            if isinstance(f, str):
                facts.append({"text": f, "category": "fact", "importance": 0.5})
            elif isinstance(f, dict):
                facts.append({
                    "text": f.get("text", ""),
                    "category": f.get("category", "fact"),
                    "importance": f.get("importance", 0.5),
                })

        return ReflectionResult(
            facts_to_store=facts,
            user_preferences=data.get("user_preferences", []),
            daily_log_entry=data.get("daily_log_entry", ""),
        )
    except Exception as e:
        logger.warning("Failed to parse reflection result: %s", e)
        return ReflectionResult()


def reflect(
    conversation_state: Optional[ConversationState],
    conversations_dir: Path,
    conversation_id: str,
    model,
) -> ReflectionResult:
    """Run the reflection LLM call and return structured results."""
    state_json = json.dumps(
        conversation_state.to_dict() if conversation_state else {},
        indent=2,
    )
    turns = _load_recent_turns(conversations_dir, conversation_id)
    turns_text = "\n".join(
        f"{t.get('role', '?')}: {str(t.get('content', ''))[:300]}"
        for t in turns
    )

    user_content = (
        f"Conversation state:\n{state_json}\n\n"
        f"Recent messages:\n{turns_text}"
    )

    try:
        result = model(
            [
                {"role": "system", "content": REFLECTION_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        return parse_reflection_result(text)
    except Exception as e:
        logger.warning("Reflection LLM call failed: %s", e)
        return ReflectionResult()


def apply_reflection(
    result: ReflectionResult,
    memory_backend,
    agent_id: str,
    user_id: Optional[str],
    conversation_id: str,
    workspace: Path,
    conversations_dir: Path,
    conversation_state: Optional[ConversationState] = None,
) -> None:
    """Apply reflection results: store facts, update user model, write daily log."""
    for fact in result.facts_to_store:
        text = fact.get("text", "")
        if not text:
            continue
        try:
            memory_backend.add(
                text,
                agent_id=agent_id,
                user_id=user_id,
                run_id=conversation_id,
                metadata={
                    "category": fact.get("category", "fact"),
                    "importance": fact.get("importance", 0.5),
                    "source": f"reflection:{conversation_id}",
                },
            )
            logger.info("Reflection stored fact [%s]: %s", fact.get("category"), text[:80])
        except Exception as e:
            logger.warning("Failed to store reflected fact: %s", e)

    if result.user_preferences and user_id:
        from .user_model import append_to_user_model
        try:
            append_to_user_model(
                workspace, user_id, "Preferences", result.user_preferences
            )
        except Exception as e:
            logger.warning("Failed to update user model: %s", e)

    if result.daily_log_entry:
        write_daily_log(workspace, result.daily_log_entry)
        logger.info("Reflection logged to daily: %s", result.daily_log_entry[:80])

    turn_count = conversation_state.turn_count if conversation_state else 0
    _save_reflected_turn(conversations_dir, conversation_id, turn_count)
