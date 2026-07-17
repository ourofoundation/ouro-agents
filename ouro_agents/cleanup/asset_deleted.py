"""Deterministic cleanup for ``asset.deleted`` webhook events.

Two-part operation:

1. **mem0 prune**: hard-delete every vector memory whose ``asset_ids`` metadata
   contains the deleted asset id. Safe and fast; UUIDs are unambiguous.
2. **Workspace sweep**: rewrite markdown / JSON files that reference the asset
   id, marking the references as ``[deleted]`` (or removing them from arrays
   for plan JSON). Plans whose own backing quest was deleted are archived to
   ``history/`` outright; everything else is rewritten in place, never deleted.

Both parts run synchronously inside the webhook handler. No LLM is invoked.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from ..memory.frontmatter import set_frontmatter_timestamp

if TYPE_CHECKING:  # pragma: no cover
    from ..agent import OuroAgent
    from ..events import EventRunContext

logger = logging.getLogger(__name__)


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Markdown link forms we recognize as typed asset references:
#     [label](post:UUID)
#     [label](dataset:UUID)
#     [label](file:UUID)
#     [label](service:UUID)
#     [label](route:UUID)
#     [label](asset:UUID)
_ASSET_LINK_TYPES = ("post", "dataset", "file", "service", "route", "quest", "asset")

# Files we never touch even if they contain the UUID.
_SWEEP_EXCLUDE_DIRS = {
    "data",
    "chroma",
    "memory",
    "memory-old",
    "__pycache__",
    "cifs",
    "cifs_old",
    "debug-runs",
    "conversations",  # immutable run records
    "daily_logs_archive",
}


@dataclass
class SweepResult:
    asset_id: str
    files_inspected: list[str] = field(default_factory=list)
    files_rewritten: list[str] = field(default_factory=list)
    edits_per_file: dict[str, int] = field(default_factory=dict)
    plans_archived: list[str] = field(default_factory=list)
    mem0_deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_edits(self) -> int:
        return sum(self.edits_per_file.values())


# ---------------------------------------------------------------------------
# Discovery: find files that reference a UUID
# ---------------------------------------------------------------------------


def _ripgrep_files_with(uuid: str, root: Path) -> Optional[list[Path]]:
    """Use ripgrep to enumerate files containing ``uuid`` under ``root``.

    Returns ``None`` when ripgrep is not on PATH so callers can fall back.
    """
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [
        rg,
        "--files-with-matches",
        "--fixed-strings",
        "--no-messages",
        "--hidden",
        # Workspaces are typically gitignored by the parent repo, which makes
        # ripgrep silently skip them. We rely on _SWEEP_EXCLUDE_DIRS for noise
        # control instead.
        "--no-ignore",
    ]
    for excluded in _SWEEP_EXCLUDE_DIRS:
        # ``!**/dir/**`` matches the directory anywhere under the search root.
        # A bare ``!dir/**`` only matches paths anchored at the cwd.
        cmd.extend(["--glob", f"!**/{excluded}/**"])
    cmd.extend([uuid, str(root)])
    try:
        completed = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=30
        )
    except subprocess.SubprocessError as exc:
        logger.warning("ripgrep failed for %s: %s", uuid, exc)
        return None
    if completed.returncode not in (0, 1):  # 1 = no matches, fine
        logger.warning("ripgrep exited %s: %s", completed.returncode, completed.stderr)
        return None
    return [Path(line) for line in completed.stdout.splitlines() if line.strip()]


def _python_walk_files_with(uuid: str, root: Path) -> list[Path]:
    """Fallback discovery when ripgrep isn't available."""
    matches: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SWEEP_EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix not in {".md", ".json", ".jsonl", ".txt"}:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if uuid in text:
            matches.append(path)
    return matches


def discover_files_with_asset(asset_id: str, workspace: Path) -> list[Path]:
    """Return all files under ``workspace`` whose contents include ``asset_id``."""
    matches = _ripgrep_files_with(asset_id, workspace)
    if matches is None:
        matches = _python_walk_files_with(asset_id, workspace)
    return matches


