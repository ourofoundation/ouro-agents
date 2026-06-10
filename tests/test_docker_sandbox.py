import json
import os
import shutil
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ouro_agents.config import SandboxConfig
from ouro_agents.tools import docker_sandbox
from ouro_agents.tools.docker_sandbox import (
    DockerSandboxSession,
    PythonExecutionResult,
    validate_python_packages_in_docker,
)


class TestDockerSandboxSession(unittest.TestCase):
    def test_docker_run_args_mount_only_workspace_and_include_limits(self):
        with TemporaryDirectory() as tmpdir:
            config = SandboxConfig(
                mode="docker",
                image="test-image:latest",
                workspace_mount="/workspace",
                network="none",
                memory="512m",
                cpus=0.5,
                pids_limit=64,
                timeout_seconds=5,
                env_allowlist=["OURO_API_KEY", "SHOULD_NOT_PASS"],
                user="1000:1000",
            )
            session = DockerSandboxSession(
                config=config,
                workspace=Path(tmpdir),
                agent_name="test-agent",
                run_id="run-1",
            )

            with patch.dict("os.environ", {"OURO_API_KEY": "secret"}, clear=True):
                args = session._docker_run_args()

            mount_value = f"type=bind,source={Path(tmpdir).resolve()},target=/workspace"
            self.assertEqual(args.count("--mount"), 1)
            self.assertIn(mount_value, args)
            self.assertIn("--security-opt", args)
            self.assertIn("no-new-privileges", args)
            self.assertIn("--cap-drop", args)
            self.assertIn("ALL", args)
            self.assertIn("--memory", args)
            self.assertIn("512m", args)
            self.assertIn("--cpus", args)
            self.assertIn("0.5", args)
            self.assertIn("--pids-limit", args)
            self.assertIn("64", args)
            self.assertIn("--network", args)
            self.assertIn("none", args)
            self.assertIn("OURO_API_KEY", args)
            self.assertNotIn("SHOULD_NOT_PASS", args)
            self.assertEqual(args[-5:-1], ["test-image:latest", "python", "-u", "-c"])

    def test_package_validation_uses_docker_session(self):
        class FakeSession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                calls.append(("init", kwargs))

            def execute(self, code):
                calls.append(("execute", code))
                return PythonExecutionResult(output={"xml.dom": "unknown"})

            def close(self):
                calls.append(("close", None))

        calls = []
        config = SandboxConfig(mode="docker", image="test-image:latest")

        with TemporaryDirectory() as tmpdir:
            with patch.object(docker_sandbox, "DockerSandboxSession", FakeSession):
                result = validate_python_packages_in_docker(
                    ["xml.dom"],
                    config=config,
                    workspace=Path(tmpdir),
                    agent_name="test-agent",
                )

        self.assertEqual(result, {"xml.dom": "unknown"})
        self.assertEqual(calls[0][0], "init")
        self.assertEqual(calls[1][0], "execute")
        self.assertIn("importlib.import_module", calls[1][1])
        self.assertEqual(calls[2][0], "close")

    def test_execute_shell_sends_shell_request_and_parses_result(self):
        class FakeStdin:
            def __init__(self):
                self.writes = []

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

        with TemporaryDirectory() as tmpdir:
            config = SandboxConfig(
                mode="docker",
                timeout_seconds=12,
                max_output_chars=1234,
            )
            session = DockerSandboxSession(
                config=config,
                workspace=Path(tmpdir),
                agent_name="test-agent",
                run_id="run-1",
            )
            fake_stdin = FakeStdin()
            session._process = SimpleNamespace(stdin=fake_stdin)

            with (
                patch.object(session, "_ensure_started"),
                patch.object(
                    session,
                    "_wait_for_response",
                    return_value={
                        "ok": True,
                        "stdout": "hello\n",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                    },
                ),
            ):
                result = session.execute_shell("printf hello")

            payload = json.loads(fake_stdin.writes[0])
            self.assertEqual(payload["kind"], "shell")
            self.assertEqual(payload["command"], "printf hello")
            self.assertEqual(payload["timeout_seconds"], 12)
            self.assertEqual(payload["max_output_chars"], 1234)
            self.assertEqual(result.stdout, "hello\n")
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)

    @unittest.skipUnless(
        os.environ.get("RUN_DOCKER_SANDBOX_TESTS") == "1" and shutil.which("docker"),
        "set RUN_DOCKER_SANDBOX_TESTS=1 with Docker available to run",
    )
    def test_real_container_writes_workspace_and_cannot_see_host_sibling(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            host_secret = root / "host-secret.txt"
            host_secret.write_text("not mounted")
            config = SandboxConfig(
                mode="docker",
                image="python:3.11-slim",
                network="none",
                timeout_seconds=10,
            )
            session = DockerSandboxSession(
                config=config,
                workspace=workspace,
                agent_name="integration",
                run_id="integration",
            )
            try:
                result = session.execute(
                    "from pathlib import Path\n"
                    "Path('created.txt').write_text('hello')\n"
                    f"Path({str(host_secret)!r}).exists()"
                )
            finally:
                session.close()

            self.assertEqual((workspace / "created.txt").read_text(), "hello")
            self.assertFalse(result.output)


if __name__ == "__main__":
    unittest.main()
