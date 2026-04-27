import unittest

from ouro_agents.tool_prompt import (
    TOOL_CALLING_SYSTEM_PROMPT,
    build_tool_calling_system_prompt,
)


class TestToolPrompt(unittest.TestCase):
    def test_returns_base_prompt_without_extra_instructions(self):
        self.assertEqual(
            build_tool_calling_system_prompt(),
            TOOL_CALLING_SYSTEM_PROMPT,
        )

    def test_appends_extra_instructions_to_base_prompt(self):
        extra = "You are operating inside Ouro."

        result = build_tool_calling_system_prompt(extra)

        self.assertTrue(result.startswith(TOOL_CALLING_SYSTEM_PROMPT))
        self.assertTrue(result.endswith(extra))
        self.assertIn("\n\n" + extra, result)

    def test_base_prompt_requires_tool_or_final_answer_turns(self):
        self.assertIn(
            "Every assistant turn must contain exactly one of: a real tool call, or a final_answer tool call.",
            TOOL_CALLING_SYSTEM_PROMPT,
        )
        self.assertIn("Never emit an empty assistant message.", TOOL_CALLING_SYSTEM_PROMPT)
        self.assertIn(
            "Do not write `final_answer(...)` as text.",
            TOOL_CALLING_SYSTEM_PROMPT,
        )
        self.assertIn(
            "put that exact structured output in final_answer's answer argument",
            TOOL_CALLING_SYSTEM_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
