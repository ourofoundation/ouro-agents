"""Canonical paths for harness-owned workspace state under ``protected/``.

The Docker sandbox bind-mounts ``protected/`` read-only. Agent code may read
these paths but must not write them (layout guard + RO mount).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

PROTECTED_DIRNAME = "protected"


def protected_root(workspace: Path | str) -> Path:
    return Path(workspace) / PROTECTED_DIRNAME


def protected_data(workspace: Path | str) -> Path:
    return protected_root(workspace) / "data"


def protected_memory(workspace: Path | str) -> Path:
    return protected_root(workspace) / "memory"


def protected_runs_db(workspace: Path | str) -> Path:
    return protected_root(workspace) / "runs.db"


def ensure_protected_dir(workspace: Path | str) -> Path:
    """Create ``protected/`` if missing; return its path."""
    root = protected_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _is_empty_or_stub_dest(dest: Path, src: Path) -> bool:
    """True when *dest* looks like an empty mkdir stub vs a real *src* tree.

    Used to recover from migrate-after-create races: an empty Chroma dir at
    ``protected/memory`` should not block moving the real store.
    """
    if not dest.exists():
        return True
    if dest.is_file():
        return False
    if not src.is_dir():
        return False
    dest_size = _dir_size_bytes(dest)
    src_size = _dir_size_bytes(src)
    # Dest is a stub if it has little content and src is clearly larger.
    return dest_size < 1_000_000 and src_size > dest_size * 4


def _move_if_needed(src: Path, dest: Path, *, label: str) -> bool:
    """Move *src* to *dest* when src exists and dest does not (or is a stub)."""
    if not src.exists():
        return False
    if dest.exists():
        if not _is_empty_or_stub_dest(dest, src):
            logger.warning(
                "Protected migration: skip %s — both %s and %s exist",
                label,
                src,
                dest,
            )
            return False
        logger.warning(
            "Protected migration: replacing stub %s with %s",
            dest,
            src,
        )
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    logger.info("Protected migration: moved %s → %s", src, dest)
    return True


def _move_sqlite_sidecars(src_db: Path, dest_db: Path) -> None:
    """Move ``-wal`` / ``-shm`` next to a relocated SQLite database."""
    for suffix in ("-wal", "-shm"):
        src_side = Path(str(src_db) + suffix)
        dest_side = Path(str(dest_db) + suffix)
        if not src_side.exists():
            continue
        if dest_side.exists():
            logger.warning(
                "Protected migration: skip sqlite sidecar %s — dest exists",
                src_side.name,
            )
            continue
        shutil.move(str(src_side), str(dest_side))
        logger.info("Protected migration: moved %s → %s", src_side, dest_side)


def migrate_protected_workspace(workspace: Path | str) -> list[str]:
    """Move legacy harness paths into ``protected/``. Idempotent.

    Migrates:
    - ``data/`` → ``protected/data/``
    - top-level ``memory/`` → ``protected/memory/`` (not ``teams/*/memory/``)
    - ``runs.db`` (+ ``-wal``/``-shm``) → ``protected/runs.db``

    Returns labels of items that were moved.
    """
    ws = Path(workspace)
    ensure_protected_dir(ws)
    moved: list[str] = []

    if _move_if_needed(ws / "data", protected_data(ws), label="data/"):
        moved.append("data")
    if _move_if_needed(ws / "memory", protected_memory(ws), label="memory/"):
        moved.append("memory")

    runs_src = ws / "runs.db"
    runs_dest = protected_runs_db(ws)
    if _move_if_needed(runs_src, runs_dest, label="runs.db"):
        moved.append("runs.db")
        _move_sqlite_sidecars(runs_src, runs_dest)
    else:
        # Main db already at dest (or absent); still collect orphaned sidecars.
        _move_sqlite_sidecars(runs_src, runs_dest)

    return moved
