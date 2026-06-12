"""Local webhook event pooling for Ouro agent servers."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Optional

from .config import EventPoolingConfig, EventPoolTimingConfig
from .event_registry import spec_for
from .events import EventRunContext

logger = logging.getLogger(__name__)

EventDispatcher = Callable[[EventRunContext], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]
JitterFn = Callable[[float, float], float]
MonotonicFn = Callable[[], float]


@dataclass
class _PendingBatch:
    key: str
    events: list[EventRunContext]
    first_received: float
    deadline: float
    timing: EventPoolTimingConfig
    task: Optional[asyncio.Task] = None


class EventPool:
    """Debounce and coalesce webhook events before running the agent once."""

    def __init__(
        self,
        config: EventPoolingConfig,
        dispatcher: EventDispatcher,
        *,
        sleep: SleepFn = asyncio.sleep,
        jitter: JitterFn = random.uniform,
        monotonic: MonotonicFn = time.monotonic,
    ) -> None:
        self._config = config
        self._dispatcher = dispatcher
        self._sleep = sleep
        self._jitter = jitter
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._pending: dict[str, _PendingBatch] = {}
        self._running_keys: set[str] = set()
        self._running_done_events: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task] = set()

    def timing_for(self, event_type: str) -> Optional[EventPoolTimingConfig]:
        if not self._config.enabled:
            return None
        timing = self._config.events.get(event_type)
        if not timing or not timing.enabled:
            return None
        return timing

    def pool_key(self, event_run: EventRunContext) -> Optional[str]:
        spec = spec_for(event_run.event_type)
        if not spec.pool_key_fn:
            return None
        return spec.pool_key_fn(event_run)

    def is_poolable(self, event_run: EventRunContext) -> bool:
        return bool(self.timing_for(event_run.event_type) and self.pool_key(event_run))

    async def submit(self, event_run: EventRunContext) -> bool:
        """Queue an event if poolable, otherwise dispatch it immediately."""
        timing = self.timing_for(event_run.event_type)
        key = self.pool_key(event_run)
        if not timing or not key:
            await self._dispatcher(event_run)
            return False

        now = self._monotonic()
        async with self._lock:
            batch = self._pending.get(key)
            if batch is None:
                batch = _PendingBatch(
                    key=key,
                    events=[event_run],
                    first_received=now,
                    deadline=self._deadline(now, now, timing),
                    timing=timing,
                )
                self._pending[key] = batch
            else:
                batch.events.append(event_run)
                batch.timing = timing
                batch.deadline = self._deadline(now, batch.first_received, timing)
                if batch.task:
                    batch.task.cancel()

            batch.task = self._create_task(self._dispatch_after_delay(key))

        logger.info(
            "Pooled %s event under %s (size=%d, deadline=%.2f)",
            event_run.event_type,
            key,
            len(batch.events),
            batch.deadline,
        )
        return True

    async def discard(self, key: str) -> list[EventRunContext]:
        """Drop a pending batch without dispatching it (e.g. user interrupt).

        Returns the discarded events so the caller can record that they
        happened (they will never reach the agent otherwise).
        """
        async with self._lock:
            batch = self._pending.pop(key, None)
        if not batch:
            return []
        if batch.task:
            batch.task.cancel()
        logger.info("Discarded pooled batch %s (size=%d)", key, len(batch.events))
        return batch.events

    async def stop(self) -> None:
        async with self._lock:
            self._pending.clear()
            tasks = list(self._tasks)

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _create_task(self, coro: Awaitable[None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                "Event pool task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _deadline(
        self,
        now: float,
        first_received: float,
        timing: EventPoolTimingConfig,
    ) -> float:
        settle_deadline = now + timing.settle_seconds
        max_deadline = first_received + timing.max_wait_seconds
        return min(settle_deadline, max_deadline)

    async def _dispatch_after_delay(self, key: str) -> None:
        while True:
            async with self._lock:
                batch = self._pending.get(key)
                if not batch:
                    return
                delay = max(0.0, batch.deadline - self._monotonic())

            if delay:
                await self._sleep(delay)

            async with self._lock:
                batch = self._pending.get(key)
                if not batch:
                    return
                if self._monotonic() < batch.deadline:
                    continue
                self._pending.pop(key, None)

            jitter_delay = (
                self._jitter(0.0, batch.timing.jitter_seconds)
                if batch.timing.jitter_seconds
                else 0.0
            )
            if jitter_delay:
                await self._sleep(jitter_delay)

            await self._acquire_run_slot(key)
            try:
                await self._dispatcher(build_pooled_event_run(batch.events))
            finally:
                await self._release_run_slot(key)
            return

    async def _acquire_run_slot(self, key: str) -> None:
        """Wait until no run is active for this key, then reserve the slot."""

        while True:
            async with self._lock:
                if key not in self._running_keys:
                    self._running_keys.add(key)
                    return

                done_event = self._running_done_events.get(key)
                if done_event is None:
                    done_event = asyncio.Event()
                    self._running_done_events[key] = done_event

            await done_event.wait()

    async def _release_run_slot(self, key: str) -> None:
        async with self._lock:
            self._running_keys.discard(key)
            done_event = self._running_done_events.pop(key, None)

        if done_event is not None:
            done_event.set()


def build_pooled_event_run(events: Sequence[EventRunContext]) -> EventRunContext:
    if not events:
        raise ValueError("Cannot build a pooled event run from an empty batch")

    latest = events[-1]
    if len(events) == 1:
        return latest

    summary = _format_pooled_context(events)
    feedback_text = (
        f"{latest.feedback_text}\n\n{summary}" if latest.feedback_text else None
    )
    return replace(
        latest,
        task=f"{latest.task}\n\n{summary}",
        feedback_text=feedback_text,
        notification_ids=_merge_notification_ids(events),
    )


def _format_pooled_context(events: Sequence[EventRunContext]) -> str:
    lines = [
        "## Pooled Event Batch",
        (
            f"This run represents {len(events)} event(s) received close together "
            "for the same conversation or thread. Consider the full batch before "
            "acting. Reply at most once; do not respond separately to each event. "
            "If no single reply would add value, return exactly `NO_ACTION`."
        ),
        "",
        "The primary task above is based on the latest event in the batch.",
        "",
        "Events in arrival order:",
    ]
    lines.extend(_format_event_line(i, event) for i, event in enumerate(events, 1))
    return "\n".join(lines)


def _merge_notification_ids(events: Sequence[EventRunContext]) -> tuple[str, ...]:
    notification_ids: list[str] = []
    for event in events:
        notification_ids.extend(event.notification_ids)
    return tuple(dict.fromkeys(notification_ids))


def _format_event_line(index: int, event: EventRunContext) -> str:
    actor = _format_actor(event)
    timestamp = f" at {event.received_at}" if event.received_at else ""
    source = f", source_id={event.source_id}" if event.source_id else ""
    text = _compact_text(event.event_text or event.feedback_text or "")
    return f"{index}. {event.event_type} from {actor}{timestamp}{source}: {text}"


def _format_actor(event: EventRunContext) -> str:
    if event.actor_username:
        label = f"@{event.actor_username}"
    elif event.actor_user_id:
        label = event.actor_user_id
    else:
        label = "unknown"

    if event.actor_is_agent is True:
        return f"{label} (agent)"
    if event.actor_is_agent is False:
        return f"{label} (human)"
    return label


def _compact_text(text: str, limit: int = 500) -> str:
    compact = " ".join(text.strip().split())
    if not compact:
        return "(no text)"
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."
