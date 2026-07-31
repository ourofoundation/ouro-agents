import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from ouro_agents.memory.naming import (
    is_catch_all_team_id,
    log_doc_display_name,
    log_doc_name,
    log_entry_timestamp,
    memory_team_id,
    rewrite_team_qualifier,
    team_doc_key,
)
from ouro_agents.memory.ouro_docs import OuroDocStore
from ouro_agents.teams import TeamRegistry


class LogEntryTimestampTests(unittest.TestCase):
    def test_daily_uses_time_only(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("daily", when), "14:30")

    def test_weekly_includes_date(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("weekly", when), "2026-06-09 14:30")

    def test_biweekly_includes_date(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("biweekly", when), "2026-06-09 14:30")

    def test_unknown_rhythm_defaults_to_daily(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("monthly", when), "14:30")


class TeamDocKeyTests(unittest.TestCase):
    def test_prefers_name_over_team_id_slug(self):
        self.assertEqual(
            team_doc_key(
                team_slug=None,
                team_name="2d-materials",
                team_id="019f4c4e-8427-7150-95d4-8140a15e2540",
            ),
            "2d-materials",
        )

    def test_skips_poisoned_slug_equal_to_team_id(self):
        tid = "019f4c4e-8427-7150-95d4-8140a15e2540"
        self.assertEqual(
            team_doc_key(team_slug=tid, team_name="2d-materials", team_id=tid),
            "2d-materials",
        )

    def test_nil_team_id_becomes_all(self):
        tid = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(team_doc_key(team_id=tid), "all")
        self.assertEqual(
            team_doc_key(team_slug=tid, team_name=tid, team_id=tid),
            "all",
        )

    def test_catch_all_team_id_helpers(self):
        nil = "00000000-0000-0000-0000-000000000000"
        self.assertTrue(is_catch_all_team_id(nil))
        self.assertTrue(is_catch_all_team_id(nil.upper()))
        self.assertFalse(is_catch_all_team_id(None))
        self.assertFalse(is_catch_all_team_id(""))
        self.assertFalse(
            is_catch_all_team_id("01954d5f-fcea-7970-b8d8-b68879df9d7f")
        )
        self.assertIsNone(memory_team_id(nil))
        self.assertIsNone(memory_team_id(None))
        real = "01954d5f-fcea-7970-b8d8-b68879df9d7f"
        self.assertEqual(memory_team_id(real), real)

    def test_rewrite_team_qualifier_for_log_and_memory(self):
        tid = "019f4c4e-8427-7150-95d4-8140a15e2540"
        self.assertEqual(
            rewrite_team_qualifier(
                f"LOG:hermes:{tid}:2026-W31",
                old=tid,
                new="2d-materials",
            ),
            "LOG:hermes:2d-materials:2026-W31",
        )
        self.assertEqual(
            rewrite_team_qualifier(
                f"MEMORY:hermes:{tid}",
                old=tid,
                new="2d-materials",
            ),
            "MEMORY:hermes:2d-materials",
        )


class TeamRegistrySlugTests(unittest.TestCase):
    def test_null_slug_falls_back_to_name(self):
        registry = TeamRegistry()
        registry.refresh(
            {
                "teams": [
                    {
                        "id": "019f4c4e-8427-7150-95d4-8140a15e2540",
                        "name": "2d-materials",
                        "slug": None,
                        "org_id": "00000000-0000-0000-0000-000000000000",
                    }
                ]
            },
            "00000000-0000-0000-0000-000000000000",
        )
        team = registry.get_team("019f4c4e-8427-7150-95d4-8140a15e2540")
        assert team is not None
        self.assertEqual(team.slug, "2d-materials")


class _FakeAssets:
    def __init__(self):
        self.search_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return []


class _FakePosts:
    def __init__(self):
        self.updated = []

    def update(self, id, content=None, name=None):
        self.updated.append({"id": id, "content": content, "name": name})

    def create(self, **kwargs):
        return SimpleNamespace(id="created-post")

    def retrieve(self, id):
        return SimpleNamespace(
            id=id,
            content=SimpleNamespace(text="", data={}),
            last_updated=None,
        )


class _FakeClient:
    def __init__(self):
        self.assets = _FakeAssets()
        self.posts = _FakePosts()


class PoisonedRegistryRecoveryTests(unittest.TestCase):
    def test_load_rewrites_uuid_keys_and_slug(self):
        tid = "019f4c4e-8427-7150-95d4-8140a15e2540"
        with TemporaryDirectory() as tmpdir:
            registry_path = Path(tmpdir) / "state.json"
            registry_path.write_text(
                (
                    "{"
                    f'"team": {{"id": "{tid}", "name": "2d-materials", "slug": "{tid}", "org_id": "org"}},'
                    f'"docs": {{"LOG:hermes:{tid}:2026-W31": {{"uuid": "post-1", "owned": true}},'
                    f'"MEMORY:hermes:{tid}": {{"uuid": "mem-1", "owned": true}}}}'
                    "}"
                )
            )
            store = OuroDocStore(
                agent_name="hermes",
                org_id="org",
                team_id=tid,
                team_slug=tid,
                team_name="2d-materials",
                client=_FakeClient(),
                registry_path=registry_path,
            )
            self.assertEqual(store.team_slug, "2d-materials")
            self.assertEqual(
                store.log_name("hermes", "2026-W31"),
                "LOG:hermes:2d-materials:2026-W31",
            )
            self.assertEqual(
                store._uuid_cache["LOG:hermes:2d-materials:2026-W31"],
                "post-1",
            )
            self.assertEqual(
                store._uuid_cache["MEMORY:hermes:2d-materials"],
                "mem-1",
            )
            self.assertEqual(
                log_doc_display_name(store.log_name("hermes", "2026-W31")),
                "#2d-materials weekly log 2026-W31",
            )
            self.assertNotIn(f"LOG:hermes:{tid}:2026-W31", store._uuid_cache)


if __name__ == "__main__":
    unittest.main()
