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


if __name__ == "__main__":
    unittest.main()
