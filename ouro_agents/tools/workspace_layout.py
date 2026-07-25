"""Workspace layout enforcement for agent-authored files.

Agents may read anywhere under the workspace, but writes are restricted so
artifacts land in a discoverable place instead of the root or framework dirs.
"""

from __future__ import annotations

from pathlib import Path

# Top-level dirs agents may create or write under.
ALLOWED_TOP_LEVEL_DIRS = frozenset(
    {
        "projects",
        "drafts",
        "scratch",
        "cifs",
        "teams",
        "conversations",
        "shared",
        "skills",
        "subagents",
        "debug-runs",
    }
)

# Framework-owned trees — agent code must not write here.
# ``protected/`` is the harness tree (RO in Docker). Legacy top-level
# ``data/`` and ``memory/`` stay forbidden so agents cannot recreate them.
FORBIDDEN_TOP_LEVEL_DIRS = frozenset({"protected", "data", "memory"})

_WRITE_MODE_CHARS = frozenset("wax+")


def _is_write_mode(mode: str) -> bool:
    """Return True if an open() mode string can create or modify file contents."""
    return any(ch in mode for ch in _WRITE_MODE_CHARS)


def check_workspace_write(
    path: str | Path,
    workspace_root: str | Path,
    *,
    is_dir: bool = False,
) -> Path:
    """Validate that *path* is an allowed write target under *workspace_root*.

    Returns the resolved path on success. Raises ``PermissionError`` with a
    corrective message on violation.
    """
    root = Path(workspace_root).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    resolved = target.resolve()

    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(
            f"Access denied — path escapes workspace: {path}"
        ) from exc

    parts = rel.parts
    if not parts:
        raise PermissionError(
            "Workspace layout: cannot write the workspace root itself. "
            "Put artifacts under projects/<slug>/, drafts/, or scratch/."
        )

    top = parts[0]

    if top in FORBIDDEN_TOP_LEVEL_DIRS:
        raise PermissionError(
            f"Workspace layout: refuse write under {top}/ — that directory is "
            "framework-managed. Use projects/<slug>/, drafts/, or scratch/ instead."
        )

    if len(parts) == 1:
        if is_dir:
            if top not in ALLOWED_TOP_LEVEL_DIRS:
                allowed = ", ".join(sorted(ALLOWED_TOP_LEVEL_DIRS))
                raise PermissionError(
                    f"Workspace layout: refuse new top-level directory '{top}/'. "
                    f"Allowed top-level dirs: {allowed}. "
                    "Prefer projects/<slug>/ for new efforts."
                )
            return resolved
        raise PermissionError(
            f"Workspace layout: refuse write at workspace root ({top}). "
            "Put artifacts under projects/<slug>/, drafts/, or scratch/ instead."
        )

    if top not in ALLOWED_TOP_LEVEL_DIRS:
        allowed = ", ".join(sorted(ALLOWED_TOP_LEVEL_DIRS))
        raise PermissionError(
            f"Workspace layout: refuse write under '{top}/'. "
            f"Allowed top-level dirs: {allowed}."
        )

    return resolved


def install_workspace_layout_guard(workspace_root: str | Path) -> None:
    """Monkey-patch builtins.open and pathlib.Path write APIs in-process.

    Safe to call more than once for the same root; subsequent calls are no-ops.
    Intended for the Docker sandbox worker and tests. Call
    ``uninstall_workspace_layout_guard()`` to restore originals (tests).
    """
    import builtins
    from pathlib import Path as PathCls

    root = str(Path(workspace_root).resolve())
    if getattr(builtins, "_ouro_workspace_layout_guard", None) == root:
        return
    if getattr(builtins, "_ouro_workspace_layout_guard", None) is not None:
        uninstall_workspace_layout_guard()

    _original_open = builtins.open
    _original_write_text = PathCls.write_text
    _original_write_bytes = PathCls.write_bytes
    _original_touch = PathCls.touch
    _original_mkdir = PathCls.mkdir
    _original_path_open = PathCls.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if _is_write_mode(str(mode)):
            check_workspace_write(file, root, is_dir=False)
        return _original_open(file, mode, *args, **kwargs)

    def _guarded_write_text(self, *args, **kwargs):
        check_workspace_write(self, root, is_dir=False)
        return _original_write_text(self, *args, **kwargs)

    def _guarded_write_bytes(self, *args, **kwargs):
        check_workspace_write(self, root, is_dir=False)
        return _original_write_bytes(self, *args, **kwargs)

    def _guarded_touch(self, *args, **kwargs):
        check_workspace_write(self, root, is_dir=False)
        return _original_touch(self, *args, **kwargs)

    def _guarded_mkdir(self, *args, **kwargs):
        check_workspace_write(self, root, is_dir=True)
        return _original_mkdir(self, *args, **kwargs)

    def _guarded_path_open(self, mode="r", *args, **kwargs):
        if _is_write_mode(str(mode)):
            check_workspace_write(self, root, is_dir=False)
        return _original_path_open(self, mode, *args, **kwargs)

    builtins._ouro_workspace_layout_originals = {  # type: ignore[attr-defined]
        "open": _original_open,
        "write_text": _original_write_text,
        "write_bytes": _original_write_bytes,
        "touch": _original_touch,
        "mkdir": _original_mkdir,
        "path_open": _original_path_open,
    }
    builtins.open = _guarded_open  # type: ignore[assignment]
    PathCls.write_text = _guarded_write_text  # type: ignore[method-assign]
    PathCls.write_bytes = _guarded_write_bytes  # type: ignore[method-assign]
    PathCls.touch = _guarded_touch  # type: ignore[method-assign]
    PathCls.mkdir = _guarded_mkdir  # type: ignore[method-assign]
    PathCls.open = _guarded_path_open  # type: ignore[method-assign]
    builtins._ouro_workspace_layout_guard = root  # type: ignore[attr-defined]


