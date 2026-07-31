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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..syncing import choose_timestamp_sync_action
from .frontmatter import (
    parse_frontmatter_timestamp,
    set_frontmatter_timestamp,
    strip_frontmatter,
)
from .ouro_docs import OuroDocStore

logger = logging.getLogger(__name__)


__all__ = [
    "SyncResult",
    "sync_workspace",
    # Re-exported for backward compatibility with callers that imported
    # frontmatter helpers from this module.
    "parse_frontmatter_timestamp",
    "strip_frontmatter",
    "set_frontmatter_timestamp",
]


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
        from .team_paths import preferred_team_dir_name, team_workspace_dir

        team_dir = team_workspace_dir(
            workspace,
            team_id,
            team_slug=getattr(doc_store, "team_slug", None),
        )
        local_path = team_dir / "MEMORY.md"
        post_name = doc_store.memory_name(agent_name)
        leaf = preferred_team_dir_name(
            team_id, team_slug=getattr(doc_store, "team_slug", None)
        )
        key = f"teams/{leaf}/MEMORY.md"
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

    decision = choose_timestamp_sync_action(
        local_body=local_body,
        remote_body=ouro_body,
        local_ts=local_ts,
        remote_ts=ouro_ts,
    )
    action = decision.action
    if action == "unchanged":
        result.unchanged.append(key)
        return

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


def _write_local_with_timestamp(path: Path, body: str, ts: datetime) -> None:
    """Write body to a local file with ``last_updated`` in frontmatter."""
    content = set_frontmatter_timestamp(body, ts)
    path.write_text(content)
