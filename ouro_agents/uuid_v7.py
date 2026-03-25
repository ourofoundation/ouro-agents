"""RFC 9562 UUID v7 strings (aligned with backend and ouro-js ``uuidv7``)."""

from __future__ import annotations

import uuid7 as _uuid7


def uuid7_str() -> str:
    """Return a new time-ordered UUID v7 as a hyphenated string."""
    return str(_uuid7.create())
