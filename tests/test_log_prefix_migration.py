import importlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def _load_migration_module():
    repo_root = Path(__file__).resolve().parents[1]
    package_dir = repo_root / "ouro_agents"

    if "ouro_agents" not in sys.modules:
        package = types.ModuleType("ouro_agents")
        package.__path__ = [str(package_dir)]
        sys.modules["ouro_agents"] = package

    if "ouro_agents.memory" not in sys.modules:
        memory_spec = importlib.util.spec_from_file_location(
            "ouro_agents.memory",
            package_dir / "memory" / "__init__.py",
            submodule_search_locations=[str(package_dir / "memory")],
        )
        memory_package = importlib.util.module_from_spec(memory_spec)
        sys.modules["ouro_agents.memory"] = memory_package
        assert memory_spec and memory_spec.loader
        memory_spec.loader.exec_module(memory_package)

    spec = importlib.util.spec_from_file_location(
        "ouro_agents.memory.log_prefix_migration",
        package_dir / "memory" / "log_prefix_migration.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ouro_agents.memory.log_prefix_migration"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_migration = _load_migration_module()
migrate_log_prefix_workspace = _migration.migrate_log_prefix_workspace


class TestLogPrefixMigration(unittest.TestCase):
    def test_migrates_registry_keys_and_daily_directory(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            team_dir = root / "teams" / "team-1"
            daily_dir = team_dir / "daily"
            daily_dir.mkdir(parents=True)
            (daily_dir / "2026-04-05.md").write_text("# log\n\n- entry\n")

            registry = team_dir / "state.json"
            registry.write_text(
                json.dumps(
                    {
                        "team": {"id": "team-1"},
                        "docs": {
                            "DAILY:hermes:research:2026-04-05": {
                                "uuid": "post-1",
                                "owned": True,
                            }
                        },
                    }
                )
            )

            result = migrate_log_prefix_workspace(root)

            self.assertFalse(result.skipped)
            self.assertEqual(result.keys_renamed, 1)
            self.assertEqual(result.files_moved, 1)
            self.assertFalse(daily_dir.exists())
            self.assertTrue((team_dir / "logs" / "2026-04-05.md").exists())

            data = json.loads(registry.read_text())
            self.assertIn("LOG:hermes:research:2026-04-05", data["docs"])
            self.assertNotIn("DAILY:hermes:research:2026-04-05", data["docs"])

    def test_second_run_is_skipped_when_clean(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            migrate_log_prefix_workspace(root)
            result = migrate_log_prefix_workspace(root)
            self.assertTrue(result.skipped)
