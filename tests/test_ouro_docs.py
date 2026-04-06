import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


def _load_ouro_docs_module():
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
        "ouro_agents.memory.ouro_docs",
        package_dir / "memory" / "ouro_docs.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ouro_agents.memory.ouro_docs"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_ouro_docs_module = _load_ouro_docs_module()
OuroDocStore = _ouro_docs_module.OuroDocStore


class _FakeAssets:
    def __init__(self, search_results=None):
        self.search_results = list(search_results or [])
        self.search_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.search_results:
            return self.search_results.pop(0)
        return []


class _FakePosts:
    class Content:
        def __init__(self):
            self.markdown = ""

        def from_markdown(self, markdown: str) -> None:
            self.markdown = markdown

    def __init__(self):
        self.created = []
        self.updated = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="created-post")

    def update(self, *, id, content):
        self.updated.append({"id": id, "content": content})


class _FakeClient:
    def __init__(self, search_results=None):
        self.assets = _FakeAssets(search_results=search_results)
        self.posts = _FakePosts()


class TestOuroDocStore(unittest.TestCase):
    def _make_store(self, client: _FakeClient, tmpdir: str) -> OuroDocStore:
        return OuroDocStore(
            agent_name="hermes",
            org_id="org-1",
            team_id="team-1",
            client=client,
            registry_path=Path(tmpdir) / "doc_registry.json",
        )

    def test_singleton_registry_hit_skips_search(self):
        name = "MEMORY:hermes"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "doc_registry.json"
            registry_path.write_text('{"MEMORY:hermes": "cached-post"}')
            client = _FakeClient(search_results=[[{"id": "wrong", "name": name}]])
            store = OuroDocStore(
                agent_name="hermes",
                org_id="org-1",
                team_id="team-1",
                client=client,
                registry_path=registry_path,
            )

            self.assertEqual(store._resolve(name), "cached-post")
            self.assertEqual(client.assets.search_calls, [])

    def test_resolve_uses_broader_exact_name_search(self):
        name = "DAILY:hermes:2026-04-05"
        matches = [{"id": f"other-{i}", "name": f"OTHER:{i}"} for i in range(8)]
        matches.append({"id": "daily-post", "name": name})

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[matches])
            store = self._make_store(client, tmpdir)

            self.assertTrue(store.exists(name))
            self.assertEqual(store._uuid_cache[name], "daily-post")
            self.assertGreaterEqual(client.assets.search_calls[0]["limit"], 25)

    def test_write_rechecks_lookup_before_creating(self):
        name = "DAILY:hermes:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(
                search_results=[
                    [],
                    [{"id": "existing-post", "name": name}],
                ]
            )
            store = self._make_store(client, tmpdir)

            ok = store.write(name, "# Daily Log 2026-04-05\n\n- 10:00 - existing")

            self.assertTrue(ok)
            self.assertEqual(client.posts.created, [])
            self.assertEqual(len(client.posts.updated), 1)
            self.assertEqual(client.posts.updated[0]["id"], "existing-post")

    def test_singleton_ambiguous_recovery_refuses_create(self):
        name = "DAILY:hermes:2026-04-05"
        duplicates = [
            {
                "id": "older-post",
                "name": name,
                "last_updated": "2026-04-05T10:00:00+00:00",
            },
            {
                "id": "newer-post",
                "name": name,
                "last_updated": "2026-04-05T12:00:00+00:00",
            },
        ]

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[duplicates])
            store = self._make_store(client, tmpdir)

            self.assertFalse(store.write(name, "# Daily Log 2026-04-05"))
            self.assertEqual(client.posts.created, [])
            self.assertEqual(client.posts.updated, [])

    def test_non_singleton_recovery_prefers_newest_duplicate_exact_match(self):
        name = "REPORT:hermes:weekly"
        duplicates = [
            {
                "id": "older-post",
                "name": name,
                "last_updated": "2026-04-05T10:00:00+00:00",
            },
            {
                "id": "newer-post",
                "name": name,
                "last_updated": "2026-04-05T12:00:00+00:00",
            },
        ]

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[duplicates])
            store = self._make_store(client, tmpdir)

            self.assertEqual(store._resolve(name), "newer-post")


if __name__ == "__main__":
    unittest.main()
