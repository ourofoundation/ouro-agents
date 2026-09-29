"""Webhook transport concerns: delivery dedupe and signature verification."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable

from ouro.events import (
    WEBHOOK_ATTEMPT_HEADER as ATTEMPT_HEADER,
    WEBHOOK_DELIVERY_HEADER as DELIVERY_HEADER,
    WEBHOOK_SIGNATURE_HEADER as SIGNATURE_HEADER,
    verify_webhook_signature,
)

__all__ = [
    "ATTEMPT_HEADER",
    "DELIVERY_HEADER",
    "SIGNATURE_HEADER",
    "DeliveryDedupe",
    "verify_webhook_signature",
]


class DeliveryDedupe:
    """Bounded, in-memory TTL set of webhook delivery ids.

    ``claim`` is check-and-insert: it returns False when the id was already
    claimed within ``ttl_seconds``. ``release`` forgets an id so a delivery
    that failed before it was accepted can be retried. State is per-process,
    so a restart between an attempt and its retry can still double-run.
    """

    def __init__(
        self,
        ttl_seconds: float = 24 * 60 * 60,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def claim(self, delivery_id: str) -> bool:
        now = self._clock()
        with self._lock:
            self._evict(now)
            if delivery_id in self._seen:
                return False
            self._seen[delivery_id] = now
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)
            return True

    def release(self, delivery_id: str) -> None:
        with self._lock:
            self._seen.pop(delivery_id, None)

    def __contains__(self, delivery_id: object) -> bool:
        with self._lock:
            self._evict(self._clock())
            return delivery_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def _evict(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        while self._seen:
            oldest_id, seen_at = next(iter(self._seen.items()))
            if seen_at > cutoff:
                break
            self._seen.popitem(last=False)
