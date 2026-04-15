"""Bidirectional sync between local team memory files and Ouro posts.

Top-level workspace files are local-only. The sync engine is only used for
team-scoped docs such as ``teams/{team_id}/MEMORY.md``.

Local files store their timestamp in YAML frontmatter::

    ---
    last_updated: 2026-03-27T14:30:00+00:00
    ---
    # Memory
    ...

Ouro posts carry ``last_updated`` on the Asset model. Whichever timestamp is
newer wins. Local frontmatter is stripped before uploading to Ouro.
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
    team_doc_stores: dict[str, OuroDocStore],
    agent_name: str,
    *,
    dry_run: bool = False,
) -> SyncResult:
    """Sync local team-memory files with their Ouro post counterparts."""
    result = SyncResult()

    for team_id, doc_store in sorted(team_doc_stores.items()):
        local_path = workspace / "teams" / team_id / "MEMORY.md"
        post_name = doc_store.memory_name(agent_name)
        key = f"teams/{team_id}/MEMORY.md"
        _sync_target(
            local_path=local_path,
            post_name=post_name,
            key=key,
            doc_store=doc_store,
            result=result,
            dry_run=dry_run,
        )

    return result


def _sync_target(
    *,
    local_path: Path,
    post_name: str,
    key: str,
    doc_store: OuroDocStore,
    result: SyncResult,
    dry_run: bool,
) -> None:
    local_raw = ""
    if local_path.exists():
        local_raw = local_path.read_text()

    local_ts = parse_frontmatter_timestamp(local_raw)
    local_body = strip_frontmatter(local_raw).strip()

    try:
        ouro_result = doc_store.read_with_meta(post_name)
    except Exception as e:
        result.errors.append(f"{key}: failed to read Ouro post: {e}")
        return

    ouro_body = ouro_result.content.strip()
    ouro_ts = ouro_result.last_updated

    if not local_body and not ouro_body:
        result.unchanged.append(key)
        return

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
            return
    elif local_body and not ouro_body:
        action = "push"
    elif ouro_body and not local_body:
        action = "pull"
    elif local_body and not local_ts and ouro_ts:
        action = "push"
    elif ouro_body and not ouro_ts and local_ts:
        action = "pull"
    else:
        action = "push"

    now = datetime.now(timezone.utc)

    if action == "push" and local_body:
        if not dry_run:
            try:
                doc_store.write(post_name, local_body)
            except Exception as e:
                result.errors.append(f"{key}: push failed: {e}")
                return
            _write_local_with_timestamp(local_path, local_body, now)
        result.pushed.append(key)
        logger.info("Synced %s → Ouro (%s)", key, post_name)
        return

    if action == "pull" and ouro_body:
        ts = ouro_ts or now
        if not dry_run:
            _write_local_with_timestamp(local_path, ouro_body, ts)
        result.pulled.append(key)
        logger.info("Synced Ouro → %s (%s)", key, post_name)
        return

    result.unchanged.append(key)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _write_local_with_timestamp(path: Path, body: str, ts: datetime) -> None:
    """Write body to a local file with ``last_updated`` in frontmatter."""
    content = set_frontmatter_timestamp(body, ts)
    path.write_text(content)
