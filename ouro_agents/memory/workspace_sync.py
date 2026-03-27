"""Bidirectional sync between local workspace files and Ouro posts.

On startup, compares ``last_updated`` timestamps to determine which side
is newer, then pushes or pulls accordingly.

Local files store their timestamp in YAML frontmatter::

    ---
    last_updated: 2026-03-27T14:30:00+00:00
    ---
    # Identity
    ...

Ouro posts carry ``last_updated`` on the Asset model.  Whichever
timestamp is newer wins.  On first sync (no local frontmatter),
the local file is pushed to Ouro if it has content.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ouro_docs import OuroDocStore

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?\n)---\s*\n?", re.DOTALL
)
_TIMESTAMP_RE = re.compile(
    r"^last_updated:\s*(.+)$", re.MULTILINE
)


@dataclass
class ManagedDoc:
    """A workspace file that maps to a named Ouro post."""

    filename: str
    post_name_template: str  # e.g. "SOUL:{agent_name}"

    def post_name(self, agent_name: str) -> str:
        return self.post_name_template.format(agent_name=agent_name)


MANAGED_DOCS = [
    ManagedDoc("SOUL.md", "SOUL:{agent_name}"),
    ManagedDoc("NOTES.md", "NOTES:{agent_name}"),
    ManagedDoc("HEARTBEAT.md", "HEARTBEAT:{agent_name}"),
    ManagedDoc("MEMORY.md", "MEMORY:{agent_name}"),
]


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sync_workspace(
    workspace: Path,
    doc_store: OuroDocStore,
    agent_name: str,
    *,
    dry_run: bool = False,
) -> SyncResult:
    """Sync local workspace files with their Ouro post counterparts.

    Compares ``last_updated`` timestamps in local frontmatter vs the Ouro
    post's ``last_updated`` field.  Newer side wins.
    """
    result = SyncResult()

    for doc in MANAGED_DOCS:
        local_path = workspace / doc.filename
        post_name = doc.post_name(agent_name)
        key = doc.filename

        local_raw = ""
        if local_path.exists():
            local_raw = local_path.read_text()

        local_ts = parse_frontmatter_timestamp(local_raw)
        local_body = strip_frontmatter(local_raw).strip()

        try:
            ouro_result = doc_store.read_with_meta(post_name)
        except Exception as e:
            result.errors.append(f"{key}: failed to read Ouro post: {e}")
            continue

        ouro_body = ouro_result.content.strip()
        ouro_ts = ouro_result.last_updated

        if not local_body and not ouro_body:
            result.unchanged.append(key)
            continue

        # Determine action based on timestamps
        action: Optional[str] = None

        if local_ts and ouro_ts:
            local_aware = _ensure_utc(local_ts)
            ouro_aware = _ensure_utc(ouro_ts)
            if local_aware > ouro_aware:
                action = "push"
            elif ouro_aware > local_aware:
                action = "pull"
            else:
                result.unchanged.append(key)
                continue
        elif local_body and not ouro_body:
            action = "push"
        elif ouro_body and not local_body:
            action = "pull"
        elif local_body and not local_ts and ouro_ts:
            # Local has no timestamp (never synced) but has content.
            # Push local so the user's edits aren't lost.
            action = "push"
        elif ouro_body and not ouro_ts and local_ts:
            action = "pull"
        else:
            # Both have content, neither has timestamps — push local
            action = "push"

        now = datetime.now(timezone.utc)

        if action == "push" and local_body:
            if not dry_run:
                try:
                    doc_store.write(post_name, local_body)
                except Exception as e:
                    result.errors.append(f"{key}: push failed: {e}")
                    continue
                _write_local_with_timestamp(local_path, local_body, now)
            result.pushed.append(key)
            logger.info("Synced %s → Ouro (%s)", key, post_name)

        elif action == "pull" and ouro_body:
            ts = ouro_ts or now
            if not dry_run:
                _write_local_with_timestamp(local_path, ouro_body, ts)
            result.pulled.append(key)
            logger.info("Synced Ouro → %s (%s)", key, post_name)

        else:
            result.unchanged.append(key)

    return result


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _write_local_with_timestamp(path: Path, body: str, ts: datetime) -> None:
    """Write body to a local file with ``last_updated`` in frontmatter."""
    content = set_frontmatter_timestamp(body, ts)
    path.write_text(content)
