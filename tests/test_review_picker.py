import unittest

from ouro_agents.tui.review_picker import build_review_plan_options, choose_review_plan


class TestReviewPicker(unittest.TestCase):
    def test_build_review_plan_options_formats_reviewable_quests(self):
        quests = [
            {
                "id": "quest-default",
                "name": "Ship the sync flow",
                "status": "draft",
                "items_total": 1,
                "items_resolved": 0,
            },
            {
                "id": "quest-goal",
                "name": "Review the materialized graph sync flow",
                "status": "open",
                "items_total": 2,
                "items_resolved": 1,
            },
            {
                "id": "quest-closed",
                "name": "Done already",
                "status": "closed",
            },
        ]

        options = build_review_plan_options(quests)

        self.assertEqual(len(options), 2)
        self.assertEqual(options[0].title, "Ship the sync flow")
        self.assertIn("draft", options[0].subtitle)
        self.assertEqual(
            options[1].title,
            "Review the materialized graph sync flow",
        )
        self.assertIn("1/2 resolved", options[1].subtitle)

    def test_choose_review_plan_short_circuits_for_single_quest(self):
        selected = choose_review_plan(
            [{"id": "quest-abcdef12", "name": "One quest only", "status": "draft"}]
        )

        self.assertEqual(selected, "quest-abcdef12")


if __name__ == "__main__":
    unittest.main()
