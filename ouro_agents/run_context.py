"""Per-run state for overlapping top-level agent modes.

Top-level modes (chat, autonomous, heartbeat, plan, review, dream) may overlap
on one ``OuroAgent``. Run-scoped fields that used to live on the agent instance
live here instead, accessed via a ``contextvars.ContextVar`` set for the
duration of each blocking run thread.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from .cancellation import RunCancellationToken
    from .subagents.context import SubAgentUsage
    from .usage import UsageTracker


@dataclass
class RunContext:
    """Mutable state owned by a single top-level (or nested dream) run."""

    run_id: str
    mode: str = ""
    event_type: Optional[str] = None
    conversation_id: Optional[str] = None
    team_id: Optional[str] = None
    tick_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    usage_tracker: Optional["UsageTracker"] = None
    subagent_ledger: list[tuple[str, "SubAgentUsage"]] = field(default_factory=list)
    heartbeat_cheap_workers: bool = False
    cancellation_token: Optional["RunCancellationToken"] = None
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    task_preview: str = ""


_current_run: ContextVar[Optional[RunContext]] = ContextVar(
    "ouro_agents_current_run", default=None
)


def get_run_context() -> Optional[RunContext]:
    return _current_run.get()


def require_run_context() -> RunContext:
    ctx = _current_run.get()
    if ctx is None:
        raise RuntimeError("No active RunContext — call outside of a run thread?")
    return ctx


@contextmanager
def bind_run_context(ctx: RunContext) -> Iterator[RunContext]:
    """Bind ``ctx`` for the current thread/task until the ``with`` block exits."""
    token: Token = _current_run.set(ctx)
    try:
        yield ctx
    finally:
        _current_run.reset(token)


@dataclass(frozen=True)
class ActiveRunSnapshot:
    """Public view of a live top-level run (no cancellation token)."""

    run_id: str
    mode: str
    event_type: Optional[str]
    started_at: str
    conversation_id: Optional[str]
    team_id: Optional[str]
    task_preview: str
    status: str = "running"


class ActiveRunRegistry:
    """In-process registry of overlapping top-level runs.

    Powers cancel/interrupt today and is the seam for a future
    ``list_active_runs`` tool (not implemented yet).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, tuple[RunContext, "RunCancellationToken"]] = {}

    def register(
        self,
        ctx: RunContext,
        token: "RunCancellationToken",
    ) -> None:
        with self._lock:
            self._runs[ctx.run_id] = (ctx, token)

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def list_snapshots(self) -> list[ActiveRunSnapshot]:
        with self._lock:
            return [
                ActiveRunSnapshot(
                    run_id=ctx.run_id,
                    mode=ctx.mode,
                    event_type=ctx.event_type,
                    started_at=ctx.started_at,
                    conversation_id=ctx.conversation_id,
                    team_id=ctx.team_id,
                    task_preview=(ctx.task_preview or "")[:200],
                    status="running",
                )
                for ctx, _ in self._runs.values()
            ]

    def get_token(self, run_id: str) -> Optional["RunCancellationToken"]:
        with self._lock:
            entry = self._runs.get(run_id)
            return entry[1] if entry else None

    def tokens_for_conversation(
        self, conversation_id: str
    ) -> list["RunCancellationToken"]:
        with self._lock:
            return [
                token
                for ctx, token in self._runs.values()
                if ctx.conversation_id == conversation_id
            ]

    def all_tokens(self) -> list["RunCancellationToken"]:
        with self._lock:
            return [token for _, token in self._runs.values()]
