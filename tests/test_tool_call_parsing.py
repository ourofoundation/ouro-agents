import unittest

from smolagents.models import ChatMessage, MessageRole

from ouro_agents.tools.agent_base import (
    _parse_inline_tool_call,
    _parse_structured_tool_calls,
    _patch_model_for_xml_tool_calls,
)


class _AlwaysFailsModel:
    def parse_tool_calls(self, message):
        raise ValueError("The model output does not contain any JSON blob.")


class TestToolCallParsing(unittest.TestCase):
    def test_recovers_keyword_style_call(self):
        tool_calls = _parse_inline_tool_call('get_comments(parent_id="abc-123")')

        self.assertIsNotNone(tool_calls)
        self.assertEqual(tool_calls[0].function.name, "get_comments")
        self.assertEqual(tool_calls[0].function.arguments, {"parent_id": "abc-123"})

    def test_recovers_dict_style_call(self):
        tool_calls = _parse_inline_tool_call("get_comments({'parent_id': 'abc-123'})")

        self.assertIsNotNone(tool_calls)
        self.assertEqual(tool_calls[0].function.name, "get_comments")
        self.assertEqual(tool_calls[0].function.arguments, {"parent_id": "abc-123"})

    def test_recovers_structured_payload_without_prefix(self):
        tool_calls = _parse_structured_tool_calls(
            """
            Sure, using the requested tool now.

            ```json
            {"function": {"name": "get_comments", "arguments": {"parent_id": "abc-123"}}}
            ```
            """
        )

        self.assertIsNotNone(tool_calls)
        self.assertEqual(tool_calls[0].function.name, "get_comments")
        self.assertEqual(tool_calls[0].function.arguments, {"parent_id": "abc-123"})

    def test_parse_failure_includes_message_preview(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="I'll inspect the thread next and then report back.",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(parsed.tool_calls, [])
        self.assertEqual(parsed.content, "I'll inspect the thread next and then report back.")


if __name__ == "__main__":
    unittest.main()
