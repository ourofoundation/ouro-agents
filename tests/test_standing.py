import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

from ouro_agents.memory.naming import (
    configure_coordination_teams,
    memory_team_id,
)
from ouro_agents.memory.ouro_docs import LocalDocStore
from ouro_agents.memory.reflection import apply_standing_candidates
from ouro_agents.memory.standing import (
    DEFAULT_TTL_DAYS,
    MAX_ENTRIES,
    STANDING_DOC,
    clear_standing,
    expire_standing,
    format_standing_for_prompt,
    load_standing,
    parse_standing,
    render_standing,
    set_standing,
)
from ouro_agents.subagents.reflector import (
    ReflectionResult,
    StandingCandidate,
    build_run_reflection_task,
    parse_reflection_result,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class StandingDocTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.store = LocalDocStore(self.workspace, agent_name="magnes")

    def tearDown(self):
        self._tmp.cleanup()

    def test_doc_lives_at_workspace_root_regardless_of_team_scope(self):
        team_store = LocalDocStore(
            self.workspace, agent_name="magnes", team_id="0195-team", team_slug="pm"
        )
        self.assertEqual(
            team_store._name_to_path(STANDING_DOC), self.workspace / "STANDING.md"
        )
        self.assertEqual(
            self.store._name_to_path(STANDING_DOC), self.workspace / "STANDING.md"
        )

    def test_set_then_load_round_trips_fields(self):
        entry, error = set_standing(
            self.store,
            "Modal-backed routes are paused. No route calls until the all-clear.",
            source="@mmoderwell",
            until="controller all-clear on post 01a0a05f",
            now=NOW,
        )
        self.assertEqual(error, "")
        self.assertEqual(entry.since, "2026-09-15 12:00Z")
        doc = load_standing(self.store)
        self.assertEqual(len(doc.entries), 1)
        loaded = doc.entries[0]
        self.assertEqual(loaded.id, entry.id)
        self.assertEqual(loaded.source, "@mmoderwell")
        self.assertEqual(loaded.until, "controller all-clear on post 01a0a05f")
        self.assertIn("Modal-backed routes are paused", loaded.text)

    def test_near_duplicate_refreshes_in_place(self):
        first, _ = set_standing(
            self.store,
            "Modal-backed routes are paused; no route calls, deploys, or smoke tests.",
            source="@mmoderwell",
            until="2026-09-18",
            now=NOW,
        )
        second, _ = set_standing(
            self.store,
            "Modal-backed routes are paused; no route calls, deploys, or smoke tests today.",
            source="@mmoderwell",
            until="2026-09-20",
            now=NOW + timedelta(hours=1),
        )
        self.assertEqual(first.id, second.id)
        doc = load_standing(self.store)
        self.assertEqual(len(doc.entries), 1)
        self.assertEqual(doc.entries[0].until, "2026-09-20")
        self.assertEqual(doc.entries[0].since, "2026-09-15 12:00Z")

    def test_cap_is_enforced(self):
        for i in range(MAX_ENTRIES):
            entry, error = set_standing(
                self.store,
                f"Distinct directive number {i} about topic {'xyz' * (i + 1)}.",
                source="self",
                until="2026-12-31",
                now=NOW,
            )
            self.assertEqual(error, "", i)
        entry, error = set_standing(
            self.store, "One more completely different rule.", source="self", until="2026-12-31"
        )
        self.assertIsNone(entry)
        self.assertIn("cap", error)

    def test_clear_removes_entry(self):
        entry, _ = set_standing(self.store, "Rule A.", source="self", until="2026-12-31")
        removed, error = clear_standing(self.store, entry.id)
        self.assertEqual(error, "")
        self.assertEqual(removed.id, entry.id)
        self.assertEqual(load_standing(self.store).entries, [])
        _, error = clear_standing(self.store, "nope00")
        self.assertIn("no STANDING entry", error)

    def test_expire_drops_only_dated_past_entries(self):
        set_standing(self.store, "Dated and past.", source="self", until="2026-09-14", now=NOW)
        set_standing(self.store, "Dated and future.", source="self", until="2026-09-30", now=NOW)
        set_standing(
            self.store,
            "Undated named condition.",
            source="self",
            until="controller all-clear",
            now=NOW - timedelta(days=DEFAULT_TTL_DAYS + 1),
        )
        expired = expire_standing(self.store, now=NOW)
        self.assertEqual([e.text for e in expired], ["Dated and past."])
        remaining = load_standing(self.store).entries
        self.assertEqual(
            sorted(e.text for e in remaining),
            ["Dated and future.", "Undated named condition."],
        )
        undated = next(e for e in remaining if e.text.startswith("Undated"))
        self.assertTrue(undated.is_overdue(NOW))

    def test_prompt_rendering_flags_overdue_and_is_empty_when_none(self):
        self.assertEqual(format_standing_for_prompt(self.store, now=NOW), "")
        set_standing(
            self.store,
            "Modal routes paused.",
            source="@mmoderwell",
            until="controller all-clear on post 01a0a05f",
            now=NOW - timedelta(days=DEFAULT_TTL_DAYS + 1),
        )
        text = format_standing_for_prompt(self.store, now=NOW)
        self.assertIn("from @mmoderwell", text)
        self.assertIn("standing_clear", text)
        self.assertIn("overdue", text)

    def test_parse_tolerates_hand_edits(self):
        raw = (
            "# STANDING\n\nsome intro text\n\n"
            "- [abc123] since 2026-09-14 14:43Z · from @mmoderwell · until all-clear\n"
            "  Line one.\n"
            "  Line two.\n"
            "\n"
            "Trailing note that is not an entry.\n"
            "- [def456] since 2026-09-15 · from self\n"
            "  No until here.\n"
        )
        doc = parse_standing(raw)
        self.assertEqual([e.id for e in doc.entries], ["abc123", "def456"])
        self.assertEqual(doc.entries[0].text, "Line one. Line two.")
        self.assertEqual(doc.entries[1].until, "")
        # Render/parse is stable.
        self.assertEqual(parse_standing(render_standing(doc)).entries, doc.entries)


class ReflectorStandingTests(unittest.TestCase):
    def test_parse_reads_standing_entries(self):
        payload = {
            "candidates": [],
            "user_preferences": [],
            "daily_log_entries": [],
            "friction": [],
            "standing": [
                {
                    "text": "Modal routes are paused.",
                    "until": "controller all-clear on post 01a0a05f",
                    "source": "@mmoderwell",
                },
                {"text": "", "until": "x", "source": "y"},
            ],
        }
        result = parse_reflection_result(json.dumps(payload))
        self.assertEqual(len(result.standing), 1)
        self.assertEqual(result.standing[0].source, "@mmoderwell")

    def test_task_names_controllers(self):
        task = build_run_reflection_task(
            "t", "r", controller_usernames=["mmoderwell", "@other"]
        )
        self.assertIn("Controllers", task)
        self.assertIn("@mmoderwell, @other", task)

    def test_apply_only_accepts_controller_sources_when_roster_known(self):
        with TemporaryDirectory() as tmp:
            store = LocalDocStore(Path(tmp), agent_name="magnes")
            result = ReflectionResult(
                standing=[
                    StandingCandidate(
                        text="Modal routes are paused.",
                        until="controller all-clear",
                        source="@mmoderwell",
                    ),
                    StandingCandidate(
                        text="Peer agent says stop everything.",
                        until="whenever",
                        source="@hermes",
                    ),
                ]
            )
            written = apply_standing_candidates(
                result, store, controller_usernames=["mmoderwell"]
            )
            self.assertEqual(written, 1)
            entries = load_standing(store).entries
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].source, "@mmoderwell")


