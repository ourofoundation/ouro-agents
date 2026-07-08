import json
import tempfile
import unittest
from pathlib import Path

from ouro_agents.provenance import resolve_event_provenance


def _write_platform_context(workspace: Path, user_id: str) -> None:
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "platform_context.json").write_text(
        json.dumps({"profile": {"id": user_id}})
    )


class TestResolveEventProvenance(unittest.TestCase):
    def test_comment_on_own_quest_is_quest_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_platform_context(workspace, "agent-user")

            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-1",
                    "source_asset_type": "comment",
                    "root_asset_id": "plan-quest-1",
                    "root_asset_type": "quest",
                    "asset_user_id": "agent-user",
                    "team": {"id": "team-1", "name": "research"},
                },
                workspace=workspace,
            )

            self.assertTrue(provenance.is_quest_feedback)
            self.assertEqual(provenance.root_asset_id, "plan-quest-1")
            self.assertEqual(provenance.team_id, "team-1")

    def test_structured_root_asset_object_matches_quest(self):
        """Canonical payloads nest the root as a structured object, not a flat key."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_platform_context(workspace, "agent-user")

            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-2",
                    "source_asset_type": "comment",
                    "parent_asset": {"id": "thread-comment-1", "type": "comment"},
                    "root_asset": {"id": "plan-quest-1", "type": "quest"},
                    "asset_user_id": "agent-user",
                    "team": {"id": "team-1", "name": "research"},
                },
                workspace=workspace,
            )

            self.assertTrue(provenance.is_quest_feedback)
            self.assertEqual(provenance.root_asset_id, "plan-quest-1")
            self.assertEqual(provenance.team_id, "team-1")

    def test_comment_on_someone_elses_quest_is_not_quest_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_platform_context(workspace, "agent-user")

            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-1",
                    "root_asset_id": "quest-1",
                    "root_asset_type": "quest",
                    "asset_user_id": "other-user",
                },
                workspace=workspace,
            )

            self.assertFalse(provenance.is_own_asset)
            self.assertFalse(provenance.is_quest_feedback)

    def test_comment_on_own_post_is_own_but_not_quest_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_platform_context(workspace, "agent-user")

            provenance = resolve_event_provenance(
                event_data={
                    "source_id": "comment-1",
                    "root_asset_id": "post-1",
                    "root_asset_type": "post",
                    "asset_user_id": "agent-user",
                },
                workspace=workspace,
            )

            self.assertTrue(provenance.is_own_asset)
            self.assertFalse(provenance.is_quest_feedback)

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
            self.assertFalse(provenance.is_quest_feedback)

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


if __name__ == "__main__":
    unittest.main()
