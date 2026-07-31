from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Protocol

from pydantic import BaseModel, Field

from .model import MemoryItem
from .ouro_docs import DocStore


__all__ = [
    "DocStore",
    "MemoryResult",
    "MemoryBackend",
    "MemoryItem",
    "create_memory_backend",
]


class MemoryResult(BaseModel):
    id: str = ""
    text: str
    score: float = 0.0
    category: str = "fact"
    strength: float = 0.5
    created_at: str = ""
    source: str = ""
    last_accessed: str = ""
    team_id: str = ""
    subject_type: str = "general"
    subject_id: str = ""
    basis: str = "inferred"
    stability: str = "stable"
    team_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    user_id: str = ""
    last_verified: str = ""
    verification_hint: str = ""
    content_hash: str = ""
    schema_version: int = 3
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryBackend(Protocol):
    """Interface all memory backends must implement."""

    def search(
        self,
        query: str,
        agent_id: str,
        user_id: Optional[str] = None,
        limit: int = 10,
        team_id: Optional[str] = None,
        scope: str = "team",
        category: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        asset_id: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[MemoryResult]: ...

    def add(
        self,
        content: str | list[dict],
        agent_id: str,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        team_id: Optional[str] = None,
        infer: bool = True,
    ) -> None: ...

    def get_all(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        limit: int = 100,
        team_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        asset_id: Optional[str] = None,
        category: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[MemoryResult]: ...

    def update_metadata(self, memory_id: str, metadata: dict) -> None: ...

    def update_text(self, memory_id: str, text: str) -> None:
        """Replace a memory's text in place, recomputing its content hash."""
        ...

    def get(self, memory_id: str) -> Optional[MemoryResult]:
        """Fetch a single memory by id, or None if it does not exist."""
        ...

    def delete(self, memory_id: str) -> None:
        """Permanently remove a memory by id. Best-effort; logs and swallows errors."""
        ...

    def find_by_asset(
        self,
        asset_id: str,
        agent_id: str,
        team_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[MemoryResult]:
        """Find every memory whose asset_ids includes ``asset_id``."""
        ...

    def reset_usage(self) -> None: ...

    def usage_ledger(self) -> list[tuple[str, Any]]: ...


def create_memory_backend(config, usage_tracker=None) -> MemoryBackend:
    if config.provider == "mem0":
        from .mem0 import Mem0Backend

        return Mem0Backend(config, usage_tracker=usage_tracker)
    raise ValueError(f"Unknown memory provider: {config.provider}")
