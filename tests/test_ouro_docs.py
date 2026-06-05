import importlib
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
CompositeDocStore = _ouro_docs_module.CompositeDocStore
_naming_module = importlib.import_module("ouro_agents.memory.naming")
log_doc_display_name = _naming_module.log_doc_display_name


def _registry_payload(docs: dict[str, object]) -> str:
    return json.dumps(
        {
            "team": {
                "id": "team-1",
                "name": "Research",
                "slug": "research",
                "org_id": "org-1",
            },
            "docs": docs,
        }
    )


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
        self.contents = {}
        # UUID -> Exception. When present, retrieve/update raise instead.
        self.retrieve_errors: dict[str, Exception] = {}
        self.update_errors: dict[str, Exception] = {}

    def create(self, **kwargs):
        self.created.append(kwargs)
        self.contents["created-post"] = kwargs.get("content_markdown", "")
        return SimpleNamespace(id="created-post")

    def update(self, id, content=None, name=None):
        if id in self.update_errors:
            raise self.update_errors[id]
        self.updated.append({"id": id, "content": content, "name": name})
        if content is not None:
            self.contents[id] = content.markdown

    def retrieve(self, id):
        if id in self.retrieve_errors:
            raise self.retrieve_errors[id]
        return SimpleNamespace(
            id=id,
            content=SimpleNamespace(text=self.contents.get(id, ""), data={}),
            last_updated=None,
        )


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

    def test_registry_hit_skips_search(self):
        name = "MEMORY:hermes:research"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(_registry_payload({name: "cached-post"}))
            client = _FakeClient(search_results=[[{"id": "wrong", "name": name}]])
            store = self._make_store(client, tmpdir)

            self.assertEqual(store._resolve(name), "cached-post")
            self.assertEqual(client.assets.search_calls, [])

    def test_loads_registry_payload_with_team_metadata(self):
        name = "MEMORY:hermes:research"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(_registry_payload({name: "cached-post"}))
            client = _FakeClient()
            store = self._make_store(client, tmpdir)

            self.assertEqual(store._resolve(name), "cached-post")
            self.assertEqual(store.team_name, "Research")
            self.assertEqual(store.team_slug, "research")

    def test_resolve_uses_broader_exact_name_search_for_recoverable_names(self):
        name = "REPORT:hermes:weekly"
        display_name = name
        matches = [{"id": f"other-{i}", "name": f"OTHER:{i}"} for i in range(8)]
        matches.append({"id": "report-post", "name": display_name})

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[matches])
            store = self._make_store(client, tmpdir)

            self.assertTrue(store.exists(name))
            self.assertEqual(store._uuid_cache[name], "report-post")
            self.assertGreaterEqual(client.assets.search_calls[0]["limit"], 25)
            self.assertEqual(client.assets.search_calls[0]["scope"], "personal")

    def test_daily_resolve_does_not_search_or_cache_external_post(self):
        name = "LOG:hermes:research:2026-04-05"
        display_name = "#research daily log 2026-04-05"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(
                search_results=[[{"id": "apollo-daily", "name": display_name}]]
            )
            store = self._make_store(client, tmpdir)

            self.assertFalse(store.exists(name))
            self.assertNotIn(name, store._uuid_cache)
            self.assertEqual(client.assets.search_calls, [])

    def test_write_rechecks_lookup_before_creating(self):
        name = "REPORT:hermes:weekly"
        display_name = name

        with TemporaryDirectory() as tmpdir:
            # First search misses, second search (under the write lock) finds
            # the post — simulates a race against another process creating it.
            client = _FakeClient(
                search_results=[
                    [],
                    [{"id": "existing-post", "name": display_name}],
                ]
            )
            store = self._make_store(client, tmpdir)

            ok = store.write(name, "# Daily Log 2026-04-05\n\n- 10:00 - existing")

            self.assertTrue(ok)
            self.assertEqual(client.posts.created, [])
            self.assertEqual(len(client.posts.updated), 1)
            self.assertEqual(client.posts.updated[0]["id"], "existing-post")

    def test_singleton_ambiguous_recovery_refuses_create(self):
        name = "MEMORY:hermes:research"
        display_name = name
        duplicates = [
            {
                "id": "older-post",
                "name": display_name,
                "last_updated": "2026-04-05T10:00:00+00:00",
            },
            {
                "id": "newer-post",
                "name": display_name,
                "last_updated": "2026-04-05T12:00:00+00:00",
            },
        ]

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[[], duplicates])
            store = self._make_store(client, tmpdir)

            self.assertFalse(store.write(name, "# Daily Log 2026-04-05"))
            self.assertEqual(client.posts.created, [])
            self.assertEqual(client.posts.updated, [])

    def test_create_daily_log_uses_natural_remote_name(self):
        name = "LOG:hermes:research:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[[], []])
            store = self._make_store(client, tmpdir)

            ok = store.write(name, "# Daily Log 2026-04-05")

            self.assertTrue(ok)
            self.assertEqual(
                client.posts.created[0]["name"],
                "#research daily log 2026-04-05",
            )
            self.assertEqual(store._uuid_cache[name], "created-post")
            self.assertTrue(store.is_owner(name))
            self.assertEqual(client.assets.search_calls, [])

    def test_log_doc_display_name_uses_team_hashtag(self):
        self.assertEqual(
            log_doc_display_name("LOG:hermes:permanent-magents:2026-04-05"),
            "#permanent-magents daily log 2026-04-05",
        )

    def test_append_list_item_uses_cached_id_without_search(self):
        name = "LOG:hermes:research:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "daily-post", "owned": True}})
            )
            client = _FakeClient(search_results=[[{"id": "wrong", "name": name}]])
            client.posts.contents["daily-post"] = (
                "# Daily Log 2026-04-05\n\n- 10:00 - first"
            )
            store = self._make_store(client, tmpdir)

            ok = store.append_list_item(
                name,
                "- 10:05 - second",
                initial_md="# Daily Log 2026-04-05\n\n- 10:05 - second",
            )

            self.assertTrue(ok)
            self.assertEqual(client.assets.search_calls, [])
            self.assertEqual(
                client.posts.contents["daily-post"],
                "# Daily Log 2026-04-05\n\n- 10:00 - first\n- 10:05 - second",
            )

    def test_append_list_item_ignores_non_owned_cached_daily_and_creates(self):
        name = "LOG:hermes:research:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(_registry_payload({name: "apollo-daily"}))
            client = _FakeClient(search_results=[[{"id": "wrong", "name": name}]])
            client.posts.contents["apollo-daily"] = (
                "# Daily Log 2026-04-05\n\n- 10:00 - apollo"
            )
            store = self._make_store(client, tmpdir)

            ok = store.append_list_item(
                name,
                "- 10:05 - hermes",
                initial_md="# Daily Log 2026-04-05\n\n- 10:05 - hermes",
            )

            self.assertTrue(ok)
            self.assertEqual(client.assets.search_calls, [])
            self.assertEqual(client.posts.updated, [])
            self.assertEqual(
                client.posts.created[0]["name"],
                "#research daily log 2026-04-05",
            )
            self.assertEqual(store._uuid_cache[name], "created-post")
            self.assertTrue(store.is_owner(name))

    def test_append_list_item_creates_when_id_missing(self):
        name = "LOG:hermes:research:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            # Search results would resolve, but append_list_item is cache-first
            # — a cache miss creates directly without searching.
            client = _FakeClient(search_results=[[{"id": "existing", "name": name}]])
            store = self._make_store(client, tmpdir)

            ok = store.append_list_item(
                name,
                "- 10:05 - first",
                initial_md="# Daily Log 2026-04-05\n\n- 10:05 - first",
            )

            self.assertTrue(ok)
            self.assertEqual(client.assets.search_calls, [])
            self.assertEqual(
                client.posts.created[0]["name"],
                "#research daily log 2026-04-05",
            )
            self.assertEqual(store._uuid_cache[name], "created-post")

    def test_append_list_item_without_initial_md_falls_back_to_item(self):
        name = "LOG:hermes:research:2026-04-05"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[])
            store = self._make_store(client, tmpdir)

            ok = store.append_list_item(name, "- 10:05 - first")

            self.assertTrue(ok)
            # Without initial_md, the item itself seeds the new post.
            self.assertEqual(
                client.posts.created[0]["content_markdown"],
                "- 10:05 - first",
            )

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

    def test_persisted_registry_carries_team_metadata(self):
        name = "REPORT:hermes:weekly"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[[]])
            store = self._make_store(client, tmpdir)

            self.assertTrue(store.write(name, "## Report"))
            payload = json.loads((Path(tmpdir) / "state.json").read_text())
            self.assertEqual(payload["team"]["id"], "team-1")
            self.assertEqual(payload["team"]["name"], "Research")
            self.assertEqual(payload["team"]["slug"], "research")
            self.assertEqual(
                payload["docs"][name],
                {"uuid": "created-post", "owned": True},
            )

    def test_ownership_persists_across_restart(self):
        name = "USER:user-abc"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(search_results=[[]])
            store = self._make_store(client, tmpdir)
            self.assertTrue(store.write(name, "## Profile"))
            self.assertTrue(store.is_owner(name))

            # Reload from disk — ownership must survive.
            client2 = _FakeClient(search_results=[])
            reloaded = self._make_store(client2, tmpdir)
            self.assertEqual(reloaded._uuid_cache[name], "created-post")
            self.assertTrue(reloaded.is_owner(name))

    def test_recovered_post_is_not_marked_as_owned(self):
        name = "REPORT:hermes:weekly"

        with TemporaryDirectory() as tmpdir:
            client = _FakeClient(
                search_results=[[{"id": "found-post", "name": name}]]
            )
            store = self._make_store(client, tmpdir)
            self.assertEqual(store._resolve(name), "found-post")
            self.assertFalse(store.is_owner(name))

            payload = json.loads((Path(tmpdir) / "state.json").read_text())
            self.assertEqual(payload["docs"][name], {"uuid": "found-post"})

    def test_read_with_meta_drops_stale_uuid_and_recovers_via_search(self):
        name = "MEMORY:hermes:research"
        coerce = RuntimeError("Cannot coerce the result to a single JSON object")

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "dead-uuid", "owned": True}})
            )
            client = _FakeClient(
                search_results=[[{"id": "live-uuid", "name": name}]]
            )
            client.posts.retrieve_errors["dead-uuid"] = coerce
            client.posts.contents["live-uuid"] = "## Live"
            store = self._make_store(client, tmpdir)

            result = store.read_with_meta(name)

            self.assertEqual(result.content, "## Live")
            self.assertEqual(store._uuid_cache[name], "live-uuid")
            self.assertFalse(store.is_owner(name))

            payload = json.loads((Path(tmpdir) / "state.json").read_text())
            self.assertEqual(payload["docs"][name], {"uuid": "live-uuid"})

    def test_read_with_meta_returns_empty_when_recovery_finds_nothing(self):
        name = "MEMORY:hermes:research"
        coerce = RuntimeError("Cannot coerce the result to a single JSON object")

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "dead-uuid", "owned": True}})
            )
            client = _FakeClient(search_results=[[]])
            client.posts.retrieve_errors["dead-uuid"] = coerce
            store = self._make_store(client, tmpdir)

            result = store.read_with_meta(name)

            self.assertEqual(result.content, "")
            self.assertNotIn(name, store._uuid_cache)
            self.assertFalse(store.is_owner(name))

    def test_write_drops_stale_uuid_on_permission_error_and_creates_new(self):
        name = "MEMORY:hermes:research"
        forbidden = RuntimeError("You don't have permission to update this asset")

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "dead-uuid", "owned": True}})
            )
            # After the dropped UUID, recovery searches twice (outside +
            # under the write lock) and finds nothing — so a new post is
            # minted instead of reattaching to a stranger's post.
            client = _FakeClient(search_results=[[], []])
            client.posts.update_errors["dead-uuid"] = forbidden
            store = self._make_store(client, tmpdir)

            ok = store.write(name, "## Refreshed memory")

            self.assertTrue(ok)
            self.assertEqual(len(client.posts.created), 1)
            self.assertEqual(
                client.posts.created[0]["content_markdown"], "## Refreshed memory"
            )
            self.assertEqual(store._uuid_cache[name], "created-post")
            self.assertTrue(store.is_owner(name))

    def test_write_does_not_loop_when_recovery_also_fails(self):
        name = "MEMORY:hermes:research"
        forbidden = RuntimeError("You don't have permission to update this asset")

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "dead-uuid", "owned": True}})
            )
            # Recovery search finds an exact-name match owned by someone
            # else; the second update also 403s. The retry guard must stop
            # us before we recurse forever.
            client = _FakeClient(
                search_results=[[{"id": "stranger", "name": name}]]
            )
            client.posts.update_errors["dead-uuid"] = forbidden
            client.posts.update_errors["stranger"] = forbidden
            store = self._make_store(client, tmpdir)

            ok = store.write(name, "## Doomed")

            self.assertFalse(ok)
            self.assertEqual(client.posts.created, [])

    def test_append_drops_stale_uuid_and_creates_new(self):
        name = "REPORT:hermes:weekly"
        coerce = RuntimeError("Cannot coerce the result to a single JSON object")

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "dead-uuid", "owned": True}})
            )
            client = _FakeClient(search_results=[[], []])
            client.posts.retrieve_errors["dead-uuid"] = coerce
            store = self._make_store(client, tmpdir)

            ok = store.append(name, "## Latest")

            self.assertTrue(ok)
            self.assertEqual(len(client.posts.created), 1)
            self.assertEqual(store._uuid_cache[name], "created-post")
            self.assertTrue(store.is_owner(name))

    def test_append_list_item_drops_stale_uuid_and_creates_new(self):
        name = "LOG:hermes:research:2026-04-05"
        coerce = RuntimeError("Cannot coerce the result to a single JSON object")

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "dead-uuid", "owned": True}})
            )
            client = _FakeClient(search_results=[])
            client.posts.retrieve_errors["dead-uuid"] = coerce
            store = self._make_store(client, tmpdir)

            ok = store.append_list_item(
                name,
                "- 10:05 - hermes",
                initial_md="# Daily Log\n\n- 10:05 - hermes",
            )

            self.assertTrue(ok)
            self.assertEqual(len(client.posts.created), 1)
            self.assertEqual(
                client.posts.created[0]["content_markdown"],
                "# Daily Log\n\n- 10:05 - hermes",
            )
            self.assertEqual(store._uuid_cache[name], "created-post")

    def test_unrelated_errors_do_not_drop_cached_uuid(self):
        name = "REPORT:hermes:weekly"
        boom = RuntimeError("connection reset by peer")

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                _registry_payload({name: {"uuid": "live-uuid", "owned": True}})
            )
            client = _FakeClient()
            client.posts.update_errors["live-uuid"] = boom
            store = self._make_store(client, tmpdir)

            ok = store.write(name, "## Body")

            self.assertFalse(ok)
            self.assertEqual(store._uuid_cache[name], "live-uuid")
            self.assertTrue(store.is_owner(name))

    def test_legacy_string_registry_entries_are_accepted(self):
        name = "MEMORY:hermes:research"

        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            # Pre-existing registries used a bare uuid string per entry.
            registry_path.write_text(_registry_payload({name: "legacy-uuid"}))
            client = _FakeClient()
            store = self._make_store(client, tmpdir)

            self.assertEqual(store._resolve(name), "legacy-uuid")
            # Nothing in the legacy payload says we own it.
            self.assertFalse(store.is_owner(name))