# ---------------------------------------------------------------------------
# Markdown rewrite policy
# ---------------------------------------------------------------------------


def _rewrite_asset_components(content: str, asset_id: str) -> tuple[str, int]:
    """Replace ```assetComponent fenced blocks whose JSON id matches with a stub."""
    pattern = re.compile(
        r"```assetComponent\s*\n(?P<body>.*?)\n```",
        re.DOTALL,
    )
    edits = 0

    def _maybe_replace(match: re.Match[str]) -> str:
        nonlocal edits
        body = match.group("body")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            if asset_id in body:
                edits += 1
                return "> [deleted asset]"
            return match.group(0)
        if isinstance(data, dict) and str(data.get("id") or "") == asset_id:
            edits += 1
            return "> [deleted asset]"
        return match.group(0)

    new_content = pattern.sub(_maybe_replace, content)
    return new_content, edits


def _rewrite_typed_links(content: str, asset_id: str) -> tuple[str, int]:
    """Rewrite ``[label](type:UUID)`` to ``label [deleted]`` for known types."""
    types = "|".join(_ASSET_LINK_TYPES)
    pattern = re.compile(
        rf"\[(?P<label>[^\]]*)\]\((?:{types}):"
        + re.escape(asset_id)
        + r"\)"
    )
    edits = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal edits
        edits += 1
        label = match.group("label").strip()
        return f"{label} [deleted]" if label else "[deleted]"

    return pattern.sub(_replace, content), edits


def _replace_bare_uuid(content: str, asset_id: str) -> tuple[str, int]:
    """Replace any remaining bare occurrences of the UUID with ``[deleted]``."""
    if asset_id not in content:
        return content, 0
    pattern = re.compile(r"(?<![0-9a-fA-F-])" + re.escape(asset_id) + r"(?![0-9a-fA-F-])")
    new_content, count = pattern.subn("[deleted]", content)
    return new_content, count


def rewrite_markdown(content: str, asset_id: str) -> tuple[str, int]:
    """Apply the full rewrite policy to a markdown body.

    Order matters: asset-component blocks first (so the JSON inside doesn't get
    chewed by typed-link rewrites), then typed links, then bare UUID fallback.
    """
    edits = 0
    content, n = _rewrite_asset_components(content, asset_id)
    edits += n
    content, n = _rewrite_typed_links(content, asset_id)
    edits += n
    content, n = _replace_bare_uuid(content, asset_id)
    edits += n
    return content, edits


# ---------------------------------------------------------------------------
# JSON rewrite policy (plan files)
# ---------------------------------------------------------------------------


def _strip_uuid_from_json(value, asset_id: str) -> tuple[object, int, bool]:
    """Walk a JSON value, removing/marking references to ``asset_id``.

    Returns (new_value, edit_count, became_empty_array_with_id).
    The bool is set when an array dropped its only entry (the deleted id).
    """
    edits = 0
    array_emptied = False
    if isinstance(value, list):
        new_list: list[object] = []
        had_id_only = len(value) > 0 and all(
            isinstance(v, str) and v == asset_id for v in value
        )
        for item in value:
            if isinstance(item, str) and item == asset_id:
                edits += 1
                continue
            new_item, n, _ = _strip_uuid_from_json(item, asset_id)
            edits += n
            new_list.append(new_item)
        if had_id_only:
            array_emptied = True
        return new_list, edits, array_emptied
    if isinstance(value, dict):
        new_dict: dict[str, object] = {}
        for k, v in value.items():
            if isinstance(v, str) and v == asset_id:
                new_dict[k] = "[deleted]"
                edits += 1
                continue
            new_v, n, _ = _strip_uuid_from_json(v, asset_id)
            edits += n
            new_dict[k] = new_v
        return new_dict, edits, False
    if isinstance(value, str) and asset_id in value:
        new_str = value.replace(asset_id, "[deleted]")
        return new_str, value.count(asset_id), False
    return value, 0, False


