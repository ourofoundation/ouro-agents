import unittest

from ouro_agents.tool_prompt import build_tool_calling_system_prompt


class TestToolPrompt(unittest.TestCase):
    def test_appends_extra_instructions(self):
        extra = "You are operating inside Ouro."

        result = build_tool_calling_system_prompt(extra)

        self.assertTrue(result.endswith(extra))
        self.assertIn("\n\n" + extra, result)

    def test_explains_tool_or_final_content_turns(self):
        prompt = build_tool_calling_system_prompt()

        self.assertIn("Turn mechanics", prompt)
        self.assertIn("Continue:", prompt)
        self.assertIn("Finish:", prompt)
        self.assertIn("Never emit an empty message", prompt)
        self.assertIn("never end a turn on a preamble", prompt)
        self.assertIn("final answer as assistant content with no tool calls", prompt)
        self.assertIn("put that exact output in the\n  final content", prompt)

    def test_requires_evidence_for_platform_work(self):
        prompt = build_tool_calling_system_prompt()

        self.assertIn("include concrete evidence", prompt)
        self.assertIn("asset IDs", prompt)
        self.assertIn("action IDs", prompt)
        self.assertIn("rather than presenting", prompt)

    def test_work_directive_present_by_default_absent_in_conversational(self):
        work = build_tool_calling_system_prompt()
        chat = build_tool_calling_system_prompt(conversational=True)
        analysis = build_tool_calling_system_prompt(include_work_directive=False)
        scoped = build_tool_calling_system_prompt(
            "You own the whole tick.",
            include_work_directive=False,
            include_mechanics=False,
        )

        self.assertIn("Prime directive: do the work", work)
        self.assertIn("analyze, execute, publish, or update", work)

        self.assertNotIn("Prime directive", chat)
        self.assertNotIn("Prime directive", analysis)
        # Mechanics are shared by both.
        self.assertIn("Turn mechanics", chat)
        self.assertIn("Turn mechanics", analysis)
        self.assertEqual(scoped, "You own the whole tick.")

    def test_observation_budget_hint_when_policy_provided(self):
        from ouro_agents.tools.observation_policy import ObservationPolicy

        policy = ObservationPolicy(max_inline_chars=12_345)
        with_hint = build_tool_calling_system_prompt(observation_policy=policy)
        without = build_tool_calling_system_prompt()

        self.assertIn("Tool-result size budget", with_hint)
        self.assertIn("12,345 characters", with_hint)
        self.assertIn("Do not `cat` the whole spill file", with_hint)
        self.assertIn("Exempt from spilling", with_hint)
        self.assertIn("`load_skill`", with_hint)
        self.assertNotIn("Tool-result size budget", without)


if __name__ == "__main__":
    unittest.main()
