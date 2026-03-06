"""Sandboxed Python execution tool wrapping smolagents' LocalPythonExecutor."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from smolagents import tool
from smolagents.local_python_executor import LocalPythonExecutor

DEFAULT_AUTHORIZED_IMPORTS = [
    "json",
    "math",
    "statistics",
    "datetime",
    "re",
    "collections",
    "itertools",
    "functools",
    "csv",
    "io",
    "textwrap",
    "hashlib",
    "base64",
    "urllib.parse",
]


def _make_workspace_fs(workspace: Path) -> dict:
    """Create sandboxed file helpers bound to a workspace directory."""
    root = workspace.resolve()

    def _safe_path(path: str) -> Path:
        target = (root / path).resolve()
        if not str(target).startswith(str(root)):
            raise PermissionError(f"Access denied — path escapes workspace: {path}")
        return target

    def read_file(path: str) -> str:
        """Read a file from the workspace. Path is relative to workspace root."""
        return _safe_path(path).read_text()

    def write_file(path: str, content: str) -> str:
        """Write content to a file in the workspace. Creates parent dirs as needed."""
        target = _safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Wrote {len(content)} chars to {target}"

    def list_dir(path: str = ".") -> list[str]:
        """List files and directories. Path is relative to workspace root."""
        return sorted(
            p.name + ("/" if p.is_dir() else "") for p in _safe_path(path).iterdir()
        )

    return {"read_file": read_file, "write_file": write_file, "list_dir": list_dir}


def make_python_tool(
    workspace: Optional[Path] = None,
    additional_authorized_imports: list[str] | None = None,
    max_print_outputs_length: int = 50_000,
):
    authorized = DEFAULT_AUTHORIZED_IMPORTS + (additional_authorized_imports or [])

    fs_funcs = _make_workspace_fs(workspace) if workspace else {}

    executor = LocalPythonExecutor(
        additional_authorized_imports=authorized,
        max_print_outputs_length=max_print_outputs_length,
        additional_functions=fs_funcs,
    )
    # Initialize static_tools (BASE_PYTHON_TOOLS + additional_functions).
    # Without this, static_tools stays None because send_tools() is only
    # called automatically when an agent manages the executor — not when
    # it's used standalone.
    executor.send_tools({})

    @tool
    def run_python(code: str) -> str:
        """Execute Python code in a sandboxed environment with restricted imports.

        Use for calculations, data transformation, text processing, JSON manipulation,
        or any logic that is easier to express in code than plain text.

        State persists between calls within a single run — variables defined in one
        call are available in later calls. Print output is captured alongside the result.

        Authorized imports: json, math, statistics, datetime, re, collections,
        itertools, functools, csv, io, textwrap, hashlib, base64, urllib.parse.

        Workspace file helpers (no import needed, paths relative to workspace):
        - read_file(path) -> str: Read a file.
        - write_file(path, content) -> str: Write a file (creates parent dirs).
        - list_dir(path='.') -> list[str]: List directory contents.

        Args:
            code: Valid Python code to execute.
        """
        try:
            result = executor(code)
        except Exception as e:
            return f"Execution error: {type(e).__name__}: {e}"

        parts = []
        if result.logs:
            parts.append(f"[stdout]\n{result.logs}")
        if result.output is not None:
            parts.append(f"[result]\n{result.output}")
        return "\n".join(parts) if parts else "(no output)"

    return run_python, executor
