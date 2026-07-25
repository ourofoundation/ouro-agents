"""One-shot workspace migration: ``DAILY:`` logical keys → ``LOG:``, ``daily/`` → ``logs/``.

Runs idempotently at agent startup before doc stores load. Ouro post UUIDs are
unchanged — only local registry keys and filesystem layout move. Display titles
on Ouro are derived from the period suffix and are identical across prefixes.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .naming import LEGACY_LOG_PREFIX, LOG_PREFIX, canonical_log_name

logger = logging.getLogger(__name__)

MIGRATION_MARKER = ".log_prefix_v1"


@dataclass
class LogPrefixMigrationResult:
    skipped: bool = False
    registries_updated: int = 0
    keys_renamed: int = 0
    dirs_renamed: int = 0
    files_moved: int = 0
    errors: list[str] = field(default_factory=list)


def _rename_registry_keys(registry_path: Path) -> tuple[int, list[str]]:
    """Rewrite ``DAILY:*`` keys to ``LOG:*`` in a team ``state.json``."""
    errors: list[str] = []
    try:
        data = json.loads(registry_path.read_text())
    except Exception as exc:
        return 0, [f"{registry_path}: read failed: {exc}"]

    if not isinstance(data, dict):
        return 0, []

    docs = data.get("docs")
    if not isinstance(docs, dict):
        return 0, []

    renamed = 0
    new_docs: dict = {}
    for key, value in docs.items():
        if not isinstance(key, str):
            new_docs[key] = value
            continue
        canonical = canonical_log_name(key)
        if canonical != key:
            if canonical in new_docs:
                logger.warning(
                    "Registry %s: dropping duplicate %s (already have %s)",
                    registry_path,
                    key,
                    canonical,
                )
                continue
            renamed += 1
            new_docs[canonical] = value
        else:
            if key in new_docs:
                logger.warning(
                    "Registry %s: duplicate key %s", registry_path, key
                )
                continue
            new_docs[key] = value

    if renamed == 0:
        return 0, errors

    data["docs"] = new_docs
    try:
        registry_path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        errors.append(f"{registry_path}: write failed: {exc}")
        return 0, errors

    return renamed, errors


def _merge_log_dir(src: Path, dest: Path) -> int:
    """Move ``src`` log files into ``dest``, skipping names already in ``dest``."""
    if not src.is_dir():
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in sorted(src.iterdir()):
        if not item.is_file():
            continue
        target = dest / item.name
        if target.exists():
            continue
        shutil.move(str(item), str(target))
        moved += 1

    try:
        if src.is_dir() and not any(src.iterdir()):
            src.rmdir()
    except OSError:
        pass

    return moved


def _migrate_log_directory(src: Path, dest: Path) -> tuple[int, int]:
    """Move files from legacy ``daily/`` into ``logs/``. Returns (dirs, files)."""
    if not src.is_dir():
        return 0, 0
    if src.resolve() == dest.resolve():
        return 0, 0

    files_moved = _merge_log_dir(src, dest)
    dirs_renamed = 0
    if src.is_dir() and not any(src.iterdir()):
        try:
            src.rmdir()
            dirs_renamed = 1
        except OSError:
            pass

    return dirs_renamed, files_moved


def _workspace_needs_migration(workspace: Path) -> bool:
    teams_root = workspace / "teams"
    if teams_root.is_dir():
        for team_dir in teams_root.iterdir():
            if not team_dir.is_dir():
                continue
            registry = team_dir / "state.json"
            if registry.exists():
                try:
                    data = json.loads(registry.read_text())
                    docs = data.get("docs") if isinstance(data, dict) else {}
                    if isinstance(docs, dict):
                        for key in docs:
                            if isinstance(key, str) and key.startswith(
                                f"{LEGACY_LOG_PREFIX}:"
                            ):
                                return True
                except Exception:
                    return True
            if (team_dir / "daily").is_dir():
                return True

    if (workspace / "shared" / "daily").is_dir():
        return True

    return False


def migrate_log_prefix_workspace(
    workspace: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> LogPrefixMigrationResult:
    """Migrate legacy ``DAILY`` keys and ``daily/`` dirs to ``LOG`` / ``logs/``."""
    result = LogPrefixMigrationResult()
    workspace = workspace.resolve()
    from ..tools.workspace_paths import protected_data

    marker = protected_data(workspace) / MIGRATION_MARKER

    if marker.exists() and not force and not _workspace_needs_migration(workspace):
        result.skipped = True
        return result

    if dry_run:
        if _workspace_needs_migration(workspace):
            logger.info(
                "Dry run: workspace %s would migrate %s → %s",
                workspace,
                LEGACY_LOG_PREFIX,
                LOG_PREFIX,
            )
        else:
            result.skipped = True
        return result

    teams_root = workspace / "teams"
    if teams_root.is_dir():
        for team_dir in sorted(teams_root.iterdir()):
            if not team_dir.is_dir():
                continue

            registry = team_dir / "state.json"
            if registry.exists():
                renamed, errors = _rename_registry_keys(registry)
                result.errors.extend(errors)
                if renamed:
                    result.registries_updated += 1
                    result.keys_renamed += renamed

            daily_dir = team_dir / "daily"
            logs_dir = team_dir / "logs"
            dirs, files = _migrate_log_directory(daily_dir, logs_dir)
            result.dirs_renamed += dirs
            result.files_moved += files

    shared_daily = workspace / "shared" / "daily"
    shared_logs = workspace / "shared" / "logs"
    dirs, files = _migrate_log_directory(shared_daily, shared_logs)
    result.dirs_renamed += dirs
    result.files_moved += files

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "version": 1,
                "from_prefix": LEGACY_LOG_PREFIX,
                "to_prefix": LOG_PREFIX,
                "keys_renamed": result.keys_renamed,
                "files_moved": result.files_moved,
            },
            indent=2,
        )
    )

    if result.keys_renamed or result.files_moved or result.dirs_renamed:
        logger.info(
            "Log prefix migration complete: %d registry keys, %d files, %d dirs",
            result.keys_renamed,
            result.files_moved,
            result.dirs_renamed,
        )
    elif not result.skipped:
        logger.debug("Log prefix migration: nothing to change")

    return result
