"""Reversible git snapshots for agentic dream runs.

Dream only commits paths inside the configured agent workspace and never
pushes.  When the workspace is not inside a git repository, a filesystem
snapshot is used instead.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..tools.workspace_paths import protected_data


@dataclass(frozen=True)
class DreamSnapshot:
    kind: str
    ref: str
    changed: bool = False


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def find_git_root(workspace: Path) -> Path | None:
    """Return the repository root containing *workspace*, if any."""
    try:
        result = _run_git(workspace, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.CalledProcessError):
        return None
    root = Path(result.stdout.strip()).resolve()
    try:
        workspace.resolve().relative_to(root)
    except ValueError:
        return None
    return root


def _workspace_pathspec(repo: Path, workspace: Path) -> str:
    return workspace.resolve().relative_to(repo.resolve()).as_posix()


def _copy_fallback_snapshot(workspace: Path, label: str) -> DreamSnapshot:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
    destination = protected_data(workspace) / "dream_snapshots" / f"{timestamp}-{safe_label}"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "NOTES.md", "HEARTBEAT.md"):
        source = workspace / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    skills = workspace / "skills"
    if skills.is_dir():
        shutil.copytree(skills, destination / "skills", dirs_exist_ok=True)
    return DreamSnapshot(kind="files", ref=str(destination), changed=True)


def snapshot(workspace: Path, label: str, *, agent_name: str) -> DreamSnapshot:
    """Commit workspace-local changes, or copy them when git is unavailable."""
    workspace = workspace.resolve()
    repo = find_git_root(workspace)
    if repo is None:
        return _copy_fallback_snapshot(workspace, label)

    pathspec = _workspace_pathspec(repo, workspace)
    before = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    status = _run_git(
        repo, "status", "--porcelain", "--untracked-files=all", "--", pathspec
    ).stdout
    if not status.strip():
        return DreamSnapshot(kind="git", ref=before, changed=False)

    _run_git(repo, "add", "--", pathspec)
    message = f"dream({agent_name}): {label}"
    # --only prevents unrelated, previously staged paths from entering the
    # dream commit.
    committed = _run_git(
        repo,
        "-c",
        "user.name=Ouro Dream",
        "-c",
        f"user.email={agent_name}@ouro.local",
        "commit",
        "--only",
        "-m",
        message,
        "--",
        pathspec,
        check=False,
    )
    if committed.returncode != 0:
        # Do not leave the workspace paths staged if a hook or git policy
        # rejected the snapshot.
        _run_git(repo, "reset", "--quiet", "--", pathspec, check=False)
        raise RuntimeError(committed.stderr.strip() or committed.stdout.strip())
    after = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    return DreamSnapshot(kind="git", ref=after, changed=after != before)


def diff_snapshots(
    workspace: Path,
    before: DreamSnapshot,
    after: DreamSnapshot,
) -> str:
    """Return a reviewable diff between two compatible snapshots."""
    if before.kind != "git" or after.kind != "git":
        return ""
    repo = find_git_root(workspace)
    if repo is None or before.ref == after.ref:
        return ""
    pathspec = _workspace_pathspec(repo, workspace)
    return _run_git(
        repo,
        "diff",
        "--no-ext-diff",
        before.ref,
        after.ref,
        "--",
        pathspec,
    ).stdout
