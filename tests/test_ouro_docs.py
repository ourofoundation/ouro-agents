import importlib.util
import json
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
LocalDocStore = _ouro_docs_module.LocalDocStore
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

    def update(self, id, content=None, name=None):
        self.updated.append({"id": id, "content": content, "name": name})


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
            team_slug="research",
            team_name="Research",
            client=client,
            registry_path=Path(tmpdir) / "state.json",
        )

    def test_singleton_registry_hit_skips_search(self):
        name = "MEMORY:hermes:research"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "doc_registry.json"
            registry_path.write_text('{"MEMORY:hermes:research": "cached-post"}')
            client = _FakeClient(search_results=[[{"id": "wrong", "name": name}]])
            store = OuroDocStore(
                agent_name="hermes",
                org_id="org-1",
                team_id="team-1",
                team_slug="research",
                team_name="Research",
                client=client,
                registry_path=Path(tmpdir) / "state.json",
            )

            self.assertEqual(store._resolve(name), "cached-post")
            self.assertEqual(client.assets.search_calls, [])

    def test_falls_back_to_legacy_doc_registry_filename(self):
        name = "MEMORY:hermes:research"

        with TemporaryDirectory() as tmpdir:
            legacy_registry = Path(tmpdir) / "doc_registry.json"
            legacy_registry.write_text('{"MEMORY:hermes:research": "cached-post"}')
            client = _FakeClient()
            store = OuroDocStore(
                agent_name="hermes",
                org_id="org-1",
                team_id="team-1",
                team_slug="research",
                team_name="Research",
                client=client,
                registry_path=Path(tmpdir) / "state.json",
            )

            self.assertEqual(store._resolve(name), "cached-post")
            payload = json.loads((Path(tmpdir) / "state.json").read_text())
            self.assertEqual(payload["docs"][name], "cached-post")

    def test_loads_new_registry_payload_with_team_metadata(self):
        name = "MEMORY:hermes:research"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "team": {
                            "id": "team-1",
                            "name": "Research",
                            "slug": "research",
                            "org_id": "org-1",
                        },
                        "docs": {name: "cached-post"},
                    }
                )
            )
            client = _FakeClient()
            store = OuroDocStore(
                agent_name="hermes",
                org_id="org-1",
                team_id="team-1",
                team_slug="research",
                team_name="Research",
                client=client,
                registry_path=registry_path,
            )

            self.assertEqual(store._resolve(name), "cached-post")
            self.assertEqual(store.team_name, "Research")
            self.assertEqual(store.team_slug, "research")

    def test_resolve_uses_broader_exact_name_search(self):
        name = "DAILY:hermes:research:2026-04-05"
        matches = [{"id": f"other-{i}", "name": f"OTHER:{i}"} for i in range(8)]
        matches.append({"id": "daily-post", "name": name})

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[matches])
            store = self._make_store(client, tmpdir)

            self.assertTrue(store.exists(name))
            self.assertEqual(store._uuid_cache[name], "daily-post")
            self.assertGreaterEqual(client.assets.search_calls[0]["limit"], 25)

    def test_write_rechecks_lookup_before_creating(self):
        name = "DAILY:hermes:research:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(
                search_results=[
                    [],
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
        name = "DAILY:hermes:research:2026-04-05"
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

    def test_write_legacy_team_name_renames_to_canonical(self):
        legacy_name = "DAILY:hermes:2026-04-05"
        canonical_name = "DAILY:hermes:research:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(
                search_results=[
                    [],
                    [{"id": "existing-post", "name": legacy_name}],
                ]
            )
            store = self._make_store(client, tmpdir)

            ok = store.write(legacy_name, "# Daily Log 2026-04-05\n\n- 10:00 - existing")

            self.assertTrue(ok)
            self.assertEqual(client.posts.created, [])
            self.assertEqual(len(client.posts.updated), 2)
            self.assertEqual(client.posts.updated[0]["name"], canonical_name)
            self.assertEqual(client.posts.updated[1]["id"], "existing-post")
            self.assertEqual(store._uuid_cache[canonical_name], "existing-post")

    def test_create_legacy_team_memory_does_not_reuse_shared_memory_alias(self):
        legacy_name = "MEMORY:hermes"
        canonical_name = "MEMORY:hermes:research"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[[]])
            store = self._make_store(client, tmpdir)

            ok = store.write(legacy_name, "## Facts\n- Team memory")

            self.assertTrue(ok)
            self.assertEqual(client.posts.created[0]["name"], canonical_name)
            self.assertEqual(store._uuid_cache[canonical_name], "created-post")
            self.assertNotIn(legacy_name, store._uuid_cache)
            payload = json.loads((Path(tmpdir) / "state.json").read_text())
            self.assertEqual(payload["team"]["id"], "team-1")
            self.assertEqual(payload["team"]["name"], "Research")
            self.assertEqual(payload["team"]["slug"], "research")
            self.assertEqual(payload["docs"][canonical_name], "created-post")

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


class TestLocalDocStore(unittest.TestCase):
    def test_team_qualified_daily_routes_to_team_file(self):
        with TemporaryDirectory() as tmpdir:
            store = LocalDocStore(
                Path(tmpdir),
                agent_name="hermes",
                team_id="team-1",
                team_slug="research",
            )

            self.assertEqual(
                store._name_to_path("DAILY:hermes:research:2026-04-05"),
                Path(tmpdir) / "teams" / "team-1" / "daily" / "2026-04-05.md",
            )

    def test_team_qualified_heartbeat_routes_to_team_file(self):
        with TemporaryDirectory() as tmpdir:
            store = LocalDocStore(
                Path(tmpdir),
                agent_name="hermes",
                team_id="team-1",
                team_slug="research",
            )

            self.assertEqual(
                store._name_to_path("HEARTBEAT:hermes"),
                Path(tmpdir) / "teams" / "team-1" / "HEARTBEAT.md",
            )


if __name__ == "__main__":
    unittest.main()
