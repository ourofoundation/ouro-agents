"""Tests for protected/ path helpers and startup migration."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ouro_agents.tools.workspace_paths import (
    migrate_protected_workspace,
    protected_data,
    protected_memory,
    protected_root,
    protected_runs_db,
)


class TestProtectedPaths(unittest.TestCase):
    def test_path_helpers(self):
        with TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            self.assertEqual(protected_root(ws), ws / "protected")
            self.assertEqual(protected_data(ws), ws / "protected" / "data")
            self.assertEqual(protected_memory(ws), ws / "protected" / "memory")
            self.assertEqual(protected_runs_db(ws), ws / "protected" / "runs.db")


class TestMigrateProtectedWorkspace(unittest.TestCase):
    def test_moves_legacy_trees(self):
        with TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "data").mkdir()
            (ws / "data" / "platform_context.json").write_text("{}")
            (ws / "memory").mkdir()
            (ws / "memory" / "chroma").mkdir()
            (ws / "runs.db").write_text("sqlite")
            # Team memory must not move
            team_mem = ws / "teams" / "t1" / "memory" / "tasks"
            team_mem.mkdir(parents=True)
            (team_mem / "x.md").write_text("# task\n")

            moved = migrate_protected_workspace(ws)

            self.assertEqual(set(moved), {"data", "memory", "runs.db"})
            self.assertFalse((ws / "data").exists())
            self.assertFalse((ws / "memory").exists())
            self.assertFalse((ws / "runs.db").exists())
            self.assertTrue(
                (protected_data(ws) / "platform_context.json").is_file()
            )
            self.assertTrue((protected_memory(ws) / "chroma").is_dir())
            self.assertEqual(protected_runs_db(ws).read_text(), "sqlite")
            self.assertTrue((team_mem / "x.md").is_file())

    def test_idempotent(self):
        with TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "data").mkdir()
            (ws / "data" / "x.json").write_text("1")

            first = migrate_protected_workspace(ws)
            second = migrate_protected_workspace(ws)

            self.assertEqual(first, ["data"])
            self.assertEqual(second, [])
            self.assertTrue((protected_data(ws) / "x.json").is_file())

    def test_creates_protected_when_empty(self):
        with TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            moved = migrate_protected_workspace(ws)
            self.assertEqual(moved, [])
            self.assertTrue(protected_root(ws).is_dir())

    def test_replaces_empty_stub_memory(self):
        with TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            # Real store at legacy path
            (ws / "memory" / "chroma").mkdir(parents=True)
            big = ws / "memory" / "chroma" / "chroma.sqlite3"
            big.write_bytes(b"x" * 2_000_000)
            # Empty stub created by premature mem0 init
            stub = protected_memory(ws) / "chroma"
            stub.mkdir(parents=True)
            (stub / "chroma.sqlite3").write_bytes(b"y" * 1000)

            moved = migrate_protected_workspace(ws)

            self.assertEqual(moved, ["memory"])
            self.assertFalse((ws / "memory").exists())
            self.assertEqual(
                (protected_memory(ws) / "chroma" / "chroma.sqlite3").stat().st_size,
                2_000_000,
            )

    def test_moves_sqlite_sidecars(self):
        with TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "runs.db").write_text("main")
            (ws / "runs.db-wal").write_text("wal")
            (ws / "runs.db-shm").write_text("shm")

            migrate_protected_workspace(ws)

            self.assertEqual(protected_runs_db(ws).read_text(), "main")
            self.assertEqual(
                Path(str(protected_runs_db(ws)) + "-wal").read_text(), "wal"
            )
            self.assertEqual(
                Path(str(protected_runs_db(ws)) + "-shm").read_text(), "shm"
            )
            self.assertFalse((ws / "runs.db-wal").exists())


if __name__ == "__main__":
    unittest.main()
