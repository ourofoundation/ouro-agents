"""Typed change-set queue for the refinement subsystem.

Producers append ``ChangeEntry`` rows describing things the agent's learnings
should react to (corrections from a user, an updated SOUL.md, etc.). The
refinement runner drains pending entries on a schedule, applies edits, and
marks them as applied (preserved in-file as an audit trail).

Persistence: a single JSONL file in the agent's workspace, written atomically.
Same shape as ``TaskStore`` in ``scheduler.py``.

Note: ``asset_deleted`` is intentionally NOT a recognized ``ChangeKind``.
Asset deletion is deterministic and handled by ``ouro_agents.cleanup``
without ever entering this queue.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from ..uuid_v7 import uuid7_str

logger = logging.getLogger(__name__)


class ChangeKind(str, Enum):
    """Kinds of changes the refiner knows how to apply.

    All entries described here are interpretive — they require an LLM to
    decide what existing learnings should be revised. Deterministic operations
    like asset deletion live in ``ouro_agents.cleanup``.
    """

    CORRECTION = "correction"
    GUIDANCE_UPDATED = "guidance_updated"
    ASSET_UPDATED = "asset_updated"


class ChangeEntry(BaseModel):
    id: str = Field(default_factory=uuid7_str)
    kind: ChangeKind
    subject_id: str
    subject_type: str = ""
    team_id: Optional[str] = None
    org_id: Optional[str] = None
    actor_user_id: Optional[str] = None
    occurred_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    payload: dict = Field(default_factory=dict)
    applied_at: Optional[str] = None
    applied_summary: str = ""

    def is_pending(self) -> bool:
        return self.applied_at is None

    def dedupe_key(self) -> tuple[str, str]:
        return (self.kind.value, self.subject_id)


class ChangeSetQueue:
    """Append-only JSONL with atomic full-file rewrites for state changes."""

    def __init__(self, path: Path):
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    # -- Read ---------------------------------------------------------------

    def load(self) -> list[ChangeEntry]:
        if not self._path.exists():
            return []
        entries: list[ChangeEntry] = []
        try:
            text = self._path.read_text()
        except OSError as exc:
            logger.warning("Failed to read change queue %s: %s", self._path, exc)
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(ChangeEntry.model_validate_json(line))
            except ValidationError as exc:
                logger.warning("Discarding malformed change-queue row: %s", exc)
        return entries

    def pending(self, limit: Optional[int] = None) -> list[ChangeEntry]:
        rows = [e for e in self.load() if e.is_pending()]
        rows.sort(key=lambda e: e.occurred_at)
        if limit is not None:
            rows = rows[:limit]
        return rows

    def stats(self) -> dict:
        pending = self.pending()
        oldest = pending[0].occurred_at if pending else None
        return {
            "pending": len(pending),
            "oldest": oldest,
            "total": len(self.load()),
        }

    # -- Write --------------------------------------------------------------

    def enqueue(self, entry: ChangeEntry) -> bool:
        """Append ``entry`` if no equivalent pending row exists.

        Returns ``True`` when the row was written, ``False`` when an unapplied
        entry with the same ``(kind, subject_id)`` already exists.
        """
        existing = self.load()
        for row in existing:
            if row.is_pending() and row.dedupe_key() == entry.dedupe_key():
                return False
        existing.append(entry)
        self._save(existing)
        return True

    def mark_applied(self, ids: list[str], summary: str = "") -> int:
        if not ids:
            return 0
        id_set = set(ids)
        rows = self.load()
        ts = datetime.now(timezone.utc).isoformat()
        updated = 0
        for row in rows:
            if row.id in id_set and row.applied_at is None:
                row.applied_at = ts
                if summary:
                    row.applied_summary = summary
                updated += 1
        if updated:
            self._save(rows)
        return updated

    def prune_applied(self, before: Optional[datetime] = None) -> int:
        """Drop applied rows older than ``before`` (default: keep last 30 days)."""
        cutoff = before or datetime.now(timezone.utc)
        kept: list[ChangeEntry] = []
        dropped = 0
        for row in self.load():
            if row.applied_at:
                try:
                    applied_dt = datetime.fromisoformat(row.applied_at)
                except ValueError:
                    kept.append(row)
                    continue
                if applied_dt.tzinfo is None:
                    applied_dt = applied_dt.replace(tzinfo=timezone.utc)
                if applied_dt < cutoff:
                    dropped += 1
                    continue
            kept.append(row)
        if dropped:
            self._save(kept)
        return dropped

    # -- Internals ----------------------------------------------------------

    def _save(self, entries: list[ChangeEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(e.model_dump_json() for e in entries)
        if body:
            body += "\n"
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


__all__ = ["ChangeEntry", "ChangeKind", "ChangeSetQueue"]
