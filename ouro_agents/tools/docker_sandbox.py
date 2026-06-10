"""Docker-backed Python execution session for ``run_python``."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..config import SandboxConfig

logger = logging.getLogger(__name__)


@dataclass
class PythonExecutionResult:
    """Small result object matching the attributes used from LocalPythonExecutor."""

    logs: str = ""
    output: Any = None
    stderr: str = ""


@dataclass
class ShellExecutionResult:
    """Captured shell command result from the Docker sandbox."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False


_WORKER_CODE = r"""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import traceback

WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/workspace")
os.chdir(WORKSPACE_ROOT)

_globals = {
    "__name__": "__main__",
    "WORKSPACE_ROOT": WORKSPACE_ROOT,
}
_ouro_client = None


def get_ouro_client():
    global _ouro_client
    if _ouro_client is None:
        from ouro import Ouro

        _ouro_client = Ouro()
    return _ouro_client


_globals["get_ouro_client"] = get_ouro_client


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def _truncate(value, limit):
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated to {limit} chars ..."


def _execute(code, max_output_chars):
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = None
    try:
        tree = ast.parse(code, mode="exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                prefix = ast.Module(body=tree.body[:-1], type_ignores=[])
                expression = ast.Expression(body=tree.body[-1].value)
                ast.fix_missing_locations(prefix)
                ast.fix_missing_locations(expression)
                if prefix.body:
                    exec(compile(prefix, "<run_python>", "exec"), _globals, _globals)
                result = eval(compile(expression, "<run_python>", "eval"), _globals, _globals)
            else:
                exec(compile(tree, "<run_python>", "exec"), _globals, _globals)
        return {
            "ok": True,
            "stdout": _truncate(stdout.getvalue(), max_output_chars),
            "stderr": _truncate(stderr.getvalue(), max_output_chars),
            "result": _json_safe(result),
        }
    except BaseException as exc:
        return {
            "ok": False,
            "stdout": _truncate(stdout.getvalue(), max_output_chars),
            "stderr": _truncate(stderr.getvalue(), max_output_chars),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": _truncate(traceback.format_exc(), max_output_chars),
        }


def _execute_shell(command, max_output_chars, timeout_seconds):
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/sh",
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "ok": True,
            "stdout": _truncate(completed.stdout, max_output_chars),
            "stderr": _truncate(completed.stderr, max_output_chars),
            "exit_code": completed.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": True,
            "stdout": _truncate(exc.stdout or "", max_output_chars),
            "stderr": _truncate(exc.stderr or "", max_output_chars),
            "exit_code": None,
            "timed_out": True,
        }
    except BaseException as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": _truncate(traceback.format_exc(), max_output_chars),
        }


print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    try:
        request = json.loads(line)
        request_id = request.get("id")
        if request.get("kind") == "shell":
            response = _execute_shell(
                request.get("command", ""),
                int(request.get("max_output_chars", 50000)),
                int(request.get("timeout_seconds", 30)),
            )
        else:
            response = _execute(
                request.get("code", ""),
                int(request.get("max_output_chars", 50000)),
            )
        response["id"] = request_id
        print(json.dumps(response), flush=True)
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "id": request.get("id") if "request" in locals() else None,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            ),
            flush=True,
        )
"""


def _docker_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()
    return cleaned[:120] or "ouro-sandbox"


def _package_import_code(packages: list[str]) -> str:
    return (
        "import importlib, json\n"
        "results = {}\n"
        f"packages = {packages!r}\n"
        "for pkg in packages:\n"
        "    import_target = pkg[:-2] if pkg.endswith('.*') else pkg\n"
        "    top_level = import_target.split('.')[0]\n"
        "    try:\n"
        "        mod = importlib.import_module(import_target)\n"
        "        version = getattr(mod, '__version__', None)\n"
        "        if version is None:\n"
        "            try:\n"
        "                from importlib.metadata import version as _meta_version\n"
        "                version = _meta_version(top_level)\n"
        "            except Exception:\n"
        "                version = 'unknown'\n"
        "        results[pkg] = version\n"
        "    except ImportError:\n"
        "        results[pkg] = None\n"
        "results"
    )