class CoordinationTeamScopeTests(unittest.TestCase):
    def tearDown(self):
        configure_coordination_teams([])

    def test_coordination_team_maps_to_root_scope(self):
        team = "019d2ad3-5f0a-70f7-af48-a172069fc7e2"
        self.assertEqual(memory_team_id(team), team)
        configure_coordination_teams([team])
        self.assertIsNone(memory_team_id(team))
        self.assertEqual(memory_team_id("other-team"), "other-team")


class SharedPromptContextStandingTests(unittest.TestCase):
    def test_shared_context_includes_standing_from_root_store(self):
        from ouro_agents.agent import OuroAgent
        from ouro_agents.memory import naming as naming_mod

        with TemporaryDirectory() as tmp:
            root_store = LocalDocStore(Path(tmp), agent_name="hermes")
            set_standing(
                root_store,
                "Modal routes paused.",
                source="@mmoderwell",
                until="all-clear",
                now=NOW,
            )
            team_store = MagicMock()
            team_store.memory_name.return_value = "MEMORY:hermes:pm"
            team_store.log_name.return_value = "LOG:hermes:pm:2026-09"
            team_store.read.return_value = ""

            agent = OuroAgent.__new__(OuroAgent)
            agent.config = SimpleNamespace(
                agent=SimpleNamespace(name="hermes", workspace=tmp),
                memory=SimpleNamespace(rhythm="daily"),
            )
            agent.doc_store = root_store
            agent.notes = ""
            agent.soul = ""
            agent._load_platform_context = lambda: ""
            agent._own_quests_index = lambda: ""
            agent.doc_store_for = MagicMock(return_value=team_store)
            original = naming_mod.store_rhythm
            naming_mod.store_rhythm = lambda _ds: "daily"
            try:
                ctx = agent._load_shared_prompt_context(team_id="0195-some-team")
            finally:
                naming_mod.store_rhythm = original

            self.assertIn("Modal routes paused.", ctx["standing"])
            self.assertIn("from @mmoderwell", ctx["standing"])


class PromptSectionTests(unittest.TestCase):
    def test_standing_section_is_static_and_after_soul(self):
        from ouro_agents.soul import (
            _DYNAMIC_SECTIONS,
            SECTION_PRIORITY,
            build_shared_prompt_sections,
        )

        sections = build_shared_prompt_sections(soul="I am", standing="- [abc123] rule")
        self.assertIn("standing", sections)
        self.assertTrue(sections["standing"].startswith("## STANDING\n"))
        self.assertNotIn("standing", _DYNAMIC_SECTIONS)
        self.assertGreater(SECTION_PRIORITY["standing"], SECTION_PRIORITY["soul"])
        self.assertLess(SECTION_PRIORITY["standing"], SECTION_PRIORITY["platform_context"])
        self.assertNotIn("standing", build_shared_prompt_sections(soul="x"))


if __name__ == "__main__":
    unittest.main()