def uninstall_workspace_layout_guard() -> None:
    """Restore builtins.open / pathlib.Path write APIs after a test install."""
    import builtins
    from pathlib import Path as PathCls

    originals = getattr(builtins, "_ouro_workspace_layout_originals", None)
    if not originals:
        return
    builtins.open = originals["open"]  # type: ignore[assignment]
    PathCls.write_text = originals["write_text"]  # type: ignore[method-assign]
    PathCls.write_bytes = originals["write_bytes"]  # type: ignore[method-assign]
    PathCls.touch = originals["touch"]  # type: ignore[method-assign]
    PathCls.mkdir = originals["mkdir"]  # type: ignore[method-assign]
    PathCls.open = originals["path_open"]  # type: ignore[method-assign]
    delattr(builtins, "_ouro_workspace_layout_originals")
    if hasattr(builtins, "_ouro_workspace_layout_guard"):
        delattr(builtins, "_ouro_workspace_layout_guard")


# Self-contained snippet for the Docker worker (ouro_agents is not installed
# in the sandbox image). Keep in sync with the helpers above.
DOCKER_WORKER_LAYOUT_GUARD = r"""
import builtins as _ouro_builtins
from pathlib import Path as _OuroPath

_OURO_ALLOWED_TOP = frozenset({
    "projects", "drafts", "scratch", "cifs", "teams", "conversations",
    "shared", "skills", "subagents", "debug-runs",
})
_OURO_FORBIDDEN_TOP = frozenset({"protected", "data", "memory"})
_OURO_WRITE_MODE = frozenset("wax+")


def _ouro_check_write(path, workspace_root, is_dir=False):
    root = _OuroPath(workspace_root).resolve()
    target = _OuroPath(path)
    if not target.is_absolute():
        target = root / target
    resolved = target.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Access denied — path escapes workspace: {path}") from exc
    parts = rel.parts
    if not parts:
        raise PermissionError(
            "Workspace layout: cannot write the workspace root itself. "
            "Put artifacts under projects/<slug>/, drafts/, or scratch/."
        )
    top = parts[0]
    if top in _OURO_FORBIDDEN_TOP:
        raise PermissionError(
            f"Workspace layout: refuse write under {top}/ — that directory is "
            "framework-managed. Use projects/<slug>/, drafts/, or scratch/ instead."
        )
    if len(parts) == 1:
        if is_dir:
            if top not in _OURO_ALLOWED_TOP:
                allowed = ", ".join(sorted(_OURO_ALLOWED_TOP))
                raise PermissionError(
                    f"Workspace layout: refuse new top-level directory '{top}/'. "
                    f"Allowed top-level dirs: {allowed}. "
                    "Prefer projects/<slug>/ for new efforts."
                )
            return resolved
        raise PermissionError(
            f"Workspace layout: refuse write at workspace root ({top}). "
            "Put artifacts under projects/<slug>/, drafts/, or scratch/ instead."
        )
    if top not in _OURO_ALLOWED_TOP:
        allowed = ", ".join(sorted(_OURO_ALLOWED_TOP))
        raise PermissionError(
            f"Workspace layout: refuse write under '{top}/'. "
            f"Allowed top-level dirs: {allowed}."
        )
    return resolved


if getattr(_ouro_builtins, "_ouro_workspace_layout_guard", None) != WORKSPACE_ROOT:
    _ouro_orig_open = _ouro_builtins.open
    _ouro_orig_write_text = _OuroPath.write_text
    _ouro_orig_write_bytes = _OuroPath.write_bytes
    _ouro_orig_touch = _OuroPath.touch
    _ouro_orig_mkdir = _OuroPath.mkdir
    _ouro_orig_path_open = _OuroPath.open

    def _ouro_guarded_open(file, mode="r", *args, **kwargs):
        if any(ch in str(mode) for ch in _OURO_WRITE_MODE):
            _ouro_check_write(file, WORKSPACE_ROOT, is_dir=False)
        return _ouro_orig_open(file, mode, *args, **kwargs)

    def _ouro_guarded_write_text(self, *args, **kwargs):
        _ouro_check_write(self, WORKSPACE_ROOT, is_dir=False)
        return _ouro_orig_write_text(self, *args, **kwargs)

    def _ouro_guarded_write_bytes(self, *args, **kwargs):
        _ouro_check_write(self, WORKSPACE_ROOT, is_dir=False)
        return _ouro_orig_write_bytes(self, *args, **kwargs)

    def _ouro_guarded_touch(self, *args, **kwargs):
        _ouro_check_write(self, WORKSPACE_ROOT, is_dir=False)
        return _ouro_orig_touch(self, *args, **kwargs)

    def _ouro_guarded_mkdir(self, *args, **kwargs):
        _ouro_check_write(self, WORKSPACE_ROOT, is_dir=True)
        return _ouro_orig_mkdir(self, *args, **kwargs)

    def _ouro_guarded_path_open(self, mode="r", *args, **kwargs):
        if any(ch in str(mode) for ch in _OURO_WRITE_MODE):
            _ouro_check_write(self, WORKSPACE_ROOT, is_dir=False)
        return _ouro_orig_path_open(self, mode, *args, **kwargs)

    _ouro_builtins.open = _ouro_guarded_open
    _OuroPath.write_text = _ouro_guarded_write_text
    _OuroPath.write_bytes = _ouro_guarded_write_bytes
    _OuroPath.touch = _ouro_guarded_touch
    _OuroPath.mkdir = _ouro_guarded_mkdir
    _OuroPath.open = _ouro_guarded_path_open
    _ouro_builtins._ouro_workspace_layout_guard = WORKSPACE_ROOT
"""