class DockerSandboxSession:
    """A persistent Python worker running inside a Docker container."""

    def __init__(
        self,
        *,
        config: SandboxConfig,
        workspace: Path,
        agent_name: str = "agent",
        run_id: str = "",
    ) -> None:
        self.config = config
        self.workspace = workspace.resolve()
        self.agent_name = agent_name
        self.run_id = run_id or uuid.uuid4().hex
        self.container_name = _docker_name(
            f"ouro-sandbox-{self.agent_name}-{self.run_id}-{uuid.uuid4().hex[:8]}"
        )
        self._process: Optional[subprocess.Popen[str]] = None
        self._messages: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._stderr_lines: list[str] = []
        self._lock = threading.RLock()
        self._closed = False

    def __call__(self, code: str) -> PythonExecutionResult:
        return self.execute(code)

    def execute(self, code: str) -> PythonExecutionResult:
        with self._lock:
            self._ensure_started()
            assert self._process is not None and self._process.stdin is not None
            request_id = uuid.uuid4().hex
            payload = {
                "id": request_id,
                "code": code,
                "max_output_chars": self.config.max_output_chars,
            }
            try:
                self._process.stdin.write(json.dumps(payload) + "\n")
                self._process.stdin.flush()
            except BrokenPipeError as exc:
                raise RuntimeError(self._startup_error("Docker sandbox stopped")) from exc

            response = self._wait_for_response(request_id)
            if not response.get("ok"):
                stderr = response.get("stderr") or response.get("traceback") or ""
                raise RuntimeError(
                    f"{response.get('error_type', 'ExecutionError')}: "
                    f"{response.get('error', '')}\n{stderr}".strip()
                )
            return PythonExecutionResult(
                logs=str(response.get("stdout") or ""),
                stderr=str(response.get("stderr") or ""),
                output=response.get("result"),
            )

    def execute_shell(self, command: str) -> ShellExecutionResult:
        """Execute a shell command inside the sandbox container workspace."""

        with self._lock:
            self._ensure_started()
            assert self._process is not None and self._process.stdin is not None
            request_id = uuid.uuid4().hex
            payload = {
                "id": request_id,
                "kind": "shell",
                "command": command,
                "max_output_chars": self.config.max_output_chars,
                "timeout_seconds": self.config.timeout_seconds,
            }
            try:
                self._process.stdin.write(json.dumps(payload) + "\n")
                self._process.stdin.flush()
            except BrokenPipeError as exc:
                raise RuntimeError(self._startup_error("Docker sandbox stopped")) from exc

            response = self._wait_for_response(request_id)
            if not response.get("ok"):
                stderr = response.get("stderr") or response.get("traceback") or ""
                raise RuntimeError(
                    f"{response.get('error_type', 'ShellExecutionError')}: "
                    f"{response.get('error', '')}\n{stderr}".strip()
                )
            return ShellExecutionResult(
                stdout=str(response.get("stdout") or ""),
                stderr=str(response.get("stderr") or ""),
                exit_code=response.get("exit_code"),
                timed_out=bool(response.get("timed_out", False)),
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._process = None

        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

        try:
            subprocess.run(
                ["docker", "rm", "-f", self.container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            pass

    def _ensure_started(self) -> None:
        if self._closed:
            raise RuntimeError("Docker sandbox session is closed")
        if self._process is not None and self._process.poll() is None:
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        args = self._docker_run_args()
        logger.info("Starting Docker sandbox %s", self.container_name)
        try:
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Docker executable not found for sandbox mode") from exc

        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(
            target=self._read_stdout,
            args=(self._process.stdout,),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(self._process.stderr,),
            daemon=True,
        ).start()
        self._wait_until_ready()

    def _docker_run_args(self) -> list[str]:
        mount = self.config.workspace_mount
        args = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            self.container_name,
            "--workdir",
            mount,
            "--mount",
            f"type=bind,source={self.workspace},target={mount}",
            "--label",
            "ouro.sandbox=true",
            "--label",
            f"ouro.agent={self.agent_name}",
            "--label",
            f"ouro.run_id={self.run_id}",
            "--label",
            f"ouro.workspace={self.workspace}",
            "--network",
            self.config.network,
            "-e",
            f"WORKSPACE_ROOT={mount}",
            "-e",
            "PYTHONUNBUFFERED=1",
            "-e",
            "HOME=/tmp",
        ]
        if self.config.no_new_privileges:
            args += ["--security-opt", "no-new-privileges"]
        if self.config.drop_capabilities:
            args += ["--cap-drop", "ALL"]
        if self.config.memory:
            args += ["--memory", self.config.memory]
        if self.config.cpus is not None:
            args += ["--cpus", str(self.config.cpus)]
        if self.config.pids_limit is not None:
            args += ["--pids-limit", str(self.config.pids_limit)]
        user = self.config.user
        if user is None and hasattr(os, "getuid") and hasattr(os, "getgid"):
            user = f"{os.getuid()}:{os.getgid()}"
        if user:
            args += ["--user", user]
        for name in self.config.env_allowlist:
            if name in os.environ:
                args += ["-e", name]
        args += [self.config.image, "python", "-u", "-c", _WORKER_CODE]
        return args

    def _wait_until_ready(self) -> None:
        timeout = min(max(self.config.timeout_seconds, 1), 30)
        while True:
            try:
                message = self._messages.get(timeout=timeout)
            except queue.Empty as exc:
                raise RuntimeError(self._startup_error("Docker sandbox did not start")) from exc
            if message.get("type") == "ready":
                return
            self._messages.put(message)

    def _wait_for_response(self, request_id: str) -> dict[str, Any]:
        timeout = self.config.timeout_seconds
        while True:
            try:
                message = self._messages.get(timeout=timeout)
            except queue.Empty as exc:
                self.close()
                raise TimeoutError(
                    f"Docker sandbox timed out after {timeout} seconds"
                ) from exc
            if message.get("id") == request_id:
                return message

    def _read_stdout(self, stream) -> None:
        for line in stream:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("Ignoring non-protocol sandbox stdout: %s", line.rstrip())

    def _read_stderr(self, stream) -> None:
        for line in stream:
            self._stderr_lines.append(line.rstrip())

    def _startup_error(self, prefix: str) -> str:
        process = self._process
        exit_part = ""
        if process is not None and process.poll() is not None:
            exit_part = f" (exit code {process.returncode})"
        stderr = "\n".join(self._stderr_lines[-20:])
        return f"{prefix}{exit_part}: {stderr}".strip()


def validate_python_packages_in_docker(
    packages: list[str],
    *,
    config: SandboxConfig,
    workspace: Path,
    agent_name: str = "agent",
) -> dict[str, str | None]:
    """Validate package imports inside the configured Docker image."""

    if not packages:
        return {}
    session = DockerSandboxSession(
        config=config,
        workspace=workspace,
        agent_name=agent_name,
        run_id="package-validation",
    )
    try:
        result = session.execute(_package_import_code(packages))
        if isinstance(result.output, dict):
            return {
                str(k): (None if v is None else str(v))
                for k, v in result.output.items()
            }
        raise RuntimeError(f"Unexpected package validation result: {result.output!r}")
    finally:
        session.close()

