"""Sandboxed Python execution tool wrapping smolagents' LocalPythonExecutor."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from smolagents import tool
from smolagents.local_python_executor import LocalPythonExecutor

if TYPE_CHECKING:
    from ouro.client import Ouro

logger = logging.getLogger(__name__)

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
    "glob",
    "urllib.parse",
]

OURO_AUTHORIZED_IMPORTS = [
    "ouro",
    "ouro.client",
    "ouro.resources",
    "ouro.models",
    "httpx",
]


def _make_workspace_fs(workspace: Path) -> dict:
    """Create sandboxed file helpers bound to a workspace directory."""
    root = workspace.resolve()
    root_name = workspace.name  # e.g. "workspace"

    def _safe_path(path: str) -> Path:
        # Strip redundant workspace prefix the model commonly prepends
        # (e.g. "workspace/foo.md" or "./workspace/foo.md" when root is already workspace)
        clean = path
        for prefix in (f"./{root_name}/", f"{root_name}/"):
            if clean.startswith(prefix):
                clean = clean[len(prefix) :]
                break
        target = (root / clean).resolve()
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

    def append_file(path: str, content: str) -> str:
        """Append content to a file in the workspace. Creates the file and parent dirs if needed."""
        target = _safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as f:
            f.write(content)
        return f"Appended {len(content)} chars to {target}"

    def list_dir(path: str = ".") -> list[str]:
        """List files and directories. Path is relative to workspace root."""
        return sorted(
            p.name + ("/" if p.is_dir() else "") for p in _safe_path(path).iterdir()
        )

    def file_exists(path: str) -> bool:
        """Check whether a file or directory exists in the workspace."""
        return _safe_path(path).exists()

    def get_file_info(path: str) -> dict:
        """Get metadata for a file: size, modified time, type."""
        target = _safe_path(path)
        stat = target.stat()
        return {
            "name": target.name,
            "size_bytes": stat.st_size,
            "modified": stat.st_mtime,
            "is_dir": target.is_dir(),
            "is_file": target.is_file(),
        }

    def create_directory(path: str) -> str:
        """Create a directory (and parents) in the workspace."""
        target = _safe_path(path)
        target.mkdir(parents=True, exist_ok=True)
        return f"Created directory {target}"

    def move_file(src: str, dst: str) -> str:
        """Move or rename a file within the workspace."""
        src_path = _safe_path(src)
        dst_path = _safe_path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        src_path.rename(dst_path)
        return f"Moved {src} -> {dst}"

    def search_files(pattern: str, path: str = ".") -> list[str]:
        """Search for files whose content matches a substring. Returns matching relative paths."""
        start = _safe_path(path)
        hits = []
        for p in start.rglob("*"):
            if not p.is_file():
                continue
            try:
                if pattern in p.read_text(errors="ignore"):
                    hits.append(str(p.relative_to(root)))
            except Exception:
                continue
        return sorted(hits)

    def glob_files(pattern: str, path: str = ".") -> list[str]:
        """Find files matching a glob pattern (e.g. '*.csv', '**/*.json'). Returns relative paths."""
        start = _safe_path(path)
        return sorted(
            str(p.relative_to(root)) for p in start.rglob(pattern) if p.is_file()
        )

    return {
        "read_file": read_file,
        "write_file": write_file,
        "append_file": append_file,
        "list_dir": list_dir,
        "file_exists": file_exists,
        "get_file_info": get_file_info,
        "create_directory": create_directory,
        "move_file": move_file,
        "search_files": search_files,
        "glob_files": glob_files,
    }


def _make_ouro_helpers(ouro_client: "Ouro") -> dict:
    """Create a pre-authenticated ``ouro`` accessor for the sandbox.

    Returns a dict with a single ``get_ouro_client`` callable that the
    sandboxed code can use to obtain the live Ouro SDK client.
    """

    def get_ouro_client():
        """Return a pre-authenticated Ouro SDK client.

        The client is already authenticated — no API key needed.
        Use it to interact with the Ouro platform directly:

            ouro = get_ouro_client()
            results = ouro.assets.search("climate data")
            post = ouro.posts.create(title="Report", content="...", org_id="...", team_id="...")
            ds = ouro.datasets.get("<uuid>")
        """
        return ouro_client

    return {"get_ouro_client": get_ouro_client}


def make_python_tool(
    workspace: Optional[Path] = None,
    additional_authorized_imports: list[str] | None = None,
    max_print_outputs_length: int = 50_000,
    ouro_client: Optional["Ouro"] = None,
):
    authorized = DEFAULT_AUTHORIZED_IMPORTS + (additional_authorized_imports or [])

    if ouro_client is not None:
        authorized += OURO_AUTHORIZED_IMPORTS
        logger.info("Ouro SDK client injected into Python sandbox")

    fs_funcs = _make_workspace_fs(workspace) if workspace else {}

    if ouro_client is not None:
        fs_funcs.update(_make_ouro_helpers(ouro_client))

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

    ouro_docs = ""
    if ouro_client is not None:
        ouro_docs = """

        Ouro SDK (ouro-py) — direct platform access:
        - Call `get_ouro_client()` to get a pre-authenticated Ouro client (no import needed).
        - Then use the client's resources: `.posts`, `.datasets`, `.files`, `.assets`,
          `.conversations`, `.comments`, `.organizations`, `.teams`, `.users`, etc.
        - Use this for complex multi-step workflows, batch operations, or data pipelines
          where chaining multiple MCP tool calls would be cumbersome.
        - You can also `import ouro` or `import httpx` if needed.
        - Common patterns:
            ouro = get_ouro_client()
            results = ouro.assets.search("topic")
            post = ouro.posts.create(title="My Post", content="...", org_id="...", team_id="...")
            ds = ouro.datasets.get("<uuid>")
            rows = ouro.datasets.query("<uuid>", query="SELECT * FROM data LIMIT 10")
            ouro.files.upload(file_path="report.pdf", org_id="...", team_id="...")"""

    @tool
    def run_python(code: str) -> str:
        """Execute Python code in a sandboxed environment with restricted imports.

        Use for calculations, data transformation, text processing, JSON manipulation,
        or any logic that is easier to express in code than plain text.

        State persists between calls within a single run — variables defined in one
        call are available in later calls. Print output is captured alongside the result.

        Important sandbox rules:
        - Do NOT use open(), pathlib.Path, os, pandas, numpy, or other unlisted libraries.
        - Only the imports listed below are allowed. If you need filesystem access, use the helpers below instead of imports.
        - Paths for file helpers are relative to the workspace root.

        Authorized imports: json, math, statistics, datetime, re, collections,
        itertools, functools, csv, io, textwrap, hashlib, base64, urllib.parse.

        Workspace file helpers (no import needed, paths relative to workspace):
        - read_file(path) -> str: Read a file.
        - write_file(path, content) -> str: Write/overwrite a file (creates parent dirs).
        - append_file(path, content) -> str: Append to a file (creates if needed).
        - list_dir(path='.') -> list[str]: List directory contents.
        - file_exists(path) -> bool: Check if a file or directory exists.
        - get_file_info(path) -> dict: File metadata (size, modified time, type).
        - create_directory(path) -> str: Create a directory (and parents).
        - move_file(src, dst) -> str: Move or rename a file within the workspace.
        - search_files(pattern, path='.') -> list[str]: Find files whose content contains a substring.
        - glob_files(pattern, path='.') -> list[str]: Find files matching a glob pattern.

        Common patterns:
        - Read JSON: data = json.loads(read_file('data.json'))
        - Write CSV/text: write_file('out/report.csv', csv_text)
        - Check files: list_dir('.'), file_exists('foo.txt'), get_file_info('foo.txt')

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

    if ouro_docs:
        run_python.description += ouro_docs

    return run_python, executor
