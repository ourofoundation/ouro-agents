import unittest

from ouro_agents.modes.planning import PlanCycle, PlanItem
from ouro_agents.tui.review_picker import build_review_plan_options, choose_review_plan


class TestReviewPicker(unittest.TestCase):
    def test_build_review_plan_options_formats_reviewable_plans(self):
        plans = [
            PlanCycle(
                id="default-1234",
                kind="default",
                status="pending_review",
                items=[PlanItem(description="Ship it")],
                quest_id="quest-default",
            ),
            PlanCycle(
                id="goal-12345678",
                kind="goal",
                goal="Review the materialized graph sync flow",
                status="active",
                items=[
                    PlanItem(description="A", status="done"),
                    PlanItem(description="B"),
                ],
                quest_id="quest-goal",
            ),
            PlanCycle(
                id="completed-1",
                kind="goal",
                goal="Done already",
                status="completed",
            ),
        ]

        options = build_review_plan_options(plans)

        self.assertEqual(len(options), 2)
        self.assertEqual(options[0].title, "Default plan")
        self.assertIn("pending review", options[0].subtitle)
        self.assertEqual(
            options[1].title,
            "Goal plan: Review the materialized graph sync flow",
        )
        self.assertIn("1/2 complete", options[1].subtitle)

    def test_choose_review_plan_short_circuits_for_single_plan(self):
        only_plan = PlanCycle(
            id="goal-abcdef12",
            kind="goal",
            goal="One plan only",
            status="pending_review",
        )

        selected = choose_review_plan([only_plan])

        self.assertEqual(selected, "goal-abcdef12")


if __name__ == "__main__":
    unittest.main()
