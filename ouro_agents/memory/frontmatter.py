"""YAML frontmatter helpers for local memory files.

Local team-memory files persist their ``last_updated`` timestamp in a YAML
frontmatter block so the workspace sync engine can decide whether to push or
pull. Kept in its own module so both the doc stores and the sync engine can
share the helpers without circular imports.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?\n)---\s*\n?", re.DOTALL
)
_TIMESTAMP_RE = re.compile(
    r"^last_updated:\s*(.+)$", re.MULTILINE
)


def parse_frontmatter_timestamp(text: str) -> Optional[datetime]:
    """Extract ``last_updated`` from YAML frontmatter, or None."""
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return None
    ts_match = _TIMESTAMP_RE.search(fm_match.group("fm"))
    if not ts_match:
        return None
    try:
        return datetime.fromisoformat(ts_match.group(1).strip())
    except ValueError:
        return None


def strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter block from markdown."""
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():] if m else text


def set_frontmatter_timestamp(text: str, ts: datetime) -> str:
    """Set ``last_updated`` in frontmatter, creating or updating it."""
    ts_line = f"last_updated: {ts.isoformat()}"
    fm_match = _FRONTMATTER_RE.match(text)

    if fm_match:
        fm_block = fm_match.group("fm")
        if _TIMESTAMP_RE.search(fm_block):
            new_fm = _TIMESTAMP_RE.sub(ts_line, fm_block)
        else:
            new_fm = ts_line + "\n" + fm_block
        return f"---\n{new_fm}---\n{text[fm_match.end():]}"

    body = text.lstrip("\n")
    return f"---\n{ts_line}\n---\n{body}"