class TestLocalDocStore(unittest.TestCase):
    def test_team_qualified_log_routes_to_team_file(self):
        with TemporaryDirectory() as tmpdir:
            store = LocalDocStore(
                Path(tmpdir),
                agent_name="hermes",
                team_id="team-1",
                team_slug="research",
            )

            self.assertEqual(
                store._name_to_path("LOG:hermes:research:2026-04-05"),
                Path(tmpdir) / "teams" / "team-1" / "logs" / "2026-04-05.md",
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

    def test_shared_memory_routes_to_workspace_root(self):
        with TemporaryDirectory() as tmpdir:
            # Both team-scoped and unscoped local stores should land at the
            # same workspace-root MEMORY.md for SHARED:memory.
            scoped = LocalDocStore(
                Path(tmpdir),
                agent_name="hermes",
                team_id="team-1",
                team_slug="research",
            )
            unscoped = LocalDocStore(Path(tmpdir), agent_name="hermes")

            for store in (scoped, unscoped):
                self.assertEqual(
                    store._name_to_path("SHARED:memory"),
                    Path(tmpdir) / "MEMORY.md",
                )

    def test_no_team_routes_to_shared_layout(self):
        with TemporaryDirectory() as tmpdir:
            store = LocalDocStore(Path(tmpdir), agent_name="hermes")

            self.assertEqual(
                store._name_to_path("MEMORY:hermes"),
                Path(tmpdir) / "shared" / "memory" / "MEMORY.md",
            )
            self.assertEqual(
                store._name_to_path("LOG:hermes:2026-04-05"),
                Path(tmpdir) / "shared" / "logs" / "2026-04-05.md",
            )
            self.assertEqual(
                store._name_to_path("USER:user-123"),
                Path(tmpdir) / "shared" / "users" / "user-123.md",
            )
            self.assertEqual(
                store._name_to_path("SOUL:hermes"),
                Path(tmpdir) / "SOUL.md",
            )
            self.assertEqual(
                store._name_to_path("NOTES:hermes"),
                Path(tmpdir) / "NOTES.md",
            )

    def test_reads_legacy_daily_directory_for_log_name(self):
        with TemporaryDirectory() as tmpdir:
            store = LocalDocStore(
                Path(tmpdir),
                agent_name="hermes",
                team_id="team-1",
                team_slug="research",
            )
            legacy_dir = Path(tmpdir) / "teams" / "team-1" / "daily"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "2026-04-05.md").write_text("# Daily Log\n\n- legacy entry\n")

            name = store.log_name("hermes", "2026-04-05")
            self.assertIn("legacy entry", store.read(name))

    def test_append_list_item_merges_into_existing_list(self):
        with TemporaryDirectory() as tmpdir:
            store = LocalDocStore(Path(tmpdir), agent_name="hermes")
            name = store.log_name("hermes", "2026-04-05")
            store.write(name, "# Daily Log 2026-04-05\n\n- 10:00 — first\n")

            ok = store.append_list_item(name, "- 10:05 — second\n")

            self.assertTrue(ok)
            self.assertEqual(
                store.read(name),
                "# Daily Log 2026-04-05\n\n- 10:00 — first\n- 10:05 — second",
            )


