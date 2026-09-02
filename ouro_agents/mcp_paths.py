"""Rewrite sandbox file paths in MCP tool arguments onto the host workspace.

Resend MCP (and any other host-side MCP) reads ``filePath`` with a plain
filesystem open. Docker agents pass ``/workspace/...``, which does not exist
on the host. Ouro MCP already remaps via ``WORKSPACE_MOUNT``; this wrapper
does the same for every MCP server before the call leaves the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

# Keys whose string values are local file paths (any capitalization / _ / -).
_FILE_PATH_KEY_TOKENS = {
    "filepath",
    "contentpath",
    "datapath",
    "outputpath",
    "localpath",
    "sourcepath",
}


def _is_file_path_key(key: Optional[str]) -> bool:
    if not key:
        return False
    token = key.replace("-", "").replace("_", "").lower()
    return token in _FILE_PATH_KEY_TOKENS


def _looks_like_url(value: str) -> bool:
    scheme = urlparse(value).scheme.lower()
    return scheme in {"http", "https", "s3", "data", "cid", "file"}


def remap_mcp_path_string(
    value: str,
    *,
    workspace_root: Path,
    workspace_mount: Optional[str] = None,
    key: Optional[str] = None,
) -> str:
    """Return *value* rewritten onto *workspace_root* when it is a sandbox path.

    Absolute paths under ``workspace_mount`` always remap. Relative paths remap
    only when *key* looks like a file-path parameter (``filePath``,
    ``file_path``, …). Paths that would escape the workspace are left unchanged
    so a host-side open fails closed instead of reading outside the tree.
    """
    if not value or _looks_like_url(value):
        return value

    root = Path(workspace_root).expanduser().resolve()
    raw = Path(value).expanduser()
    mapped = raw

    mount = (workspace_mount or "").strip()
    if mount and raw.is_absolute():
        try:
            mapped = root / raw.relative_to(Path(mount))
        except ValueError:
            mapped = raw

    if mapped == raw and _is_file_path_key(key) and not raw.is_absolute():
        mapped = root / raw

    if mapped == raw:
        return value

    try:
        resolved = mapped.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return value
    return str(resolved)


def remap_mcp_value(
    value: Any,
    *,
    workspace_root: Path,
    workspace_mount: Optional[str] = None,
    key: Optional[str] = None,
) -> Any:
    """Recursively remap sandbox file paths inside MCP tool arguments."""
    if isinstance(value, dict):
        return {
            k: remap_mcp_value(
                v,
                workspace_root=workspace_root,
                workspace_mount=workspace_mount,
                key=k,
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            remap_mcp_value(
                item,
                workspace_root=workspace_root,
                workspace_mount=workspace_mount,
                key=key,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            remap_mcp_value(
                item,
                workspace_root=workspace_root,
                workspace_mount=workspace_mount,
                key=key,
            )
            for item in value
        )
    if isinstance(value, str):
        return remap_mcp_path_string(
            value,
            workspace_root=workspace_root,
            workspace_mount=workspace_mount,
            key=key,
        )
    return value


def wrap_mcp_tool_with_workspace_paths(
    tool: Any,
    *,
    workspace_root: Path | str,
    workspace_mount: Optional[str] = None,
) -> Any:
    """Wrap ``tool.forward`` so sandbox paths are rewritten on every call."""
    root = Path(workspace_root).expanduser().resolve()
    mount = (workspace_mount or "").strip() or None
    original_forward: Callable[..., Any] = tool.forward

    def remapped_forward(*args: Any, **kwargs: Any) -> Any:
        mapped_args = tuple(
            remap_mcp_value(
                arg,
                workspace_root=root,
                workspace_mount=mount,
            )
            for arg in args
        )
        mapped_kwargs = {
            name: remap_mcp_value(
                arg,
                workspace_root=root,
                workspace_mount=mount,
                key=name,
            )
            for name, arg in kwargs.items()
        }
        return original_forward(*mapped_args, **mapped_kwargs)

    tool.forward = remapped_forward  # type: ignore[method-assign]
    return tool
