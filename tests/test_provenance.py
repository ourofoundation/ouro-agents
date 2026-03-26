import json
import tempfile
import unittest
from pathlib import Path

from ouro_agents.modes.planning import PlanCycle
from ouro_agents.provenance import resolve_event_provenance


class TestResolveEventProvenance(unittest.TestCase):
    def _write_active_plan(self, workspace: Path, post_id: str, status: str = "active") -> None:
        plan = PlanCycle(
            id="cycle-1",
            status=status,
            kind="default",
            plan_text="# Plan",
            post_id=post_id,
        )
        active_dir = workspace / "plans" / "active"
        active_dir.mkdir(parents=True, exist_ok=True)
        (active_dir / "default.json").write_text(json.dumps(plan.model_dump(), indent=2))

    def test_comment_on_plan_post_matches_target_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_active_plan(workspace, post_id="plan-post-1")

            provenance = resolve_event_provenance(
                source_id="comment-1",
                event_data={
                    "source_id": "comment-1",
                    "source_asset_type": "comment",
                    "target_id": "plan-post-1",
                    "target_asset_type": "post",
                },
                workspace=workspace,
                planning_enabled=True,
            )

            self.assertTrue(provenance.is_plan_feedback)
            self.assertEqual(provenance.plan_cycle.post_id, "plan-post-1")

    def test_reply_in_plan_comment_thread_resolves_root_plan_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_active_plan(workspace, post_id="plan-post-1")

            provenance = resolve_event_provenance(
                source_id="comment-2",
                event_data={
                    "source_id": "comment-2",
                    "source_asset_type": "comment",
                    "target_id": "thread-comment-1",
                    "target_asset_type": "comment",
                },
                workspace=workspace,
                planning_enabled=True,
                resolve_comment_parent=lambda comment_id: (
                    ("plan-post-1", "post") if comment_id == "thread-comment-1" else (None, None)
                ),
            )

            self.assertTrue(provenance.is_plan_feedback)
            self.assertEqual(provenance.plan_cycle.post_id, "plan-post-1")


if __name__ == "__main__":
    unittest.main()