def rewrite_plan_json(content: str, asset_id: str) -> tuple[str, int, bool]:
    """Strip references from a plan JSON file. Returns (new_content, edits, archived)."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Fall back to raw string replacement so we don't leave broken refs.
        new_content, edits = _replace_bare_uuid(content, asset_id)
        return new_content, edits, False

    new_data, edits, _ = _strip_uuid_from_json(data, asset_id)
    archived = False
    if isinstance(new_data, dict):
        # If the plan's primary asset list (heuristic: any top-level "asset_ids"
        # / "assets" / "items" array) became empty due to our removals, mark
        # the plan archived rather than leaving an empty husk.
        for key in ("asset_ids", "assets", "items"):
            value = new_data.get(key)
            if isinstance(value, list) and len(value) == 0 and asset_id in content:
                if not new_data.get("archived"):
                    new_data["archived"] = True
                    archived = True
                break

    return json.dumps(new_data, indent=2), edits, archived


# ---------------------------------------------------------------------------
# Workspace sweep
# ---------------------------------------------------------------------------


def _is_plan_json(path: Path) -> bool:
    return path.suffix == ".json" and "plans" in path.parts


def _archive_plan_file(path: Path, data: dict) -> bool:
    """Move an active plan whose backing quest was deleted to ``history/``.

    Legacy-workspace hygiene: mark the cycle cancelled, write it to the
    sibling ``history/`` directory, and remove the active file. A plan whose
    own quest is gone has no platform counterpart left to track, so removing
    it entirely beats leaving a ``[deleted]`` husk in the active index.
    """
    data = dict(data)
    data["status"] = "cancelled"
    data["completed_at"] = datetime.now(timezone.utc).isoformat()
    data["quest_id"] = None

    plan_id = str(data.get("id") or path.stem)
    history_dir = path.parent.parent / "history"
    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / f"{plan_id}.json").write_text(json.dumps(data, indent=2))
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Cannot archive plan %s: %s", path, exc)
        return False
    return True


def _is_skipped_file(path: Path) -> bool:
    if path.suffix == ".jsonl":
        return True
    if path.suffix not in {".md", ".json"}:
        # Other extensions (.txt, .cif, etc.) are touched only via bare UUID
        # replacement and only if explicitly markdown-like. Keep them skipped.
        return True
    return False


def _rewrite_file(path: Path, asset_id: str) -> tuple[int, bool]:
    """Rewrite a single file in place. Returns (edit_count, archived)."""
    try:
        original = path.read_text()
    except OSError as exc:
        logger.warning("Cannot read %s: %s", path, exc)
        return 0, False

    if _is_plan_json(path):
        # When the deleted asset is the plan's own backing quest, the whole
        # plan is moot: archive the file instead of rewriting ids inside it.
        try:
            data = json.loads(original)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and str(data.get("quest_id") or "") == asset_id:
            if _archive_plan_file(path, data):
                return 1, True
            return 0, False
        new_content, edits, archived = rewrite_plan_json(original, asset_id)
    elif path.suffix == ".md":
        new_body, edits = rewrite_markdown(original, asset_id)
        if edits:
            new_content = set_frontmatter_timestamp(
                new_body, datetime.now(timezone.utc)
            )
        else:
            new_content = new_body
        archived = False
    else:
        return 0, False

    if edits == 0 and not archived:
        return 0, False
    if new_content == original:
        return 0, False

    try:
        path.write_text(new_content)
    except OSError as exc:
        logger.warning("Cannot write %s: %s", path, exc)
        return 0, False

    return edits, archived


def sweep_workspace_for_deleted_asset(
    workspace: Path,
    asset_id: str,
) -> SweepResult:
    """Mark every reference to a deleted asset under ``workspace`` as ``[deleted]``.

    Discovery uses ripgrep when available (sub-second on typical workspaces),
    falling back to a stdlib walk. Only files that match are read+rewritten.

    Does NOT touch mem0 vectors — see ``handle_asset_deleted_webhook`` for the
    full webhook flow.
    """
    result = SweepResult(asset_id=asset_id)
    if not asset_id or not workspace.exists():
        return result

    candidates = discover_files_with_asset(asset_id, workspace)
    for path in candidates:
        if _is_skipped_file(path):
            continue
        result.files_inspected.append(str(path))
        try:
            edits, archived = _rewrite_file(path, asset_id)
        except Exception as exc:  # pragma: no cover - defensive
            result.errors.append(f"{path}: {exc}")
            continue
        if edits or archived:
            result.files_rewritten.append(str(path))
            result.edits_per_file[str(path)] = edits
            if archived:
                result.plans_archived.append(str(path))

    return result


# ---------------------------------------------------------------------------
# Webhook entry point
# ---------------------------------------------------------------------------


def _team_ids_for(agent: "OuroAgent") -> list[Optional[str]]:
    teams: list[Optional[str]] = [None]  # global / unscoped pass
    teams.extend(sorted(getattr(agent, "_team_doc_stores", {}).keys()))
    return teams


def _prune_mem0(agent: "OuroAgent", asset_id: str) -> int:
    """Delete every memory whose asset_ids contains ``asset_id``."""
    backend = agent.memory
    agent_id = agent.config.agent.name
    deleted = 0
    seen_ids: set[str] = set()
    for team_id in _team_ids_for(agent):
        try:
            results = backend.find_by_asset(asset_id, agent_id=agent_id, team_id=team_id)
        except Exception as exc:
            logger.warning(
                "find_by_asset failed (team=%s, asset=%s): %s",
                team_id,
                asset_id,
                exc,
            )
            continue
        for mem in results:
            if not mem.id or mem.id in seen_ids:
                continue
            seen_ids.add(mem.id)
            try:
                backend.delete(mem.id)
                deleted += 1
            except Exception as exc:
                logger.warning("Failed to delete memory %s: %s", mem.id, exc)
    return deleted


def _summarize_sweep(asset_id: str, asset_type: str, sweep: SweepResult, mem0_deleted: int) -> str:
    """Build a one-line daily-log entry summarizing what we cleaned."""
    file_word = "file" if len(sweep.files_rewritten) == 1 else "files"
    parts = [
        f"cleaned up deleted asset {asset_type}:{asset_id} —",
        f"{mem0_deleted} mem0 vectors,",
        f"{sweep.total_edits} workspace edits across {len(sweep.files_rewritten)} {file_word}",
    ]
    if sweep.plans_archived:
        parts.append(f"({len(sweep.plans_archived)} plans archived)")
    return " ".join(parts)


async def handle_asset_deleted_webhook(
    agent: "OuroAgent", event_run: "EventRunContext"
) -> SweepResult:
    """Top-level handler for ``asset.deleted`` webhook deliveries.

    Order:
      1. mem0 prune (deterministic, indexed by asset_id)
      2. workspace sweep (regex-based mark + delink)
      3. daily-log entry summarizing the pass
    """
    asset_id = (event_run.asset_id or "").strip()
    asset_type = event_run.asset_type or "asset"

    if not asset_id:
        logger.info("asset.deleted received without an asset id; skipping")
        return SweepResult(asset_id="")

    workspace = agent.config.agent.workspace
    logger.info("asset.deleted cleanup starting: %s:%s", asset_type, asset_id)

    sweep = sweep_workspace_for_deleted_asset(workspace, asset_id)
    sweep.mem0_deleted = _prune_mem0(agent, asset_id)

    summary = _summarize_sweep(asset_id, asset_type, sweep, sweep.mem0_deleted)
    logger.info(summary)

    try:
        from ..memory.reflection import write_log

        write_log(
            workspace=workspace,
            entry_text=summary,
            doc_store=getattr(agent, "doc_store", None),
            agent_name=agent.config.agent.name,
        )
    except Exception as exc:  # pragma: no cover - logging only
        logger.warning("Failed to write asset-deleted daily log: %s", exc)

    return sweep


__all__ = [
    "SweepResult",
    "discover_files_with_asset",
    "handle_asset_deleted_webhook",
    "rewrite_markdown",
    "rewrite_plan_json",
    "sweep_workspace_for_deleted_asset",
]
