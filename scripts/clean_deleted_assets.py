#!/usr/bin/env python
"""Backfill cleanup for assets the user deleted while the agent was offline.

Scans mem0 vectors and workspace docs for asset UUIDs, batch-checks each one
against Ouro, and runs the same regex-sweep + mem0-prune that the
``asset.deleted`` webhook handler uses for any UUID Ouro returns 404 for.

Usage::

    cd ouro-agents
    python -m scripts.clean_deleted_assets --config config.json --dry-run
    python -m scripts.clean_deleted_assets --config config.json
    python -m scripts.clean_deleted_assets --config config.json --max 50
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from ouro_agents.cleanup.asset_deleted import (
    SweepResult,
    discover_files_with_asset,
    sweep_workspace_for_deleted_asset,
)
from ouro_agents.config import OuroAgentsConfig
from ouro_agents.memory import create_memory_backend


logger = logging.getLogger("clean_deleted_assets")


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


# ---------------------------------------------------------------------------
# UUID discovery
# ---------------------------------------------------------------------------


def collect_uuids_from_mem0(backend, agent_id: str) -> set[str]:
    """Scan every memory's asset_ids metadata for known UUIDs."""
    uuids: set[str] = set()
    # Single batch — most agents have <1k memories. mem0 doesn't expose a
    # stable cursor, so we ask once for a generous limit.
    try:
        rows = backend.get_all(agent_id=agent_id, limit=1000)
    except Exception as exc:
        logger.warning("Failed to enumerate mem0: %s", exc)
        return uuids
    for row in rows:
        for aid in row.asset_ids or []:
            if _UUID_RE.fullmatch(aid):
                uuids.add(aid)
        # Catch ids embedded in raw text or stray metadata too.
        for match in _UUID_RE.findall(row.text or ""):
            uuids.add(match)
    return uuids


def collect_uuids_from_workspace(workspace: Path) -> set[str]:
    """Grep the workspace for UUID-shaped tokens in markdown / json files."""
    uuids: set[str] = set()
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".md", ".json"}:
            continue
        # Skip discovery in clearly noisy/cached dirs.
        skip_parts = {
            "data",
            "chroma",
            "memory",
            "memory-old",
            "__pycache__",
            "cifs",
            "cifs_old",
            "debug-runs",
            "conversations",
        }
        if any(part in skip_parts for part in path.parts):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for match in _UUID_RE.findall(text):
            uuids.add(match)
    return uuids


# ---------------------------------------------------------------------------
# Non-asset UUID filter
# ---------------------------------------------------------------------------


# UUIDs that are guaranteed not to be assets and would otherwise generate noisy
# 404-equivalent responses from /assets/{id}/type. We exclude:
#   * the nil UUID (placeholder in config + GLOBAL_ORG_ID)
#   * every team id known locally via workspace/teams/<uuid>/
#   * the agent's configured org_id
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def collect_known_non_asset_uuids(workspace: Path, org_id: str | None) -> set[str]:
    """Return UUIDs we know belong to teams/orgs/users, not assets."""
    skip: set[str] = {_NIL_UUID}
    if org_id and _UUID_RE.fullmatch(org_id):
        skip.add(org_id)
    teams_root = workspace / "teams"
    if teams_root.is_dir():
        for child in teams_root.iterdir():
            if child.is_dir() and _UUID_RE.fullmatch(child.name):
                skip.add(child.name)
    return skip


# ---------------------------------------------------------------------------
# Existence check
# ---------------------------------------------------------------------------


_MISSING_ERROR_FRAGMENTS = (
    "404",
    "not found",
    "no asset_type",
    # PostgREST PGRST116: ``.single()`` returned 0 rows. The /assets/{id}/type
    # endpoint uses single() so these messages are functionally a 404.
    "single json object",
    "json object requested",
    "multiple (or no) rows returned",
    "pgrst116",
    "0 rows",
)


def asset_missing_on_ouro(client, asset_id: str) -> bool:
    """Return True iff Ouro has no asset with that id (404 or PGRST116)."""
    try:
        response = client.assets.client.get(f"/assets/{asset_id}/type")
        data = client.assets._handle_response(response)
        if not data or not data.get("asset_type"):
            return True
        return False
    except Exception as exc:
        # ouro-py raises NotFoundError on 404 (subclass of ApiError). Anything
        # else is treated as transient and we conservatively keep the asset.
        msg = str(exc).lower()
        if any(frag in msg for frag in _MISSING_ERROR_FRAGMENTS):
            return True
        logger.warning("Existence check for %s failed: %s", asset_id, exc)
        return False


# ---------------------------------------------------------------------------
# Cleanup driver
# ---------------------------------------------------------------------------


