import importlib.util
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory


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


class TestPythonToolWorkspaceFs(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
