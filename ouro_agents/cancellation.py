"""Cooperative cancellation for long-running agent loops."""

from __future__ import annotations

import logging
import threading
import weakref
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)


class RunCancelled(KeyboardInterrupt):
    """Raised when an Ouro agent run is cancelled cooperatively."""


class RunCancellationToken:
    """Cancellation signal shared by a parent run and its subagents.

    smolagents exposes an ``interrupt()`` switch, but it lives on each concrete
    agent instance. This token keeps track of every smolagents loop created for
    a run so one Ctrl-C or server shutdown can trip them all.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._agents: weakref.WeakSet[object] = weakref.WeakSet()
        self.reason = "cancelled"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason or "cancelled"
        self._event.set()
        with self._lock:
            agents = list(self._agents)

        for agent in agents:
            interrupt = getattr(agent, "interrupt", None)
            if not callable(interrupt):
                continue
            try:
                interrupt()
            except Exception:
                logger.debug("Failed to interrupt active smolagents loop", exc_info=True)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelled(self.reason)

    @contextmanager
    def registered_agent(self, agent: object) -> Iterator[None]:
        with self._lock:
            self._agents.add(agent)
        try:
            self.raise_if_cancelled()
            yield
        finally:
            with self._lock:
                self._agents.discard(agent)

