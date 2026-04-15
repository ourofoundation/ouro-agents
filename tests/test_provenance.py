import json
import tempfile
import unittest
from pathlib import Path

from ouro_agents.modes.planning import PlanCycle
from ouro_agents.provenance import resolve_event_provenance


class TestResolveEventProvenance(unittest.TestCase):
    def _write_active_plan(
        self, workspace: Path, quest_id: str, status: str = "active", team_id: str = "team-1"
    ) -> None:
        plan = PlanCycle(
            id="cycle-1",
            status=status,
            kind="default",
            plan_text="# Plan",
            quest_id=quest_id,
            team_id=team_id,
        )
        active_dir = workspace / "teams" / team_id / "plans" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        (active_dir / "default.json").write_text(json.dumps(plan.model_dump(), indent=2))

    def test_comment_on_plan_quest_matches_target_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_active_plan(workspace, quest_id="plan-quest-1", team_id="team-1")

            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-1",
                    "source_asset_type": "comment",
                    "root_asset_id": "plan-quest-1",
                    "root_asset_type": "quest",
                    "team": {"id": "team-1", "name": "research"},
                },
                workspace=workspace,
                planning_enabled=True,
            )

            self.assertTrue(provenance.is_plan_feedback)
            self.assertEqual(provenance.plan_cycle.quest_id, "plan-quest-1")
            self.assertEqual(provenance.team_id, "team-1")

    def test_reply_in_plan_comment_thread_resolves_root_plan_quest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_active_plan(workspace, quest_id="plan-quest-1", team_id="team-1")

            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-2",
                    "source_asset_type": "comment",
                    "target_id": "thread-comment-1",
                    "target_asset_type": "comment",
                    "root_asset_id": "plan-quest-1",
                    "root_asset_type": "quest",
                    "team": {"id": "team-1", "name": "research"},
                },
                workspace=workspace,
                planning_enabled=True,
            )

            self.assertTrue(provenance.is_plan_feedback)
            self.assertEqual(provenance.plan_cycle.quest_id, "plan-quest-1")
            self.assertEqual(provenance.team_id, "team-1")

    def test_event_without_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-1",
                    "root_asset_id": "asset-1",
                },
                workspace=workspace,
            )
            self.assertIsNone(provenance.team_id)
            self.assertFalse(provenance.is_plan_feedback)

    def test_event_extracts_team_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-1",
                    "root_asset_id": "asset-1",
                    "team": {"id": "team-42", "name": "design"},
                },
                workspace=workspace,
            )
            self.assertEqual(provenance.team_id, "team-42")

    def test_searches_all_teams_when_no_event_team(self):
        """When event has no team, provenance scans all team plan stores."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_active_plan(workspace, quest_id="q-1", team_id="team-a")

            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-1",
                    "root_asset_id": "q-1",
                },
                workspace=workspace,
                planning_enabled=True,
            )
            self.assertTrue(provenance.is_plan_feedback)
            self.assertEqual(provenance.plan_cycle.quest_id, "q-1")


if __name__ == "__main__":
    unittest.main()
