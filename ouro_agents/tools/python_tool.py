"""Python execution tool with local and Docker sandbox backends."""

from __future__ import annotations

import importlib
import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from smolagents import tool
from smolagents.local_python_executor import LocalPythonExecutor

from .workspace_layout import check_workspace_write

if TYPE_CHECKING:
    from ..config import SandboxConfig
    from ouro.client import Ouro

logger = logging.getLogger(__name__)


def validate_python_packages(packages: list[str]) -> dict[str, str | None]:
    """Import each package and return {name: version_or_unknown_or_None}.

    Logs a warning for any package that cannot be imported. ``None`` means the
    import failed; importable packages without discoverable metadata use
    ``"unknown"`` so they are not treated as missing.
    """
    results: dict[str, str | None] = {}
    for pkg in packages:
        import_target = pkg[:-2] if pkg.endswith(".*") else pkg
        top_level = import_target.split(".")[0]
        try:
            mod = importlib.import_module(import_target)
            version = getattr(mod, "__version__", None)
            if version is None:
                try:
                    from importlib.metadata import version as _meta_version

                    version = _meta_version(top_level)
                except Exception:
                    version = "unknown"
            results[pkg] = version
            logger.info("Python package validated: %s==%s", pkg, version or "unknown")
        except ImportError:
            results[pkg] = None
            logger.warning(
                "Python package '%s' is configured but not importable — "
                "install it in the agent's Python environment to enable it",
                pkg,
            )
    return results


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
    "urllib.request",
    "urllib",
    # "open",
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
        try:
            target.relative_to(root)
        except ValueError:
            raise PermissionError(f"Access denied — path escapes workspace: {path}")
        return target

    def read_file(path: str) -> str:
        """Read a file from the workspace. Path is relative to workspace root."""
        return _safe_path(path).read_text()

    def write_file(path: str, content: str) -> str:
        """Write content to a file in the workspace. Creates parent dirs as needed."""
        target = check_workspace_write(_safe_path(path), root, is_dir=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Bypass layout guard on Path.write_text when helpers run in-process with
        # the Docker-style monkeypatch installed (tests); helpers already checked.
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to {target}"

    def append_file(path: str, content: str) -> str:
        """Append content to a file in the workspace. Creates the file and parent dirs if needed."""
        target = check_workspace_write(_safe_path(path), root, is_dir=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
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
        target = check_workspace_write(_safe_path(path), root, is_dir=True)
        target.mkdir(parents=True, exist_ok=True)
        return f"Created directory {target}"

    def move_file(src: str, dst: str) -> str:
        """Move or rename a file within the workspace."""
        src_path = _safe_path(src)
        dst_is_dir = dst.endswith(("/", "\\")) or (
            _safe_path(dst).exists() and _safe_path(dst).is_dir()
        )
        if dst_is_dir and not dst.endswith(("/", "\\")):
            # Moving into an existing directory keeps the source basename.
            dst_path = check_workspace_write(
                _safe_path(dst) / src_path.name, root, is_dir=False
            )
        else:
            dst_path = check_workspace_write(
                _safe_path(dst), root, is_dir=dst.endswith(("/", "\\"))
            )
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

    def extract_zip(zip_path: str, output_dir: str | None = None) -> dict:
        """Extract a zip file inside the workspace and return basic metadata."""
        zip_file = _safe_path(zip_path)
        destination = _safe_path(output_dir) if output_dir else zip_file.with_suffix("")
        check_workspace_write(destination, root, is_dir=True)
        destination.mkdir(parents=True, exist_ok=True)

        extracted_files = []
        destination_root = destination.resolve()
        with zipfile.ZipFile(zip_file) as archive:
            members = archive.infolist()
            for member in members:
                target = (destination_root / member.filename).resolve()
                try:
                    target.relative_to(destination_root)
                except ValueError as exc:
                    raise PermissionError(
                        f"Unsafe zip entry would escape destination: {member.filename}"
                    ) from exc
                if not member.is_dir():
                    check_workspace_write(target, root, is_dir=False)

            archive.extractall(destination_root)
            extracted_files = [
                member.filename for member in members if not member.is_dir()
            ]

        return {
            "zip_path": str(zip_file),
            "output_dir": str(destination_root),
            "file_count": len(extracted_files),
            "files": extracted_files[:200],
        }

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
        "extract_zip": extract_zip,
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
            post = ouro.posts.create(name="Report", content_markdown="...", org_id="...", team_id="...")
            ds = ouro.datasets.retrieve("<uuid>")
        """
        return ouro_client

    return {"get_ouro_client": get_ouro_client}


def make_python_tool(
    workspace: Optional[Path] = None,
    additional_authorized_imports: list[str] | None = None,
    max_print_outputs_length: int = 50_000,
    ouro_client: Optional["Ouro"] = None,
    python_packages: list[str] | None = None,
    package_versions: dict[str, str | None] | None = None,
    sandbox_config: Optional["SandboxConfig"] = None,
    agent_name: str = "agent",
    run_id: str = "",
):
    use_docker = sandbox_config is not None and sandbox_config.mode == "docker"

    if use_docker:
        from .docker_sandbox import DockerSandboxSession

        executor = DockerSandboxSession(
            config=sandbox_config,
            workspace=workspace or Path.cwd(),
            agent_name=agent_name,
            run_id=run_id,
        )
    else:
        authorized = DEFAULT_AUTHORIZED_IMPORTS + (additional_authorized_imports or [])

        if python_packages:
            authorized += python_packages

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
            post = ouro.posts.create(name="My Post", content_markdown="...", org_id="...", team_id="...")
            ds = ouro.datasets.retrieve("<uuid>")
            # datasets.query always returns a pandas.DataFrame (not list[dict])
            df = ouro.datasets.query("<uuid>")
            df = ouro.datasets.query("<uuid>", "SELECT * FROM {{table}} WHERE status = 'sent'")
            for _, row in df.iterrows():
                print(row["name"], row["status"])
        - To publish a workspace file: use MCP `ouro:create_file` with `file_path` equal to the same
          relative path you wrote under WORKSPACE_ROOT (the run_python workspace)."""

    @tool
    def run_python(code: str) -> str:
        """Execute Python code in a persistent sandbox.

        Use for calculations, data transformation, text processing, JSON manipulation,
        or any logic that is easier to express in code than plain text.

        State persists between calls within a single run — variables defined in one
        call are available in later calls. Print output is captured alongside the result.

        Args:
            code: Valid Python code to execute.
        """
        try:
            result = executor(code)
        except TimeoutError as e:
            return f"Execution error: TimeoutError: {e}"
        except Exception as e:
            return f"Execution error: {type(e).__name__}: {e}"

        parts = []
        if result.logs:
            parts.append(f"[stdout]\n{result.logs}")
        stderr = getattr(result, "stderr", "")
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if result.output is not None:
            parts.append(f"[result]\n{result.output}")
        return "\n".join(parts) if parts else "(no output)"

    if use_docker:
        run_python.description += f"""

        Docker sandbox mode:
        - Code runs inside the configured container image, not on the host.
        - The workspace is bind-mounted at `{sandbox_config.workspace_mount}` and
          the worker starts with cwd set to that directory.
        - Use normal Python APIs: `pathlib.Path`, `open()`, `shutil`, `glob`,
          `zipfile`, installed packages, and `subprocess.run(...)`.
        - Keep file reads and writes under `WORKSPACE_ROOT` / `{sandbox_config.workspace_mount}`.
        - Call saved coils programmatically with `run_coil(name, params)` (no import
          needed) — it runs coils/<name>/handler.py in-process and returns its dict.
          Use this to compose coils in code (loops, fan-out, post-processing) instead
          of many separate run_coil tool calls.
        - Per-call timeout is `{sandbox_config.timeout_seconds}` seconds. On timeout the
          worker resets and the next call starts fresh — workspace files persist,
          in-memory variables do not. For bulk platform work, write a workspace script
          and checkpoint local progress periodically when needed; dataset create/update
          automatically chunk large JSON uploads.
        - In-memory state is also discarded when the run ends, but workspace files persist.

        Common patterns:
        - Read JSON: `data = json.loads(Path("scratch/state.json").read_text())`
        - Write text: `Path("scratch/report.csv").write_text(csv_text)`
        - Shell command: `subprocess.run(["python", "--version"], capture_output=True, text=True)`
        - Layout: never write at the workspace root or under `protected/`
          (also refuses legacy `data/` / `memory/`; enforced — you will get
          PermissionError). Use `projects/`, `drafts/`, or `scratch/`.
        """
    else:
        run_python.description += """

        Local compatibility sandbox mode:
        - Do NOT use open(), pathlib.Path, os, pandas, or other unlisted libraries.
        - Only the imports listed below are allowed. If you need filesystem access, use the helpers below instead of imports.
        - Paths for file helpers are relative to the workspace root.

        Authorized imports: json, math, statistics, datetime, re, collections,
        itertools, functools, csv, io, textwrap, hashlib, base64, urllib.parse,
        plus any configured packages listed below.

        Legacy workspace file helpers (no import needed, paths relative to workspace):
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
        - extract_zip(zip_path, output_dir=None) -> dict: Extract a zip archive safely inside the workspace.

        Common patterns:
        - Read JSON: data = json.loads(read_file('scratch/state.json'))
        - Write CSV/text: write_file('scratch/report.csv', csv_text)
        - Check files: list_dir('.'), file_exists('foo.txt'), get_file_info('foo.txt')
        - Layout: never write at the workspace root or under `protected/`
          (also refuses legacy `data/` / `memory/`; enforced — you will get
          PermissionError). Use `projects/`, `drafts/`, or `scratch/`.
        """

    if ouro_docs:
        run_python.description += ouro_docs

    if package_versions:
        available = {k: v for k, v in package_versions.items() if v is not None}
        if available:
            lines = [
                f"        - {pkg[:-2]} {ver} (including submodules)"
                if pkg.endswith(".*")
                else f"        - {pkg} {ver}"
                for pkg, ver in available.items()
            ]
            pkg_docs = (
                "\n\n        Additional installed packages (import these directly):\n"
                + "\n".join(lines)
            )
            run_python.description += pkg_docs

    return run_python, executor


def make_shell_tool(executor, sandbox_config: "SandboxConfig"):
    """Create a Docker-backed shell tool bound to an existing sandbox session."""

    @tool
    def run_shell(command: str) -> str:
        """Execute a non-interactive shell command in the Docker sandbox.

        Use for inspecting files, running installed CLI tools, or short build/test
        commands that are easier to express as shell than Python. The command runs
        from the sandbox workspace directory and returns stdout, stderr, and exit
        code. It is not interactive.

        Args:
            command: Shell command to run with `/bin/sh -c` semantics.
        """
        if not command.strip():
            return "Execution error: command must not be empty"
        if not hasattr(executor, "execute_shell"):
            return "Execution error: run_shell requires Docker sandbox mode"
        try:
            result = executor.execute_shell(command)
        except Exception as e:
            return f"Execution error: {type(e).__name__}: {e}"

        parts = [f"[exit_code]\n{result.exit_code if result.exit_code is not None else 'timeout'}"]
        if result.timed_out:
            parts.append(
                f"[timeout]\nCommand exceeded {sandbox_config.timeout_seconds} seconds"
            )
        if result.stdout:
            parts.append(f"[stdout]\n{result.stdout}")
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        return "\n".join(parts)

    run_shell.description += f"""

        Docker sandbox shell mode:
        - Commands run inside `{sandbox_config.image}`, not on the host.
        - The workspace is bind-mounted at `{sandbox_config.workspace_mount}` and
          commands start from that directory.
        - The configured timeout is {sandbox_config.timeout_seconds} seconds and
          output is truncated at {sandbox_config.max_output_chars} characters.
        - Keep reads and writes under `{sandbox_config.workspace_mount}`.
        - Prefer `projects/`, `drafts/`, or `scratch/` for new files; do not write
          at the workspace root or under `protected/` (RO mount + layout guard;
          also refuses legacy `data/` / `memory/`).
        - Avoid interactive commands, long-running daemons, and commands requiring TTY input.
        """
    return run_shell


def make_code_tools(
    workspace: Optional[Path] = None,
    additional_authorized_imports: list[str] | None = None,
    max_print_outputs_length: int = 50_000,
    ouro_client: Optional["Ouro"] = None,
    python_packages: list[str] | None = None,
    package_versions: dict[str, str | None] | None = None,
    sandbox_config: Optional["SandboxConfig"] = None,
    agent_name: str = "agent",
    run_id: str = "",
):
    """Create code execution tools for the configured sandbox."""

    python_tool, executor = make_python_tool(
        workspace=workspace,
        additional_authorized_imports=additional_authorized_imports,
        max_print_outputs_length=max_print_outputs_length,
        ouro_client=ouro_client,
        python_packages=python_packages,
        package_versions=package_versions,
        sandbox_config=sandbox_config,
        agent_name=agent_name,
        run_id=run_id,
    )
    tools = [python_tool]
    if (
        sandbox_config is not None
        and sandbox_config.mode == "docker"
        and getattr(sandbox_config, "enable_shell", False)
    ):
        tools.append(make_shell_tool(executor, sandbox_config))
    return tools, executor
