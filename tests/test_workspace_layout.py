"""Tests for workspace layout write enforcement."""

from __future__ import annotations

import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from ouro_agents.tools.python_tool import _make_workspace_fs
from ouro_agents.tools.workspace_layout import (
    check_workspace_write,
    install_workspace_layout_guard,
    uninstall_workspace_layout_guard,
)


class TestCheckWorkspaceWrite(unittest.TestCase):
    def test_allows_projects_drafts_scratch(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for rel in (
                "projects/novomag/out.cif",
                "drafts/email.md",
                "scratch/state.json",
                "cifs/misc/foo.cif",
                "teams/abc/memory/tasks/x.md",
            ):
                check_workspace_write(root / rel, root, is_dir=False)

    def test_rejects_root_files(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(PermissionError) as ctx:
                check_workspace_write(root / "foo.cif", root, is_dir=False)
            self.assertIn("workspace root", str(ctx.exception))

    def test_rejects_data_and_memory(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(PermissionError) as ctx:
                check_workspace_write(root / "data" / "crm.json", root, is_dir=False)
            self.assertIn("data/", str(ctx.exception))
            with self.assertRaises(PermissionError) as ctx:
                check_workspace_write(root / "memory" / "note.md", root, is_dir=False)
            self.assertIn("memory/", str(ctx.exception))

    def test_rejects_protected(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(PermissionError) as ctx:
                check_workspace_write(
                    root / "protected" / "data" / "crm.json", root, is_dir=False
                )
            self.assertIn("protected/", str(ctx.exception))
            with self.assertRaises(PermissionError) as ctx:
                check_workspace_write(
                    root / "protected" / "memory" / "note.md", root, is_dir=False
                )
            self.assertIn("protected/", str(ctx.exception))
            with self.assertRaises(PermissionError):
                check_workspace_write(root / "protected" / "runs.db", root, is_dir=False)

    def test_allows_known_top_level_dirs(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            check_workspace_write(root / "projects", root, is_dir=True)
            check_workspace_write(root / "scratch", root, is_dir=True)

    def test_rejects_unknown_top_level_dirs(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(PermissionError) as ctx:
                check_workspace_write(root / "analyses", root, is_dir=True)
            self.assertIn("top-level directory", str(ctx.exception))


class TestWorkspaceFsHelpers(unittest.TestCase):
    def test_write_file_rejects_root_and_data(self):
        with TemporaryDirectory() as tmpdir:
            helpers = _make_workspace_fs(Path(tmpdir))
            with self.assertRaises(PermissionError):
                helpers["write_file"]("root.cif", "data")
            with self.assertRaises(PermissionError):
                helpers["write_file"]("data/crm.json", "{}")
            self.assertFalse((Path(tmpdir) / "root.cif").exists())
            self.assertFalse((Path(tmpdir) / "data" / "crm.json").exists())

    def test_write_file_allows_scratch(self):
        with TemporaryDirectory() as tmpdir:
            helpers = _make_workspace_fs(Path(tmpdir))
            msg = helpers["write_file"]("scratch/state.json", '{"ok": true}')
            self.assertIn("Wrote", msg)
            self.assertEqual(
                (Path(tmpdir) / "scratch" / "state.json").read_text(),
                '{"ok": true}',
            )

    def test_extract_zip_rejects_data_destination(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            zip_path = workspace / "scratch" / "bundle.zip"
            zip_path.parent.mkdir(parents=True)
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("nested/file.txt", "hello")

            helpers = _make_workspace_fs(workspace)
            with self.assertRaises(PermissionError):
                helpers["extract_zip"]("scratch/bundle.zip", "data/out")

    def test_extract_zip_allows_scratch_destination(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            zip_path = workspace / "scratch" / "bundle.zip"
            zip_path.parent.mkdir(parents=True)
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("nested/file.txt", "hello world")

            helpers = _make_workspace_fs(workspace)
            result = helpers["extract_zip"]("scratch/bundle.zip")

            extracted = workspace / "scratch" / "bundle" / "nested" / "file.txt"
            self.assertTrue(extracted.exists())
            self.assertEqual(extracted.read_text(), "hello world")
            self.assertEqual(result["file_count"], 1)


class TestInstallGuard(unittest.TestCase):
    def tearDown(self):
        uninstall_workspace_layout_guard()

    def test_path_write_text_rejects_root(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            install_workspace_layout_guard(root)
            with self.assertRaises(PermissionError):
                (root / "bad.cif").write_text("nope")
            (root / "scratch").mkdir()
            (root / "scratch" / "ok.txt").write_text("yes")
            self.assertEqual((root / "scratch" / "ok.txt").read_text(), "yes")
            self.assertFalse((root / "bad.cif").exists())


if __name__ == "__main__":
    unittest.main()
