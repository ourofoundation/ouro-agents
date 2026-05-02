import unittest
from types import SimpleNamespace

from smolagents.models import ChatMessage, MessageRole

from ouro_agents.tools.agent_base import (
    _EMPTY_MODEL_RESPONSE_ANSWER,
    _extract_raw_reasoning_text,
    _parse_dsml_tool_call_recovery,
    _parse_dsml_tool_calls,
    _parse_kimi_tool_call_recovery,
    _parse_inline_tool_call,
    _parse_structured_tool_call_recovery,
    _parse_structured_tool_calls,
    _patch_model_for_xml_tool_calls,
)


class _AlwaysFailsModel:
    def parse_tool_calls(self, message):
        raise ValueError("The model output does not contain any JSON blob.")


class _EmptyResponseModel:
    def parse_tool_calls(self, message):
        raise ValueError("Message contains no content and no tool calls")


class _FakeModelWithToolCalls:
    """Stand-in that satisfies SanitizedToolCallingAgent's __init__ patch."""

    model_id = "fake-model"

    def parse_tool_calls(self, message):
        return message


class TestToolCallParsing(unittest.TestCase):
    def test_empty_model_response_terminates_with_sentinel_final_answer(self):
        model = _EmptyResponseModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"answer": _EMPTY_MODEL_RESPONSE_ANSWER},
        )

    def test_extracts_reasoning_from_raw_empty_response(self):
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=None,
                        reasoning="I considered calling a tool but stopped early.",
                        reasoning_details=[{"text": "provider-specific trace"}],
                    ),
                )
            ]
        )
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            raw=raw,
        )

        reasoning = _extract_raw_reasoning_text(message)

        self.assertIn("reasoning:", reasoning)
        self.assertIn("stopped early", reasoning)
        self.assertIn("reasoning_details:", reasoning)
        self.assertIn("provider-specific trace", reasoning)

    def test_recovers_raw_no_action_as_final_answer(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="NO_ACTION",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(parsed.tool_calls[0].function.arguments, {"answer": "NO_ACTION"})

    def test_recovers_terminal_no_action_after_reasoning(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=(
                "This thread is wrapping up and I have nothing useful to add.\n\n"
                "NO_ACTION"
            ),
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(parsed.tool_calls[0].function.arguments, {"answer": "NO_ACTION"})

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

    def test_recovers_nested_route_execution_arguments(self):
        tool_calls = _parse_structured_tool_calls(
            """
            {"tool": "execute_route", "arguments": {
              "route_id": "route-123",
              "body": {
                "input": {
                  "structure": {"formula": "Ce2Fe17", "sites": [{"element": "Ce"}, {"element": "Fe"}]},
                  "options": {"relax": false, "properties": ["formation_energy", "bandgap"]}
                },
                "metadata": {"source": {"asset_id": "file-123", "team": "materials-science"}}
              }
            }}
            """
        )

        self.assertIsNotNone(tool_calls)
        self.assertEqual(tool_calls[0].function.name, "execute_route")
        self.assertEqual(
            tool_calls[0].function.arguments["body"]["input"]["structure"]["sites"][1]["element"],
            "Fe",
        )
        self.assertEqual(
            tool_calls[0].function.arguments["body"]["metadata"]["source"]["team"],
            "materials-science",
        )

    def test_recovers_kimi_special_token_tool_call(self):
        recovery = _parse_kimi_tool_call_recovery(
            """
            <|tool_calls_section_begin|>
            <|tool_call_begin|>
            functions.memory_recall:0
            <|tool_call_argument_begin|>
            {"queries": [{"query": "Ce2Fe17 test", "scope": "team"}]}
            <|tool_call_end|>
            <|tool_calls_section_end|>
            """
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(len(recovery.tool_calls), 1)
        self.assertEqual(recovery.tool_calls[0].id, "functions.memory_recall:0")
        self.assertEqual(recovery.tool_calls[0].function.name, "memory_recall")
        self.assertEqual(
            recovery.tool_calls[0].function.arguments,
            {"queries": [{"query": "Ce2Fe17 test", "scope": "team"}]},
        )

    def test_recovers_kimi_nested_route_execution_arguments(self):
        recovery = _parse_kimi_tool_call_recovery(
            """
            <|tool_calls_section_begin|>
            <|tool_call_begin|>
            functions.execute_route:0
            <|tool_call_argument_begin|>
            {"route_id":"route-123","body":{"input":{"structure":{"formula":"Ce2Fe17","sites":[{"element":"Ce"},{"element":"Fe"}]},"options":{"relax":false}}}}
            <|tool_call_end|>
            <|tool_calls_section_end|>
            """
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.tool_calls[0].function.name, "execute_route")
        self.assertEqual(
            recovery.tool_calls[0].function.arguments["body"]["input"]["structure"]["formula"],
            "Ce2Fe17",
        )
        self.assertFalse(
            recovery.tool_calls[0].function.arguments["body"]["input"]["options"]["relax"]
        )

    def test_recovers_structured_payload_with_contaminated_list_arguments(self):
        recovery = _parse_structured_tool_call_recovery(
            """
            {"tool": "memory_recall", "arguments": [
              {"queries": [{"query": "Ce2Fe17 test", "scope": "team"}]},
              ["reasoning text that should not be treated as tool input"]
            ]}
            """
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(len(recovery.tool_calls), 1)
        self.assertEqual(recovery.tool_calls[0].function.name, "memory_recall")
        self.assertEqual(
            recovery.tool_calls[0].function.arguments,
            {"queries": [{"query": "Ce2Fe17 test", "scope": "team"}]},
        )
        self.assertIn("reasoning text", recovery.thought_text)

    def test_rejects_structured_payload_with_ambiguous_list_arguments(self):
        tool_calls = _parse_structured_tool_calls(
            """
            {"tool": "create_comment", "arguments": [
              {"parent_id": "abc"},
              {"content": "two dicts are ambiguous"}
            ]}
            """
        )

        self.assertIsNone(tool_calls)

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

    def test_chat_mode_recovers_raw_text_with_angle_brackets(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model, is_chat_mode=True)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="Hey! I can help with <that> if you want.",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"answer": "Hey! I can help with <that> if you want."},
        )

    def test_chat_mode_recovers_raw_text_with_braces(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model, is_chat_mode=True)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="Sure - use {'key': 'value'} as an example payload.",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"answer": "Sure - use {'key': 'value'} as an example payload."},
        )

    def test_parses_deepseek_dsml_tool_call_block(self):
        # The exact DSML format that vLLM (#36654) leaks into the reasoning
        # field for DeepSeek-V3.x/V4 when the upstream tool-call parser is
        # bypassed by the reasoning parser.
        recovery = _parse_dsml_tool_call_recovery(
            "<｜DSML｜function_calls>\n"
            "<｜DSML｜invoke name=\"get_asset\">\n"
            "<｜DSML｜parameter name=\"id\" string=\"true\">019de031-c2a0-7d2f-a818-a6480296e4ab</｜DSML｜parameter>\n"
            "<｜DSML｜parameter name=\"detail\" string=\"true\">full</｜DSML｜parameter>\n"
            "</｜DSML｜invoke>\n"
            "</｜DSML｜function_calls>"
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(len(recovery.tool_calls), 1)
        self.assertEqual(recovery.tool_calls[0].function.name, "get_asset")
        self.assertEqual(
            recovery.tool_calls[0].function.arguments,
            {
                "id": "019de031-c2a0-7d2f-a818-a6480296e4ab",
                "detail": "full",
            },
        )

    def test_dsml_decodes_non_string_parameter_values(self):
        # When ``string="true"`` is absent or false, parameters should be
        # decoded as JSON so booleans/numbers/lists come through correctly.
        tool_calls = _parse_dsml_tool_calls(
            "<｜DSML｜function_calls>"
            "<｜DSML｜invoke name=\"execute_route\">"
            "<｜DSML｜parameter name=\"dry_run\">true</｜DSML｜parameter>"
            "<｜DSML｜parameter name=\"limit\">10</｜DSML｜parameter>"
            "<｜DSML｜parameter name=\"tags\">[\"a\",\"b\"]</｜DSML｜parameter>"
            "</｜DSML｜invoke>"
            "</｜DSML｜function_calls>"
        )

        self.assertIsNotNone(tool_calls)
        self.assertEqual(tool_calls[0].function.name, "execute_route")
        self.assertEqual(
            tool_calls[0].function.arguments,
            {"dry_run": True, "limit": 10, "tags": ["a", "b"]},
        )

    def test_dsml_parser_accepts_normalized_ascii_bar(self):
        # Some providers normalize the ``｜`` (U+FF5C) byte to a regular ``|``
        # in transit. Make sure either form parses.
        recovery = _parse_dsml_tool_call_recovery(
            "<|DSML|function_calls>"
            "<|DSML|invoke name=\"final_answer\">"
            "<|DSML|parameter name=\"answer\" string=\"true\">done</|DSML|parameter>"
            "</|DSML|invoke>"
            "</|DSML|function_calls>"
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.tool_calls[0].function.name, "final_answer")
        self.assertEqual(
            recovery.tool_calls[0].function.arguments, {"answer": "done"}
        )

    def test_recovers_tool_call_from_reasoning_when_content_empty(self):
        # vLLM #36654: the model's tool call leaks into the ``reasoning``
        # field rather than ``content``/``tool_calls``. Without recovery we
        # would terminate the loop with MODEL_EMPTY_RESPONSE; with it we
        # extract the call and continue.
        model = _EmptyResponseModel()
        _patch_model_for_xml_tool_calls(model)

        leaked = (
            "I should look up the asset first.\n\n"
            "<｜DSML｜function_calls>"
            "<｜DSML｜invoke name=\"get_asset\">"
            "<｜DSML｜parameter name=\"id\" string=\"true\">abc-123</｜DSML｜parameter>"
            "</｜DSML｜invoke>"
            "</｜DSML｜function_calls>"
        )
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=None,
                        reasoning=leaked,
                        reasoning_details=None,
                    ),
                )
            ]
        )
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            raw=raw,
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "get_asset")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments, {"id": "abc-123"}
        )

    def test_recovers_tool_call_from_reasoning_details_text(self):
        # When the leaked tokens live inside ``reasoning_details[].text``
        # instead of the flat ``reasoning`` string, the salvage should still
        # find them.
        model = _EmptyResponseModel()
        _patch_model_for_xml_tool_calls(model)

        leaked = (
            "<｜DSML｜function_calls>"
            "<｜DSML｜invoke name=\"create_comment\">"
            "<｜DSML｜parameter name=\"parent_id\" string=\"true\">post-1</｜DSML｜parameter>"
            "<｜DSML｜parameter name=\"content\" string=\"true\">ok</｜DSML｜parameter>"
            "</｜DSML｜invoke>"
            "</｜DSML｜function_calls>"
        )
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=None,
                        reasoning=None,
                        reasoning_details=[
                            {"type": "reasoning.text", "text": leaked, "format": "unknown"}
                        ],
                    ),
                )
            ]
        )
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            raw=raw,
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "create_comment")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"parent_id": "post-1", "content": "ok"},
        )

    def test_reasoning_only_streak_logs_diagnostic_warning(self):
        from logging import WARNING
        from smolagents import tool

        from ouro_agents.tools.agent_base import (
            SanitizedToolCallingAgent,
            logger as agent_base_logger,
        )

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
        )

        # Below threshold: no warning fires.
        with self.assertNoLogs(agent_base_logger, level=WARNING):
            for _ in range(SanitizedToolCallingAgent._REASONING_ONLY_WARN_THRESHOLD - 1):
                agent._track_reasoning_only_step(SimpleNamespace(tool_calls=None))

        # Crossing the threshold fires exactly one warning.
        with self.assertLogs(agent_base_logger, level=WARNING) as captured:
            agent._track_reasoning_only_step(SimpleNamespace(tool_calls=None))
            agent._track_reasoning_only_step(SimpleNamespace(tool_calls=None))

        warning_messages = [
            r.getMessage() for r in captured.records if r.levelno == WARNING
        ]
        self.assertEqual(len(warning_messages), 1)
        self.assertIn("reasoning-only output", warning_messages[0])
        self.assertIn("vllm-project/vllm#36654", warning_messages[0])

        # A successful tool call resets the streak so future stalls warn again
        # for a fresh reason rather than going silent forever.
        agent._track_reasoning_only_step(SimpleNamespace(tool_calls=["x"]))
        self.assertEqual(agent._reasoning_only_streak, 0)

    def test_repatching_model_does_not_leak_chat_mode_recovery(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model, is_chat_mode=True)
        _patch_model_for_xml_tool_calls(model, is_chat_mode=False)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="I'll inspect the thread next and then report back.",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(parsed.tool_calls, [])
        self.assertEqual(
            parsed.content,
            "I'll inspect the thread next and then report back.",
        )


if __name__ == "__main__":
    unittest.main()
