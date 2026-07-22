"""Process-wide lock for durable workspace / doc mutations under parallel runs."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_memory_write_lock = threading.RLock()


@contextmanager
def memory_write_lock() -> Iterator[None]:
    """Serialize RMW workspace/doc writes across overlapping agent runs."""
    with _memory_write_lock:
        yield
