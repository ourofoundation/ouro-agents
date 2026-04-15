import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


def _load_workspace_sync_module():
    repo_root = Path(__file__).resolve().parents[1]
    package_dir = repo_root / "ouro_agents"

    if "ouro_agents" not in sys.modules:
        package = types.ModuleType("ouro_agents")
        package.__path__ = [str(package_dir)]
        sys.modules["ouro_agents"] = package

    if "ouro_agents.memory" not in sys.modules:
        memory_package = types.ModuleType("ouro_agents.memory")
        memory_package.__path__ = [str(package_dir / "memory")]
        sys.modules["ouro_agents.memory"] = memory_package

    spec = importlib.util.spec_from_file_location(
        "ouro_agents.memory.workspace_sync",
        package_dir / "memory" / "workspace_sync.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ouro_agents.memory.workspace_sync"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_workspace_sync_module = _load_workspace_sync_module()
sync_workspace = _workspace_sync_module.sync_workspace


class _FakeDocStore:
    def __init__(self, remote_content: str = "", remote_last_updated: datetime | None = None):
        self.remote_content = remote_content
        self.remote_last_updated = remote_last_updated
        self.write_calls: list[tuple[str, str]] = []

    def memory_name(self, agent_name: str) -> str:
        return f"MEMORY:{agent_name}:research"

    def read_with_meta(self, name: str):
        return types.SimpleNamespace(
            content=self.remote_content,
            last_updated=self.remote_last_updated,
            post_id="post-1",
        )

    def write(self, name: str, content_md: str) -> bool:
        self.write_calls.append((name, content_md))
        self.remote_content = content_md
        self.remote_last_updated = datetime.now(timezone.utc)
        return True


class TestWorkspaceSync(unittest.TestCase):
    def test_syncs_team_memory_and_strips_frontmatter_before_upload(self):
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            local_path = workspace / "teams" / "team-1" / "MEMORY.md"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(
                "---\n"
                "last_updated: 2026-04-14T12:00:00+00:00\n"
                "---\n"
                "## Facts\n"
                "- Local team memory\n"
            )

            store = _FakeDocStore(
                remote_content="## Facts\n- Older remote memory\n",
                remote_last_updated=datetime(2026, 4, 14, 11, 0, tzinfo=timezone.utc),
            )

            result = sync_workspace(
                workspace=workspace,
                team_doc_stores={"team-1": store},
                agent_name="hermes",
            )

            self.assertEqual(
                result.pushed,
                ["teams/team-1/MEMORY.md"],
            )
            self.assertEqual(
                store.write_calls,
                [("MEMORY:hermes:research", "## Facts\n- Local team memory")],
            )
            rewritten = local_path.read_text()
            self.assertIn("last_updated:", rewritten)
            self.assertIn("## Facts\n- Local team memory", rewritten)


if __name__ == "__main__":
    unittest.main()