class _FakeOuroForComposite:
    """Minimal stand-in capturing which calls reached the Ouro backend."""

    def __init__(self):
        self.reads: list[str] = []
        self.writes: list[tuple[str, str]] = []
        self.appends: list[tuple[str, str]] = []
        self.searches: list[str] = []
        self.exists_calls: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.read_comment_calls: list[str] = []
        self.is_owner_calls: list[str] = []
        self.list_item_calls: list[tuple[str, str, str | None]] = []

    def memory_name(self, agent_name=None):
        return f"MEMORY:{agent_name or 'agent'}:research"

    def log_name(self, agent_name, period):
        return f"LOG:{agent_name or 'agent'}:research:{period}"

    def read(self, name):
        self.reads.append(name)
        return f"ouro:{name}"

    def read_with_meta(self, name):
        self.reads.append(name)
        return SimpleNamespace(content=f"ouro:{name}", last_updated=None, post_id="x")

    def write(self, name, content_md):
        self.writes.append((name, content_md))
        return True

    def append(self, name, markdown):
        self.appends.append((name, markdown))
        return True

    def append_list_item(self, name, item, *, initial_md=None):
        self.list_item_calls.append((name, item, initial_md))
        return True

    def exists(self, name):
        self.exists_calls.append(name)
        return True

    def comment(self, name, content_md):
        self.comments.append((name, content_md))
        return True

    def read_comments(self, name):
        self.read_comment_calls.append(name)
        return [{"id": "c1"}]

    def is_owner(self, name):
        self.is_owner_calls.append(name)
        return False

    def search(self, query):
        self.searches.append(query)
        return [{"id": "p1", "name": query}]