def _prune_mem0(backend, agent_id: str, asset_id: str) -> int:
    """Delete every mem0 entry whose asset_ids contains ``asset_id``."""
    deleted = 0
    seen: set[str] = set()
    try:
        results = backend.find_by_asset(asset_id, agent_id=agent_id)
    except Exception as exc:
        logger.warning("find_by_asset failed for %s: %s", asset_id, exc)
        return 0
    for mem in results:
        if not mem.id or mem.id in seen:
            continue
        seen.add(mem.id)
        try:
            backend.delete(mem.id)
            deleted += 1
        except Exception as exc:
            logger.warning("Delete failed for memory %s: %s", mem.id, exc)
    return deleted


def cleanup_one(
    backend,
    agent_id: str,
    workspace: Path,
    asset_id: str,
    *,
    dry_run: bool,
) -> SweepResult:
    if dry_run:
        candidates = discover_files_with_asset(asset_id, workspace)
        result = SweepResult(asset_id=asset_id)
        result.files_inspected = [str(p) for p in candidates]
        try:
            mem_hits = backend.find_by_asset(asset_id, agent_id=agent_id)
        except Exception:
            mem_hits = []
        result.mem0_deleted = len(mem_hits)
        return result

    result = sweep_workspace_for_deleted_asset(workspace, asset_id)
    result.mem0_deleted = _prune_mem0(backend, agent_id, asset_id)
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_ouro_client(config: OuroAgentsConfig):
    """Mirror OuroAgent._get_ouro_client without spinning up the whole agent."""
    from ouro import Ouro

    api_key = None
    base_url = None
    for server in config.mcp_servers:
        if server.name == "ouro" and server.env:
            api_key = server.env.get("OURO_API_KEY")
            base_url = server.env.get("OURO_BASE_URL") or server.env.get(
                "OURO_BACKEND_URL"
            )
            break
    if not api_key:
        api_key = os.environ.get("OURO_API_KEY")
    if not base_url:
        base_url = os.environ.get("OURO_BASE_URL") or os.environ.get(
            "OURO_BACKEND_URL"
        )
    if not api_key:
        raise RuntimeError(
            "OURO_API_KEY not found in config (mcp_servers[ouro].env) or environment"
        )

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return Ouro(**kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to the agent's config.json (default: %(default)s)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Override the agent's workspace path",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Cap the number of UUIDs processed in this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended actions without writing or deleting anything",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show DEBUG-level logs",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = OuroAgentsConfig.load_from_file(args.config)
    workspace = Path(args.workspace) if args.workspace else config.agent.workspace
    if not workspace.exists():
        print(f"Workspace not found: {workspace}", file=sys.stderr)
        return 2

    backend = create_memory_backend(config.memory)
    client = _build_ouro_client(config)
    agent_id = config.agent.name

    logger.info("Collecting candidate UUIDs from mem0 + workspace...")
    mem_uuids = collect_uuids_from_mem0(backend, agent_id)
    ws_uuids = collect_uuids_from_workspace(workspace)
    raw_candidates = mem_uuids | ws_uuids

    skip = collect_known_non_asset_uuids(workspace, config.agent.org_id)
    skipped = raw_candidates & skip
    candidates = sorted(raw_candidates - skip)
    logger.info(
        "Found %d unique candidate UUIDs (mem0=%d, workspace=%d, skipped %d known team/org ids)",
        len(candidates),
        len(mem_uuids),
        len(ws_uuids),
        len(skipped),
    )

    if args.max is not None:
        candidates = candidates[: args.max]

    missing: list[str] = []
    for i, asset_id in enumerate(candidates, start=1):
        if asset_missing_on_ouro(client, asset_id):
            missing.append(asset_id)
            logger.info("[%d/%d] %s -> MISSING on Ouro", i, len(candidates), asset_id)
        else:
            logger.debug("[%d/%d] %s -> exists", i, len(candidates), asset_id)

    if not missing:
        logger.info("No missing assets. Nothing to clean up.")
        return 0

    logger.info(
        "%d asset(s) missing on Ouro. %s cleanup...",
        len(missing),
        "Simulating" if args.dry_run else "Running",
    )

    total_files = 0
    total_edits = 0
    total_mem0 = 0
    for asset_id in missing:
        result = cleanup_one(
            backend, agent_id, workspace, asset_id, dry_run=args.dry_run
        )
        files_for_asset = result.files_rewritten or result.files_inspected
        total_files += len(files_for_asset)
        total_edits += result.total_edits
        total_mem0 += result.mem0_deleted
        if files_for_asset or result.mem0_deleted:
            logger.info(
                "  %s: %d mem0, %d edits across %d file(s)%s",
                asset_id,
                result.mem0_deleted,
                result.total_edits,
                len(files_for_asset),
                " (dry-run)" if args.dry_run else "",
            )

    logger.info(
        "%s: %d asset(s), %d file(s) touched, %d total edits, %d mem0 vectors %s",
        "Dry-run summary" if args.dry_run else "Cleanup complete",
        len(missing),
        total_files,
        total_edits,
        total_mem0,
        "would be deleted" if args.dry_run else "deleted",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
