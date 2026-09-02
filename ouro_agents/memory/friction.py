"""Durable queue for process friction discovered during agent runs."""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..memory_lock import memory_write_lock
from ..tools.workspace_paths import protected_data
from ..uuid_v7 import uuid7_str

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FrictionKind(str, Enum):
    SKILL_MISLED = "skill_misled"
    WASTED_STEPS = "wasted_steps"
    USER_CORRECTION = "user_correction"
    REPEATED_WORK = "repeated_work"
    TOOL_FAILURE = "tool_failure"
    INSTRUCTION_CONFLICT = "instruction_conflict"


class FrictionSeverity(str, Enum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


class FrictionStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"


class FrictionEntry(BaseModel):
    """One observed process problem and its eventual disposition."""

    id: str = Field(default_factory=uuid7_str)
    kind: FrictionKind
    skill: Optional[str] = None
    evidence: str
    severity: FrictionSeverity = FrictionSeverity.MED
    run_id: str = ""
    mode: str = ""
    team_id: Optional[str] = None
    ts: str = Field(default_factory=_utc_now)
    status: FrictionStatus = FrictionStatus.PENDING
    resolved_at: Optional[str] = None
    dream_run_id: Optional[str] = None
    disposition: str = ""
    note: str = ""

    @field_validator("skill", "team_id", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("evidence", "run_id", "mode", mode="before")
    @classmethod
    def _normalize_text(cls, value):
        return " ".join(str(value or "").split())

    @field_validator("evidence")
    @classmethod
    def _require_evidence(cls, value: str) -> str:
        if not value:
            raise ValueError("evidence is required")
        return value

    def is_pending(self) -> bool:
        return self.status == FrictionStatus.PENDING

    def dedupe_key(self) -> tuple[str, str, str, str]:
        return (
            self.run_id,
            self.kind.value,
            (self.skill or "").casefold(),
            self.evidence.casefold(),
        )


class FrictionQueue:
    """JSONL queue that preserves resolved rows as an audit trail."""

    def __init__(self, path: Path):
        self._path = Path(path)

    @classmethod
    def for_workspace(cls, workspace: Path | str) -> "FrictionQueue":
        return cls(protected_data(workspace) / "friction.jsonl")

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[FrictionEntry]:
        if not self._path.exists():
            return []
        try:
            text = self._path.read_text()
        except OSError as exc:
            logger.warning("Failed to read friction queue %s: %s", self._path, exc)
            return []

        entries: list[FrictionEntry] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(FrictionEntry.model_validate_json(line))
            except (ValidationError, ValueError) as exc:
                logger.warning(
                    "Discarding malformed friction row %s:%d: %s",
                    self._path,
                    line_number,
                    exc,
                )
        return entries

    def pending(self, limit: Optional[int] = None) -> list[FrictionEntry]:
        rows = [entry for entry in self.load() if entry.is_pending()]
        rows.sort(key=lambda entry: entry.ts)
        return rows if limit is None else rows[: max(0, limit)]

    def list(
        self,
        status: str | FrictionStatus | None = None,
        limit: Optional[int] = None,
    ) -> list[FrictionEntry]:
        """List queue rows, optionally filtered by status."""
        rows = self.load()
        if status is not None:
            wanted = FrictionStatus(status)
            rows = [entry for entry in rows if entry.status == wanted]
        rows.sort(key=lambda entry: entry.ts)
        return rows if limit is None else rows[: max(0, limit)]

    def enqueue(self, entry: FrictionEntry | dict) -> bool:
        """Write an entry unless an equivalent pending row already exists."""
        if not isinstance(entry, FrictionEntry):
            entry = FrictionEntry.model_validate(entry)
        with memory_write_lock():
            rows = self.load()
            if any(
                row.is_pending() and row.dedupe_key() == entry.dedupe_key()
                for row in rows
            ):
                return False
            rows.append(entry)
            self._save(rows)
        return True

    def resolve(
        self,
        ids: list[str],
        *,
        dream_run_id: str = "",
        disposition: str = "",
        note: str = "",
    ) -> int:
        """Resolve pending rows while retaining them in the JSONL audit trail."""
        id_set = {str(entry_id).strip() for entry_id in ids if str(entry_id).strip()}
        if not id_set:
            return 0

        with memory_write_lock():
            rows = self.load()
            resolved_at = _utc_now()
            updated = 0
            for row in rows:
                if row.id not in id_set or not row.is_pending():
                    continue
                row.status = FrictionStatus.RESOLVED
                row.resolved_at = resolved_at
                row.dream_run_id = dream_run_id.strip() or None
                row.disposition = disposition.strip()
                row.note = note.strip()
                updated += 1
            if updated:
                self._save(rows)
        return updated

    def _save(self, entries: list[FrictionEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(entry.model_dump_json() for entry in entries)
        if body:
            body += "\n"
        fd, temp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
            os.replace(temp_path, self._path)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise


__all__ = [
    "FrictionEntry",
    "FrictionKind",
    "FrictionQueue",
    "FrictionSeverity",
    "FrictionStatus",
]
