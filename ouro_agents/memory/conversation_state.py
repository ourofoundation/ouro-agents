"""Conversation state tracker for chat mode.

Maintains a compact structured summary of the conversation that updates after
every turn.  The state provides orientation to the LLM ("you are discussing X,
you decided Y"), enables conversation-aware memory retrieval, and keeps a
rolling summary of the full conversation so older turns don't get lost.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_UPDATE_PROMPT = """\
You are a conversation state tracker. Given the previous state and the latest
exchange, output an updated JSON state object. Keep each field concise.

Rules:
- current_topic: The main subject of the conversation RIGHT NOW (may shift).
- active_goals: What the user currently wants done (remove completed goals).
- decisions_made: Important choices or completed actions (append new ones, keep recent 5, drop old).
- open_questions: Unresolved questions or ambiguities (remove answered ones).
- key_entities: People, projects, tools, or concepts central to the conversation.
- key_moments: Important moments worth preserving — user corrections, key decisions, surprising
  information, task completions. Keep the 8 most important across the whole conversation.
  Format: brief description of what happened. Drop trivial exchanges.
- conversation_summary: A rolling 2-4 sentence summary of the ENTIRE conversation so far.
  Update it to incorporate the latest exchange. This is the agent's memory of everything
  that happened before the recent messages window. Be factual and concise.
- turn_count: Increment by 1.

Output ONLY valid JSON matching this schema, no markdown fences, no explanation:
{
  "current_topic": "string",
  "active_goals": ["string"],
  "decisions_made": ["string"],
  "open_questions": ["string"],
  "key_entities": ["string"],
  "key_moments": ["string"],
  "conversation_summary": "string",
  "turn_count": int
}"""


@dataclass
class ConversationState:
    current_topic: str = ""
    active_goals: list[str] = field(default_factory=list)
    decisions_made: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    key_entities: list[str] = field(default_factory=list)
    key_moments: list[str] = field(default_factory=list)
    conversation_summary: str = ""
    turn_count: int = 0

    def to_dict(self) -> dict:
        return {
            "current_topic": self.current_topic,
            "active_goals": self.active_goals,
            "decisions_made": self.decisions_made,
            "open_questions": self.open_questions,
            "key_entities": self.key_entities,
            "key_moments": self.key_moments,
            "conversation_summary": self.conversation_summary,
            "turn_count": self.turn_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationState":
        return cls(
            current_topic=data.get("current_topic", ""),
            active_goals=data.get("active_goals", []),
            decisions_made=data.get("decisions_made", []),
            open_questions=data.get("open_questions", []),
            key_entities=data.get("key_entities", []),
            key_moments=data.get("key_moments", []),
            conversation_summary=data.get("conversation_summary", ""),
            turn_count=data.get("turn_count", 0),
        )

    def format_for_prompt(self) -> str:
        """Render the state as a compact orientation section for the system prompt."""
        if not self.current_topic:
            return ""

        lines = [f"Topic: {self.current_topic}"]

        if self.active_goals:
            lines.append(f"Active goals: {'; '.join(self.active_goals)}")

        if self.decisions_made:
            recent = self.decisions_made[-5:]
            lines.append(f"Decided: {'; '.join(recent)}")

        if self.open_questions:
            lines.append(f"Open questions: {'; '.join(self.open_questions)}")

        if self.key_entities:
            lines.append(f"Key entities: {', '.join(self.key_entities)}")

        if self.key_moments:
            lines.append("Key moments:")
            for moment in self.key_moments[-8:]:
                lines.append(f"  - {moment}")

        if self.conversation_summary:
            lines.append(f"\nConversation so far: {self.conversation_summary}")

        lines.append(f"Turn: {self.turn_count}")
        return "\n".join(lines)


def _state_path(conversations_dir: Path, conversation_id: str) -> Path:
    return conversations_dir / f"{conversation_id}.state.json"


def load_state(
    conversations_dir: Path, conversation_id: str
) -> Optional[ConversationState]:
    """Load conversation state from disk.  Returns None if no state exists."""
    path = _state_path(conversations_dir, conversation_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return ConversationState.from_dict(data)
    except Exception as e:
        logger.warning("Failed to load conversation state from %s: %s", path, e)
        return None


def save_state(
    conversations_dir: Path, conversation_id: str, state: ConversationState
) -> None:
    """Persist conversation state to disk."""
    path = _state_path(conversations_dir, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2))


def update_state(
    previous_state: Optional[ConversationState],
    user_message: str,
    assistant_response: str,
    model,
) -> ConversationState:
    """Update the conversation state using a cheap LLM call."""
    prev_json = json.dumps(
        previous_state.to_dict() if previous_state else ConversationState().to_dict(),
        indent=2,
    )

    user_msg = user_message[:800]
    assistant_msg = assistant_response[:500]

    user_content = (
        f"Previous state:\n{prev_json}\n\n"
        f"User message:\n{user_msg}\n\n"
        f"Assistant response:\n{assistant_msg}"
    )

    try:
        result = model(
            [
                {"role": "system", "content": STATE_UPDATE_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return ConversationState.from_dict(data)
    except Exception as e:
        logger.warning("Conversation state update failed: %s", e)
        if previous_state:
            previous_state.turn_count += 1
            return previous_state
        return ConversationState(
            current_topic=user_message[:100],
            turn_count=1,
        )
