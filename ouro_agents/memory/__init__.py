from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, List, Optional

from pydantic import BaseModel

if TYPE_CHECKING:
    from .conversation_state import ConversationState


class DocStore(Protocol):
    """Interface for document stores (Ouro-backed or local filesystem)."""

    def read(self, name: str) -> str: ...
    def write(self, name: str, content_md: str) -> bool: ...
    def append(self, name: str, markdown: str) -> bool: ...
    def exists(self, name: str) -> bool: ...
    def comment(self, name: str, content_md: str) -> bool: ...
    def read_comments(self, name: str) -> list[dict]: ...
    def search(self, query: str) -> list[dict]: ...
    def is_owner(self, name: str) -> bool: ...
    def memory_name(self, agent_name: str | None = None) -> str: ...
    def daily_name(self, agent_name: str | None, day: str) -> str: ...


class MemoryResult(BaseModel):
    text: str
    score: float = 0.0
    category: str = "general"
    importance: float = 0.5
    created_at: str = ""
    source: str = ""
    last_accessed: str = ""


class MemoryBackend(Protocol):
    """Interface all memory backends must implement."""

    def search(self, query: str, agent_id: str,
               user_id: Optional[str] = None, limit: int = 10,
               team_id: Optional[str] = None,
               scope: str = "team") -> List[MemoryResult]:
        ...

    def add(self, content: str | list[dict], agent_id: str,
            user_id: Optional[str] = None, run_id: Optional[str] = None,
            metadata: Optional[dict] = None,
            team_id: Optional[str] = None) -> None:
        ...

    def get_all(self, agent_id: str, user_id: Optional[str] = None,
                limit: int = 100,
                team_id: Optional[str] = None) -> List[MemoryResult]:
        ...

    def update_metadata(self, memory_id: str, metadata: dict) -> None:
        ...

    def reset_usage(self) -> None:
        ...

    def usage_ledger(self) -> list[tuple[str, Any]]:
        ...


CATEGORY_LABELS = {
    "fact": "Facts",
    "preference": "Preferences",
    "learning": "Learnings",
    "decision": "Decisions",
    "observation": "Observations",
    "general": "Context",
}


def format_memories(
    memories: List[MemoryResult], min_score: float = 0.5
) -> str:
    """Format memory results grouped by category, filtering low-relevance ones."""
    relevant = [r for r in memories if r.score >= min_score]
    if not relevant:
        return ""

    grouped: dict[str, list[MemoryResult]] = {}
    for m in relevant:
        grouped.setdefault(m.category, []).append(m)

    lines: list[str] = []
    for cat in ["fact", "decision", "learning", "preference", "observation", "general"]:
        items = grouped.get(cat, [])
        if not items:
            continue
        label = CATEGORY_LABELS.get(cat, cat.title())
        lines.append(f"**{label}:**")
        for item in items:
            lines.append(f"- {item.text}")
    return "\n".join(lines)


def expand_query(task: str, state: ConversationState) -> str:
    """Build a conversation-aware memory search query."""
    parts: list[str] = []
    if state.current_topic:
        parts.append(state.current_topic)
    if state.active_goals:
        parts.append("; ".join(state.active_goals))
    parts.append(task)
    return " ".join(parts)


def create_memory_backend(config, usage_tracker=None) -> MemoryBackend:
    if config.provider == "mem0":
        from .mem0 import Mem0Backend
        return Mem0Backend(config, usage_tracker=usage_tracker)
    raise ValueError(f"Unknown memory provider: {config.provider}")
