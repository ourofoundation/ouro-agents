"""On-disk team workspace paths: slug directories, UUID identity.

Platform team id (UUID) is the stable identity. Human slug is the preferred
directory name under ``teams/``. Legacy UUID-named dirs are migrated and
renames are applied when the platform slug changes.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .naming import is_catch_all_team_id, team_doc_key

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def preferred_team_dir_name(
    team_id: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
) -> str:
    """Directory leaf for a team: slug when available, else UUID.

    Catch-all / nil team always uses ``all``.
    """
    if is_catch_all_team_id(team_id):
        return "all"
    key = team_doc_key(
        team_slug=team_slug, team_name=team_name, team_id=team_id
    )
    if key and key != "all":
        return key
    return team_id


def read_team_identity(team_dir: Path) -> tuple[str | None, str | None]:
    """Return ``(team_id, slug)`` from ``state.json``, or ``(None, None)``."""
    registry = team_dir / "state.json"
    if not registry.is_file():
        return None, None
    try:
        data = json.loads(registry.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    team = data.get("team") if isinstance(data, dict) else None
    if not isinstance(team, dict):
        return None, None
    tid = str(team.get("id") or "").strip() or None
    slug = str(team.get("slug") or team.get("name") or "").strip() or None
    return tid, slug


def iter_team_dirs(workspace: Path) -> list[tuple[Path, str | None, str | None]]:
    """List ``(dir, team_id, slug)`` under ``teams/``.

    ``team_id``/``slug`` come from ``state.json`` when present. Legacy UUID-named
    dirs without state still yield ``(dir, dirname, None)``.
    """
    teams_root = workspace / "teams"
    if not teams_root.is_dir():
        return []
    out: list[tuple[Path, str | None, str | None]] = []
    for team_dir in sorted(teams_root.iterdir()):
        if not team_dir.is_dir():
            continue
        tid, slug = read_team_identity(team_dir)
        if tid is None and _UUID_RE.fullmatch(team_dir.name):
            tid = team_dir.name
        out.append((team_dir, tid, slug))
    return out


def find_team_dir_by_id(workspace: Path, team_id: str) -> Path | None:
    """Locate an existing team dir whose identity matches *team_id*."""
    if not team_id:
        return None
    teams_root = workspace / "teams"
    if not teams_root.is_dir():
        return None

    # Fast path: current preferred or legacy UUID name.
    for candidate_name in (
        preferred_team_dir_name(team_id),
        team_id,
        "all" if is_catch_all_team_id(team_id) else None,
    ):
        if not candidate_name:
            continue
        candidate = teams_root / candidate_name
        if not candidate.is_dir():
            continue
        tid, _ = read_team_identity(candidate)
        if tid is None and candidate.name == team_id:
            return candidate
        if tid == team_id:
            return candidate

    for team_dir, tid, _slug in iter_team_dirs(workspace):
        if tid == team_id:
            return team_dir
    return None


def find_team_dir_by_slug_or_id(workspace: Path, name: str) -> Path | None:
    """Resolve ``teams/<slug|uuid>`` to an on-disk directory."""
    text = (name or "").strip()
    if not text:
        return None
    teams_root = workspace / "teams"
    direct = teams_root / text
    if direct.is_dir():
        return direct

    if _UUID_RE.fullmatch(text) or is_catch_all_team_id(text):
        return find_team_dir_by_id(workspace, text)

    for team_dir, tid, slug in iter_team_dirs(workspace):
        if slug == text or team_dir.name == text:
            return team_dir
        if tid and preferred_team_dir_name(tid, team_slug=slug) == text:
            return team_dir
    return None


def team_workspace_dir(
    workspace: Path,
    team_id: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
) -> Path:
    """Return the on-disk team directory (existing or preferred path).

    Does not create the directory. Prefers an existing dir found by UUID,
    otherwise returns ``teams/<preferred-slug>``.
    """
    existing = find_team_dir_by_id(workspace, team_id)
    if existing is not None:
        return existing
    return workspace / "teams" / preferred_team_dir_name(
        team_id, team_slug=team_slug, team_name=team_name
    )


def ensure_team_dir(
    workspace: Path,
    team_id: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
) -> Path:
    """Resolve the team dir, renaming onto the preferred slug when needed."""
    preferred_name = preferred_team_dir_name(
        team_id, team_slug=team_slug, team_name=team_name
    )
    teams_root = workspace / "teams"
    preferred_path = teams_root / preferred_name
    existing = find_team_dir_by_id(workspace, team_id)

    if existing is None:
        return preferred_path

    if existing.resolve() == preferred_path.resolve():
        return preferred_path

    if preferred_path.exists():
        other_id, _ = read_team_identity(preferred_path)
        if other_id and other_id != team_id:
            # Slug collision with another team — keep a UUID-qualified name.
            fallback = teams_root / f"{preferred_name}-{team_id[:8]}"
            logger.warning(
                "Team dir slug collision for %s (%s); using %s",
                team_id,
                preferred_name,
                fallback.name,
            )
            if existing.resolve() != fallback.resolve():
                if fallback.exists():
                    return existing
                logger.info(
                    "Renaming team dir %s → %s (slug collision fallback)",
                    existing.name,
                    fallback.name,
                )
                existing.rename(fallback)
                return fallback
            return existing
        # Preferred path exists but belongs to us (or has no identity) —
        # leave preferred in place; abandon empty/legacy duplicate if needed.
        if existing != preferred_path:
            logger.warning(
                "Both %s and %s exist for team %s; keeping %s",
                existing.name,
                preferred_path.name,
                team_id,
                preferred_path.name,
            )
        return preferred_path

    teams_root.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Renaming team dir %s → %s (team_id=%s)",
        existing.name,
        preferred_name,
        team_id,
    )
    existing.rename(preferred_path)
    return preferred_path


def migrate_workspace_team_dirs(workspace: Path) -> list[str]:
    """Rename legacy UUID-named team dirs to slug names. Idempotent.

    Returns a list of ``old→new`` rename descriptions.
    """
    moves: list[str] = []
    teams_root = workspace / "teams"
    if not teams_root.is_dir():
        return moves

    # Snapshot first — renaming while iterating is unsafe.
    entries = list(iter_team_dirs(workspace))
    for team_dir, tid, slug in entries:
        if not tid:
            continue
        preferred = preferred_team_dir_name(tid, team_slug=slug)
        if team_dir.name == preferred:
            continue
        try:
            new_path = ensure_team_dir(
                workspace, tid, team_slug=slug, team_name=slug
            )
        except OSError as exc:
            logger.warning(
                "Failed to migrate team dir %s: %s", team_dir.name, exc
            )
            continue
        if new_path.name != team_dir.name:
            moves.append(f"{team_dir.name}→{new_path.name}")
    return moves


def rewrite_teams_relative_path(workspace: Path, relative_path: str) -> str | None:
    """Map ``teams/<slug|uuid>/...`` to the on-disk relative path, or None."""
    text = (relative_path or "").strip().lstrip("./")
    parts = Path(text).parts
    if len(parts) < 2 or parts[0] != "teams":
        return text or None
    team_key = parts[1]
    team_dir = find_team_dir_by_slug_or_id(workspace, team_key)
    if team_dir is None:
        return text
    rest = Path(*parts[2:]) if len(parts) > 2 else Path()
    resolved = team_dir / rest if parts[2:] else team_dir
    try:
        return str(resolved.relative_to(workspace.resolve()).as_posix())
    except ValueError:
        try:
            return str(resolved.relative_to(workspace).as_posix())
        except ValueError:
            return text


def public_team_relpath(workspace: Path, path: Path) -> str:
    """Workspace-relative path using the slug dir name when known."""
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        try:
            rel = path.relative_to(workspace)
        except ValueError:
            return str(path)
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "teams":
        team_dir = workspace / "teams" / parts[1]
        tid, slug = read_team_identity(team_dir)
        if tid:
            leaf = preferred_team_dir_name(tid, team_slug=slug)
            return str(Path("teams", leaf, *parts[2:]).as_posix())
    return rel.as_posix()