class TestCompositeDocStore(unittest.TestCase):
    def _composite(self, tmpdir, *, with_ouro: bool):
        local = LocalDocStore(
            Path(tmpdir),
            agent_name="hermes",
            team_id="team-1",
            team_slug="research",
        )
        ouro = _FakeOuroForComposite() if with_ouro else None
        return CompositeDocStore(local=local, ouro=ouro), local, ouro

    def test_identity_prefixes_route_to_local(self):
        with TemporaryDirectory() as tmpdir:
            composite, local, ouro = self._composite(tmpdir, with_ouro=True)

            soul_path = local._name_to_path("SOUL:hermes")
            soul_path.parent.mkdir(parents=True, exist_ok=True)
            soul_path.write_text("local soul")

            shared_path = Path(tmpdir) / "MEMORY.md"
            shared_path.write_text("cross-team facts")

            self.assertEqual(composite.read("SOUL:hermes"), "local soul")
            self.assertEqual(composite.read("SHARED:memory"), "cross-team facts")
            self.assertTrue(composite.write("HEARTBEAT:hermes", "beat"))
            self.assertTrue(composite.write("NOTES:hermes", "note"))
            self.assertEqual(ouro.reads, [])
            self.assertEqual(ouro.writes, [])

            heartbeat_path = local._name_to_path("HEARTBEAT:hermes")
            self.assertEqual(heartbeat_path.read_text(), "beat")

    def test_non_identity_routes_to_ouro_when_available(self):
        with TemporaryDirectory() as tmpdir:
            composite, _local, ouro = self._composite(tmpdir, with_ouro=True)

            self.assertEqual(
                composite.read("MEMORY:hermes:research"), "ouro:MEMORY:hermes:research"
            )
            self.assertTrue(composite.write("MEMORY:hermes:research", "fact"))
            self.assertTrue(composite.append("LOG:hermes:research:2026-04-05", "- x"))
            self.assertTrue(composite.exists("USER:abc"))
            self.assertEqual(composite.search("query"), [{"id": "p1", "name": "query"}])

            self.assertEqual(ouro.reads, ["MEMORY:hermes:research"])
            self.assertEqual(ouro.writes, [("MEMORY:hermes:research", "fact")])
            self.assertEqual(
                ouro.appends, [("LOG:hermes:research:2026-04-05", "- x")]
            )
            self.assertEqual(ouro.searches, ["query"])

    def test_no_ouro_falls_back_to_local_for_everything(self):
        with TemporaryDirectory() as tmpdir:
            composite, local, _ouro = self._composite(tmpdir, with_ouro=False)

            self.assertTrue(composite.write("MEMORY:hermes:research", "## Facts"))
            mem_path = local._name_to_path("MEMORY:hermes:research")
            self.assertTrue(mem_path.exists())
            self.assertEqual(composite.search("anything"), [])
            self.assertFalse(composite.comment("MEMORY:hermes:research", "hi"))

    def test_memory_and_log_names_come_from_ouro_when_present(self):
        with TemporaryDirectory() as tmpdir:
            composite, _local, ouro = self._composite(tmpdir, with_ouro=True)

            self.assertEqual(
                composite.memory_name("hermes"),
                ouro.memory_name("hermes"),
            )
            self.assertEqual(
                composite.log_name("hermes", "2026-04-05"),
                ouro.log_name("hermes", "2026-04-05"),
            )

    def test_memory_and_log_names_fall_back_to_local_when_no_ouro(self):
        with TemporaryDirectory() as tmpdir:
            composite, local, _ouro = self._composite(tmpdir, with_ouro=False)

            self.assertEqual(
                composite.memory_name("hermes"),
                local.memory_name("hermes"),
            )
            self.assertEqual(
                composite.log_name("hermes", "2026-04-05"),
                local.log_name("hermes", "2026-04-05"),
            )

    def test_append_list_item_forwards_initial_md_to_ouro(self):
        with TemporaryDirectory() as tmpdir:
            composite, _local, ouro = self._composite(tmpdir, with_ouro=True)

            composite.append_list_item(
                "LOG:hermes:research:2026-04-05",
                "- entry",
                initial_md="# header\n\n- entry",
            )

            self.assertEqual(
                ouro.list_item_calls,
                [("LOG:hermes:research:2026-04-05", "- entry", "# header\n\n- entry")],
            )

    def test_ouro_property_exposes_inner_store(self):
        with TemporaryDirectory() as tmpdir:
            composite, _local, ouro = self._composite(tmpdir, with_ouro=True)
            self.assertIs(composite.ouro, ouro)

            composite_local_only, _local2, _none = self._composite(
                tmpdir, with_ouro=False
            )
            self.assertIsNone(composite_local_only.ouro)


if __name__ == "__main__":
    unittest.main()
