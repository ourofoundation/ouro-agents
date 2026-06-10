import importlib.util
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from ouro_agents.config import SandboxConfig


def _load_python_tool_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "ouro_agents" / "tools" / "python_tool.py"
    spec = importlib.util.spec_from_file_location("ouro_agents.tools.python_tool", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ouro_agents.tools.python_tool"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_python_tool_module = _load_python_tool_module()
_make_workspace_fs = _python_tool_module._make_workspace_fs
make_code_tools = _python_tool_module.make_code_tools
make_python_tool = _python_tool_module.make_python_tool
validate_python_packages = _python_tool_module.validate_python_packages


class TestPythonToolWorkspaceFs(unittest.TestCase):
    def test_validate_python_packages_checks_dotted_import_targets(self):
        results = validate_python_packages(["xml.dom", "xml.not_a_real_module"])

        self.assertIsNotNone(results["xml.dom"])
        self.assertIsNone(results["xml.not_a_real_module"])

    def test_validate_python_packages_accepts_wildcard_import_targets(self):
        results = validate_python_packages(["xml.dom.*"])

        self.assertIsNotNone(results["xml.dom.*"])

    def test_extract_zip_unpacks_into_workspace(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            zip_path = workspace / "data" / "bundle.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("nested/file.txt", "hello world")

            helpers = _make_workspace_fs(workspace)
            result = helpers["extract_zip"]("data/bundle.zip")

            extracted_path = workspace / "data" / "bundle" / "nested" / "file.txt"
            self.assertTrue(extracted_path.exists())
            self.assertEqual(extracted_path.read_text(), "hello world")
            self.assertEqual(result["file_count"], 1)
            self.assertIn("nested/file.txt", result["files"])

    def test_extract_zip_rejects_zip_slip_entries(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            zip_path = workspace / "bundle.zip"

            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../escape.txt", "nope")

            helpers = _make_workspace_fs(workspace)

            with self.assertRaises(PermissionError):
                helpers["extract_zip"]("bundle.zip")

            self.assertFalse((workspace / "escape.txt").exists())

    def test_docker_mode_description_prefers_standard_python_apis(self):
        with TemporaryDirectory() as tmpdir:
            python_tool, _executor = make_python_tool(
                workspace=Path(tmpdir),
                sandbox_config=SandboxConfig(mode="docker"),
            )

        self.assertIn("Docker sandbox mode", python_tool.description)
        self.assertIn("pathlib.Path", python_tool.description)
        self.assertIn("subprocess.run", python_tool.description)
        self.assertNotIn("Do NOT use open()", python_tool.description)

    def test_code_tools_include_shell_only_when_enabled_in_docker_mode(self):
        with TemporaryDirectory() as tmpdir:
            tools, _executor = make_code_tools(
                workspace=Path(tmpdir),
                sandbox_config=SandboxConfig(mode="docker", enable_shell=True),
            )
            local_tools, _local_executor = make_code_tools(
                workspace=Path(tmpdir),
                sandbox_config=SandboxConfig(mode="local", enable_shell=True),
            )
            disabled_tools, _disabled_executor = make_code_tools(
                workspace=Path(tmpdir),
                sandbox_config=SandboxConfig(mode="docker", enable_shell=False),
            )

        self.assertEqual([tool.name for tool in tools], ["run_python", "run_shell"])
        self.assertEqual([tool.name for tool in local_tools], ["run_python"])
        self.assertEqual([tool.name for tool in disabled_tools], ["run_python"])
        self.assertIn("Docker sandbox shell mode", tools[1].description)

    def test_local_mode_description_keeps_legacy_helpers(self):
        with TemporaryDirectory() as tmpdir:
            python_tool, _executor = make_python_tool(workspace=Path(tmpdir))

        self.assertIn("Local compatibility sandbox mode", python_tool.description)
        self.assertIn("Legacy workspace file helpers", python_tool.description)
        self.assertIn("read_file(path)", python_tool.description)


if __name__ == "__main__":
    unittest.main()
