"""Tests for native tool-call reconstruction in the message-cleaning patch.

smolagents replays prior tool use as a *text* protocol — an assistant message
ending in ``Calling tools:\n[{...}]`` followed by a ``Observation:\n...`` user
message. Native-function-calling models (GLM especially) imitate that text
instead of emitting real tool calls. The patch rewrites it into proper
``assistant.tool_calls`` + ``role:"tool"`` messages; these tests lock that in.
"""

import json
import unittest

import smolagents.models as smol_models
from smolagents.models import ChatMessage, MessageRole

import ouro_agents.smolagents_patches  # noqa: F401  (applies the patch)

CONV = smol_models.tool_role_conversions


def _text_msg(role, text):
    return ChatMessage(role=role, content=[{"type": "text", "text": text}])


def _calling_tools_text(calls):
    # Mirror smolagents/memory.py: "Calling tools:\n" + str([tc.dict() ...]).
    return "Calling tools:\n" + str(calls)


def _clean(messages):
    return smol_models.get_clean_message_list(messages, role_conversions=CONV)


class TestNativeToolCallReconstruction(unittest.TestCase):
    def test_single_call_becomes_native_with_tool_message(self):
        calls = [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "get_asset", "arguments": {"asset_id": "x1"}},
            }
        ]
        messages = [
            _text_msg(MessageRole.ASSISTANT, "Let me look."),
            _text_msg(MessageRole.TOOL_CALL, _calling_tools_text(calls)),
            _text_msg(MessageRole.TOOL_RESPONSE, "Observation:\nresult-data"),
        ]

        cleaned = _clean(messages)

        self.assertEqual(len(cleaned), 2)
        assistant = cleaned[0]
        self.assertEqual(assistant["role"], MessageRole.ASSISTANT)
        self.assertEqual(assistant["content"], "Let me look.")
        self.assertEqual(len(assistant["tool_calls"]), 1)
        tc = assistant["tool_calls"][0]
        self.assertEqual(tc["id"], "call_abc")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "get_asset")
        # arguments must be a JSON string for the API, not a dict.
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"asset_id": "x1"})

        tool_msg = cleaned[1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["tool_call_id"], "call_abc")
        self.assertEqual(tool_msg["content"], "result-data")

    def test_multi_call_pairs_each_id_with_a_tool_message(self):
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_asset", "arguments": {"id": "a"}},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "search_assets", "arguments": {"q": "b"}},
            },
        ]
        messages = [
            _text_msg(MessageRole.TOOL_CALL, _calling_tools_text(calls)),
            _text_msg(MessageRole.TOOL_RESPONSE, "Observation:\nresA\nresB"),
        ]

        cleaned = _clean(messages)

        self.assertEqual(cleaned[0]["role"], MessageRole.ASSISTANT)
        self.assertEqual(len(cleaned[0]["tool_calls"]), 2)
        # One tool message per id (API requires exact pairing).
        tool_msgs = [m for m in cleaned if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["call_1", "call_2"])
        self.assertEqual(tool_msgs[0]["content"], "resA\nresB")
        self.assertTrue(tool_msgs[1]["content"])  # placeholder, non-empty

    def test_labeled_multi_call_attributes_each_result(self):
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_quest_items", "arguments": {"quest_id": "q"}},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "create_quest", "arguments": {"name": "Plan"}},
            },
        ]
        labeled = (
            "=== Tool result: list_quest_items (id=call_1) ===\n"
            "items: []\n"
            "=== Tool result: create_quest (id=call_2) ===\n"
            '{"id": "quest-xyz", "name": "Plan"}'
        )
        messages = [
            _text_msg(MessageRole.TOOL_CALL, _calling_tools_text(calls)),
            _text_msg(MessageRole.TOOL_RESPONSE, f"Observation:\n{labeled}"),
        ]

        cleaned = _clean(messages)

        tool_msgs = [m for m in cleaned if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["call_1", "call_2"])
        self.assertEqual(tool_msgs[0]["content"], "items: []")
        self.assertEqual(
            tool_msgs[1]["content"], '{"id": "quest-xyz", "name": "Plan"}'
        )
        self.assertNotIn("result included with the first tool call", tool_msgs[1]["content"])

    def test_error_step_carries_error_text_as_tool_message(self):
        calls = [
            {
                "id": "call_e",
                "type": "function",
                "function": {"name": "get_asset", "arguments": {}},
            }
        ]
        messages = [
            _text_msg(MessageRole.TOOL_CALL, _calling_tools_text(calls)),
            _text_msg(
                MessageRole.TOOL_RESPONSE,
                "Call id: call_e\nError:\nboom\nNow let's retry:",
            ),
        ]

        cleaned = _clean(messages)

        tool_msgs = [m for m in cleaned if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call_e")
        self.assertIn("Error:", tool_msgs[0]["content"])

    def test_narrated_leak_is_dropped_real_call_is_used(self):
        # Model imitates the format in its own content (fabricated id), and the
        # authoritative smolagents block follows. Keep narration before the
        # first marker; parse the real (last) block.
        narrated = [
            {
                "id": "fake",
                "type": "function",
                "function": {"name": "x", "arguments": {}},
            }
        ]
        real = [
            {
                "id": "call_real",
                "type": "function",
                "function": {"name": "get_asset", "arguments": {"a": 1}},
            }
        ]
        leaked = "Checking.\n" + _calling_tools_text(narrated)
        messages = [
            _text_msg(MessageRole.ASSISTANT, leaked),
            _text_msg(MessageRole.TOOL_CALL, _calling_tools_text(real)),
            _text_msg(MessageRole.TOOL_RESPONSE, "Observation:\nok"),
        ]

        cleaned = _clean(messages)

        assistant = cleaned[0]
        self.assertEqual(assistant["content"], "Checking.")
        self.assertEqual([tc["id"] for tc in assistant["tool_calls"]], ["call_real"])

    def test_content_only_call_sets_content_none(self):
        calls = [
            {
                "id": "call_x",
                "type": "function",
                "function": {"name": "noop", "arguments": {}},
            }
        ]
        messages = [
            _text_msg(MessageRole.TOOL_CALL, _calling_tools_text(calls)),
            _text_msg(MessageRole.TOOL_RESPONSE, "Observation:\ndone"),
        ]

        cleaned = _clean(messages)

        # No narration preceded the call → assistant content is None.
        self.assertIsNone(cleaned[0]["content"])
        self.assertEqual(len(cleaned[0]["tool_calls"]), 1)

    def test_plain_assistant_without_calls_is_untouched(self):
        messages = [_text_msg(MessageRole.ASSISTANT, "Here is the final answer.")]

        cleaned = _clean(messages)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["content"], [{"type": "text", "text": "Here is the final answer."}])
        self.assertNotIn("tool_calls", cleaned[0])

    def test_empty_call_list_stays_as_text(self):
        # An empty "[]" is not a real call; leave the step untouched.
        messages = [_text_msg(MessageRole.TOOL_CALL, "Calling tools:\n[]")]

        cleaned = _clean(messages)

        self.assertNotIn("tool_calls", cleaned[0])

    def test_reasoning_details_survive_native_rewrite(self):
        calls = [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "get_asset", "arguments": {"id": "z"}},
            }
        ]
        tc = _text_msg(MessageRole.TOOL_CALL, _calling_tools_text(calls))
        tc.reasoning_details = [
            {
                "type": "reasoning.text",
                "text": "why this call",
                "format": "anthropic-claude-v1",
            }
        ]
        messages = [tc, _text_msg(MessageRole.TOOL_RESPONSE, "Observation:\nr")]

        cleaned = _clean(messages)

        assistant = cleaned[0]
        self.assertEqual(assistant["role"], MessageRole.ASSISTANT)
        self.assertIn("tool_calls", assistant)
        self.assertEqual(
            assistant["reasoning_details"],
            [
                {
                    "type": "reasoning.text",
                    "text": "why this call",
                    "format": "anthropic-claude-v1",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
