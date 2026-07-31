import unittest
from types import SimpleNamespace

from smolagents.models import ChatMessage, MessageRole

from ouro_agents.tools.agent_base import (
    _EMPTY_MODEL_RESPONSE_ANSWER,
    _EMPTY_RESPONSE_MAX_RETRIES,
    _EMPTY_RESPONSE_NUDGE_OBSERVATION,
    _build_empty_narrated_tool_call_nudge_observation,
    _extract_raw_reasoning_text,
    _looks_like_empty_narrated_tool_call,
    _parse_dsml_tool_call_recovery,
    _parse_dsml_tool_calls,
    _parse_kimi_tool_call_recovery,
    _parse_minimax_tool_call_recovery,
    _parse_minimax_tool_calls,
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
    def test_empty_model_response_nudges_on_first_attempt(self):
        model = _EmptyResponseModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(parsed.tool_calls, [])
        self.assertEqual(parsed.content, "")
        nudge = getattr(parsed, "_ouro_nudge_observation", None)
        self.assertIsNotNone(nudge)
        self.assertEqual(nudge, _EMPTY_RESPONSE_NUDGE_OBSERVATION)
        self.assertEqual(model._ouro_empty_response_streak, 1)

    def test_empty_model_response_terminates_after_max_retries(self):
        model = _EmptyResponseModel()
        _patch_model_for_xml_tool_calls(model)

        for _ in range(_EMPTY_RESPONSE_MAX_RETRIES - 1):
            msg = ChatMessage(role=MessageRole.ASSISTANT, content="")
            parsed = model.parse_tool_calls(msg)
            self.assertEqual(parsed.tool_calls, [])

        final_msg = ChatMessage(role=MessageRole.ASSISTANT, content="")
        parsed = model.parse_tool_calls(final_msg)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"answer": _EMPTY_MODEL_RESPONSE_ANSWER},
        )

    def test_empty_response_streak_resets_on_successful_parse(self):
        call_count = 0

        class _AlternatingModel:
            def parse_tool_calls(self, message):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise ValueError("Message contains no content and no tool calls")
                return message

        model = _AlternatingModel()
        _patch_model_for_xml_tool_calls(model)

        msg1 = ChatMessage(role=MessageRole.ASSISTANT, content="")
        model.parse_tool_calls(msg1)
        self.assertEqual(model._ouro_empty_response_streak, 1)

        msg2 = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="done",
            tool_calls=[],
        )
        model.parse_tool_calls(msg2)
        self.assertEqual(model._ouro_empty_response_streak, 0)

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

    def test_interleaved_thinking_step_continues_without_terminating(self):
        model = _EmptyResponseModel()
        _patch_model_for_xml_tool_calls(model)

        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=None,
                        reasoning="Let me check the team feeds next.",
                        reasoning_details=[
                            {
                                "type": "reasoning.text",
                                "text": "Let me check the team feeds next.",
                                "format": "unknown",
                            }
                        ],
                    ),
                )
            ]
        )

        for _ in range(_EMPTY_RESPONSE_MAX_RETRIES + 1):
            message = ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                raw=raw,
            )
            parsed = model.parse_tool_calls(message)

            self.assertEqual(parsed.role, MessageRole.ASSISTANT)
            self.assertEqual(parsed.tool_calls, [])
            self.assertEqual(parsed.content, "")
            nudge = getattr(parsed, "_ouro_nudge_observation", None)
            self.assertIsNotNone(nudge)
            self.assertIn("interleaved-thinking step", nudge)
            self.assertIn("end this step at an action boundary", nudge)
            self.assertIn("Let me check the team feeds next.", nudge)
            self.assertEqual(model._ouro_empty_response_streak, 0)

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
        self.assertEqual(
            parsed.tool_calls[0].function.arguments, {"answer": "NO_ACTION"}
        )

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
        self.assertEqual(
            parsed.tool_calls[0].function.arguments, {"answer": "NO_ACTION"}
        )

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

    def test_recovered_narrated_tool_call_scrubs_raw_content(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=(
                "Calling tools:\n"
                "[{'id': 'call_00', 'type': 'function', 'function': "
                "{'name': 'get_asset', 'arguments': {'id': 'asset-123'}}}]"
            ),
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "get_asset")
        self.assertEqual(parsed.tool_calls[0].function.arguments, {"id": "asset-123"})
        self.assertEqual(parsed.content, "")

    def test_empty_narrated_tool_call_gets_specific_nudge(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="Let me check for a draft first:\nCalling tools:\n[]",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(parsed.tool_calls, [])
        self.assertEqual(parsed.content, "")
        nudge = getattr(parsed, "_ouro_nudge_observation", None)
        self.assertIsNotNone(nudge)
        self.assertIn("empty tool list", nudge)
        self.assertIn("Do not write `Calling tools:`", nudge)

    def test_detects_empty_narrated_tool_call(self):
        self.assertTrue(_looks_like_empty_narrated_tool_call("Calling tools:\n[]"))
        self.assertFalse(
            _looks_like_empty_narrated_tool_call(
                "Calling tools:\n[{'function': {'name': 'get_asset', 'arguments': {}}}]"
            )
        )

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
            tool_calls[0].function.arguments["body"]["input"]["structure"]["sites"][1][
                "element"
            ],
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
            recovery.tool_calls[0].function.arguments["body"]["input"]["structure"][
                "formula"
            ],
            "Ce2Fe17",
        )
        self.assertFalse(
            recovery.tool_calls[0].function.arguments["body"]["input"]["options"][
                "relax"
            ]
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
            {"tool": "write_comment", "arguments": [
              {"parent_id": "abc"},
              {"content": "two dicts are ambiguous"}
            ]}
            """
        )

        self.assertIsNone(tool_calls)

    def test_unsalvageable_plain_content_ends_turn(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="I'll inspect the thread next and then report back.",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"answer": "I'll inspect the thread next and then report back."},
        )

    def test_parses_deepseek_dsml_tool_call_block(self):
        # The exact DSML format that vLLM (#36654) leaks into the reasoning
        # field for DeepSeek-V3.x/V4 when the upstream tool-call parser is
        # bypassed by the reasoning parser.
        recovery = _parse_dsml_tool_call_recovery(
            "<｜DSML｜function_calls>\n"
            '<｜DSML｜invoke name="get_asset">\n'
            '<｜DSML｜parameter name="id" string="true">019de031-c2a0-7d2f-a818-a6480296e4ab</｜DSML｜parameter>\n'
            '<｜DSML｜parameter name="detail" string="true">full</｜DSML｜parameter>\n'
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
            '<｜DSML｜invoke name="execute_route">'
            '<｜DSML｜parameter name="dry_run">true</｜DSML｜parameter>'
            '<｜DSML｜parameter name="limit">10</｜DSML｜parameter>'
            '<｜DSML｜parameter name="tags">["a","b"]</｜DSML｜parameter>'
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
            '<|DSML|invoke name="final_answer">'
            '<|DSML|parameter name="answer" string="true">done</|DSML|parameter>'
            "</|DSML|invoke>"
            "</|DSML|function_calls>"
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.tool_calls[0].function.name, "final_answer")
        self.assertEqual(recovery.tool_calls[0].function.arguments, {"answer": "done"})

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
            '<｜DSML｜invoke name="get_asset">'
            '<｜DSML｜parameter name="id" string="true">abc-123</｜DSML｜parameter>'
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
        self.assertEqual(parsed.tool_calls[0].function.arguments, {"id": "abc-123"})

    def test_recovers_tool_call_from_reasoning_details_text(self):
        # When the leaked tokens live inside ``reasoning_details[].text``
        # instead of the flat ``reasoning`` string, the salvage should still
        # find them.
        model = _EmptyResponseModel()
        _patch_model_for_xml_tool_calls(model)

        leaked = (
            "<｜DSML｜function_calls>"
            '<｜DSML｜invoke name="write_comment">'
            '<｜DSML｜parameter name="parent_id" string="true">post-1</｜DSML｜parameter>'
            '<｜DSML｜parameter name="content" string="true">ok</｜DSML｜parameter>'
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
                            {
                                "type": "reasoning.text",
                                "text": leaked,
                                "format": "unknown",
                            }
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
        self.assertEqual(parsed.tool_calls[0].function.name, "write_comment")
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
            for _ in range(
                SanitizedToolCallingAgent._REASONING_ONLY_WARN_THRESHOLD - 1
            ):
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

class TestPlainContentEndsTurn(unittest.TestCase):
    """Plain assistant content with no tool calls is the terminal reply.

    The standard tool-calling convention in every mode: tool calls continue
    the turn, content alone ends it (via the synthesized internal stop signal).
    """

    def _assert_plain_content_ends_turn(self, content: str):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        parsed = model.parse_tool_calls(
            ChatMessage(role=MessageRole.ASSISTANT, content=content)
        )

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(parsed.tool_calls[0].function.arguments, {"answer": content})

    def test_plain_content_ends_turn(self):
        self._assert_plain_content_ends_turn("Hey! I can help with that.")
        self._assert_plain_content_ends_turn(
            "The key change is that gold and silver both broke below their "
            "200-day exponential moving averages, which makes the short-term "
            "technical picture weaker than it looked during the Q1 advance."
        )
        self._assert_plain_content_ends_turn(
            "Sure - use {'key': 'value'} as an example payload."
        )

    def test_markdown_function_mention_is_not_a_tool_call(self):
        # Hermes mentioned `open()` while summarizing a completed memory
        # update; inline recovery must not interpret prose as a tool call.
        self._assert_plain_content_ends_turn(
            "All set. The stale `open()` fact was replaced with the current "
            "sandbox status."
        )

    def test_empty_narrated_tool_call_nudge_truncates_long_preview(self):
        long_text = "Calling tools:\n[]" + (" extra" * 100)
        nudge = _build_empty_narrated_tool_call_nudge_observation(long_text)
        self.assertIn("empty tool list", nudge)
        self.assertIn("…", nudge)
        self.assertNotIn(long_text, nudge)


class TestPreambleNudgeStepHook(unittest.TestCase):
    """SanitizedToolCallingAgent should convert the marker into an observation
    so the next inference sees a TOOL_RESPONSE corrective hint.
    """

    def test_step_hook_promotes_marker_to_observation(self):
        from smolagents import tool

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
        )

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="Let me search the team next.",
        )
        message._ouro_nudge_observation = (
            "[runtime] preamble nudge for testing"
        )
        memory_step = SimpleNamespace(
            model_output_message=message,
            observations=None,
        )

        agent._inject_nudge_observation(memory_step)

        self.assertEqual(
            memory_step.observations,
            "[runtime] preamble nudge for testing",
        )
        # Marker is consumed so a re-run does not stack duplicates.
        self.assertFalse(
            hasattr(message, "_ouro_nudge_observation"),
        )

    def test_step_hook_appends_to_existing_observations(self):
        from smolagents import tool

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
        )

        message = ChatMessage(role=MessageRole.ASSISTANT, content="x")
        message._ouro_nudge_observation = "NUDGE"
        memory_step = SimpleNamespace(
            model_output_message=message,
            observations="prior observation",
        )

        agent._inject_nudge_observation(memory_step)

        self.assertIn("prior observation", memory_step.observations)
        self.assertIn("NUDGE", memory_step.observations)

    def test_step_hook_is_noop_without_marker(self):
        from smolagents import tool

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
        )

        message = ChatMessage(role=MessageRole.ASSISTANT, content="x")
        memory_step = SimpleNamespace(
            model_output_message=message,
            observations=None,
        )

        agent._inject_nudge_observation(memory_step)

        self.assertIsNone(memory_step.observations)

    def test_step_budget_observation_warns_near_end(self):
        from smolagents import tool

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
            max_steps=20,
        )
        memory_step = SimpleNamespace(
            step_number=15,
            observations=None,
            is_final_answer=False,
        )

        agent._inject_step_budget_observation(memory_step)

        self.assertIn("completed 15/20", memory_step.observations)
        self.assertIn("5 steps remain", memory_step.observations)
        self.assertIn("Begin converging now", memory_step.observations)

    def test_final_answer_hidden_from_model_tool_schemas(self):
        from smolagents import tool

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
        )

        advertised = {t.name for t in agent.tools_and_managed_agents}
        self.assertIn("sample_tool", advertised)
        self.assertNotIn("final_answer", advertised)
        # The tool stays registered internally as smolagents' stop signal.
        self.assertIn("final_answer", agent.tools)

    def test_step_budget_observation_tells_last_step_to_close(self):
        from smolagents import tool

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
            max_steps=20,
        )
        memory_step = SimpleNamespace(
            step_number=19,
            observations="prior",
            is_final_answer=False,
        )

        agent._inject_step_budget_observation(memory_step)

        self.assertIn("prior", memory_step.observations)
        self.assertIn("completed 19/20", memory_step.observations)
        self.assertIn("1 step remains", memory_step.observations)
        self.assertIn("last available next step", memory_step.observations)
        self.assertIn("deliver your final reply", memory_step.observations)
        self.assertNotIn("create/update/comment", memory_step.observations)
        self.assertNotIn("save the artifact", memory_step.observations)

    def test_step_budget_observation_is_quiet_before_threshold(self):
        from smolagents import tool

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def sample_tool() -> str:
            """Sample tool that exists so the agent can be constructed."""
            return "ok"

        agent = SanitizedToolCallingAgent(
            tools=[sample_tool],
            model=_FakeModelWithToolCalls(),
            max_steps=20,
        )
        memory_step = SimpleNamespace(
            step_number=14,
            observations=None,
            is_final_answer=False,
        )

        agent._inject_step_budget_observation(memory_step)

        self.assertIsNone(memory_step.observations)


class TestMiniMaxToolCallParsing(unittest.TestCase):
    """MiniMax M2/M2.1 emit XML tool calls that some OpenRouter routes fail to
    parse natively, leaking the tokens into content. Without a salvage parser
    these get coerced into ``final_answer`` and the real tool never runs (e.g.
    a ``send_email`` that silently never sends).
    """

    def test_parses_canonical_minimax_tool_call(self):
        recovery = _parse_minimax_tool_call_recovery(
            "<minimax:tool_call>\n"
            '<invoke name="get_weather">\n'
            '<parameter name="location">San Francisco</parameter>\n'
            '<parameter name="unit">celsius</parameter>\n'
            "</invoke>\n"
            "</minimax:tool_call>"
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(len(recovery.tool_calls), 1)
        self.assertEqual(recovery.tool_calls[0].function.name, "get_weather")
        self.assertEqual(
            recovery.tool_calls[0].function.arguments,
            {"location": "San Francisco", "unit": "celsius"},
        )

    def test_canonical_parameter_json_values_are_decoded(self):
        tool_calls = _parse_minimax_tool_calls(
            "<minimax:tool_call>"
            '<invoke name="search_web">'
            '<parameter name="query_tag">["technology", "events"]</parameter>'
            '<parameter name="query">OpenAI latest release</parameter>'
            "</invoke>"
            "</minimax:tool_call>"
        )

        self.assertIsNotNone(tool_calls)
        self.assertEqual(tool_calls[0].function.name, "search_web")
        self.assertEqual(
            tool_calls[0].function.arguments,
            {"query_tag": ["technology", "events"], "query": "OpenAI latest release"},
        )

    def test_parses_multiple_invoke_blocks(self):
        tool_calls = _parse_minimax_tool_calls(
            "<minimax:tool_call>"
            '<invoke name="get_weather"><parameter name="location">Paris</parameter></invoke>'
            '<invoke name="get_weather"><parameter name="location">New York</parameter></invoke>'
            "</minimax:tool_call>"
        )

        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0].function.arguments, {"location": "Paris"})
        self.assertEqual(tool_calls[1].function.arguments, {"location": "New York"})

    def test_recovers_degenerate_bare_tag_send_email(self):
        # Exact shape captured from a hermes run: minimax-m3 dumped the
        # send_email JSON schema as bare XML tags separated by the
        # ``]<]minimax[>[`` artifact, with an <html> value that itself contains
        # a nested <html>...</html> document.
        content = (
            "Tools used:\n- Loaded tools: resend:send_email\n\n"
            "[TOOL_CALL]\n"
            '{ tool = "send_email", args = { to = ["matt@ouro.foundation"],'
            'subject = "Test from Hermes",text = "Hey Matt",html = "<tool_call>\n'
            ']<]minimax[>[<invoke name="send_email">'
            "]<]minimax[>[<to>]<]minimax[>[<item>matt@ouro.foundation]<]minimax[>[</item>"
            "]<]minimax[>[</to>]<]minimax[>[<subject>Test from Hermes]<]minimax[>[</subject>"
            "]<]minimax[>[<text>Hey Matt — test 🚀\n\n— Hermes]<]minimax[>[</text>"
            ']<]minimax[>[<html><!DOCTYPE html>\n<html>\n  <body style="padding: 24px;">'
            "<h2>Hey Matt</h2></body>\n</html>]<]minimax[>[</html>"
            "]<]minimax[>[<cc>]<]minimax[>[</cc>]<]minimax[>[<bcc>]<]minimax[>[</bcc>"
            "]<]minimax[>[<scheduledAt>]<]minimax[>[</scheduledAt>"
            "]<]minimax[>[<attachments>]<]minimax[>[</attachments>"
            "]<]minimax[>[<tags>]<]minimax[>[</tags>"
            "]<]minimax[>[<topicId>]<]minimax[>[</topicId>"
            "]<]minimax[>[<replyTo>]<]minimax[>[</replyTo>"
            "]<]minimax[>[</invoke>\n]<]minimax[>[</tool_call>"
        )

        recovery = _parse_minimax_tool_call_recovery(content)

        self.assertIsNotNone(recovery)
        self.assertEqual(len(recovery.tool_calls), 1)
        call = recovery.tool_calls[0]
        self.assertEqual(call.function.name, "send_email")
        args = call.function.arguments
        self.assertEqual(args["to"], ["matt@ouro.foundation"])
        self.assertEqual(args["subject"], "Test from Hermes")
        self.assertEqual(args["text"], "Hey Matt — test 🚀\n\n— Hermes")
        # The full nested HTML document is captured intact.
        self.assertIn("<!DOCTYPE html>", args["html"])
        self.assertIn("<h2>Hey Matt</h2>", args["html"])
        self.assertTrue(args["html"].rstrip().endswith("</html>"))
        # Empty schema fields are dropped, not sent as blank strings.
        for blank in (
            "cc",
            "bcc",
            "scheduledAt",
            "attachments",
            "tags",
            "topicId",
            "replyTo",
        ):
            self.assertNotIn(blank, args)

    def test_ignores_non_minimax_content(self):
        self.assertIsNone(
            _parse_minimax_tool_call_recovery("Just a normal reply with no tool call.")
        )

    def test_patched_parser_recovers_send_email_instead_of_final_answer(self):
        # The actual regression: in chat mode, a leaked MiniMax send_email must
        # be recovered as a real tool call, NOT coerced into final_answer text.
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model)

        content = (
            '<minimax:tool_call><invoke name="send_email">'
            '<parameter name="to">["matt@ouro.foundation"]</parameter>'
            '<parameter name="subject">Test from Hermes</parameter>'
            "</invoke></minimax:tool_call>"
        )
        message = ChatMessage(role=MessageRole.ASSISTANT, content=content)

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "send_email")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"to": ["matt@ouro.foundation"], "subject": "Test from Hermes"},
        )


class TestRunObservationCompaction(unittest.TestCase):
    """One-shot compact when cumulative observations cross the ceiling."""

    @staticmethod
    def _make_steps(count, observation_chars, prefix="step"):
        from smolagents import ActionStep
        from smolagents.monitoring import Timing

        return [
            ActionStep(
                step_number=i,
                timing=Timing(start_time=0.0, end_time=0.0),
                observations=f"{prefix}-{i} " + "x" * observation_chars,
            )
            for i in range(count)
        ]

    @staticmethod
    def _fake_agent(steps, policy=None):
        import types

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent
        from ouro_agents.tools.observation_policy import ObservationPolicy

        fake = types.SimpleNamespace(
            memory=types.SimpleNamespace(steps=steps),
            _observation_policy=policy or ObservationPolicy(),
            _observation_compact_done=False,
        )
        fake._maybe_one_shot_compact_observations = (
            SanitizedToolCallingAgent._maybe_one_shot_compact_observations.__get__(
                fake
            )
        )
        return fake

    def test_one_shot_folds_oldest_when_over_ceiling(self):
        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            RUN_COMPACT_MARKER,
        )

        policy = ObservationPolicy(
            run_compact_ceiling=20_000,
            keep_recent_steps=3,
            excerpt_chars=200,
        )
        # 10 steps x 5k = 50k > 20k ceiling
        steps = self._make_steps(10, 5_000)
        originals = [s.observations for s in steps]
        agent = self._fake_agent(steps, policy)

        agent._maybe_one_shot_compact_observations()

        self.assertTrue(agent._observation_compact_done)
        self.assertIn(RUN_COMPACT_MARKER, steps[0].observations)
        for step in steps[-policy.keep_recent_steps :]:
            self.assertNotIn(RUN_COMPACT_MARKER, step.observations)
            self.assertEqual(step.observations, originals[step.step_number])

    def test_noop_under_ceiling_keeps_history_stable(self):
        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            RUN_COMPACT_MARKER,
        )

        policy = ObservationPolicy(run_compact_ceiling=100_000, keep_recent_steps=3)
        steps = self._make_steps(10, 1_000)
        originals = [s.observations for s in steps]
        agent = self._fake_agent(steps, policy)

        agent._maybe_one_shot_compact_observations()

        self.assertFalse(agent._observation_compact_done)
        for step, original in zip(steps, originals):
            self.assertEqual(step.observations, original)
            self.assertNotIn(RUN_COMPACT_MARKER, step.observations)

    def test_preserves_spill_paths_when_folding(self):
        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            RUN_COMPACT_MARKER,
        )

        spill_header = (
            "[tool output spilled: 41,625 chars → "
            "scratch/tool-outputs/run/0016-run_shell.txt]\n"
        )
        policy = ObservationPolicy(
            run_compact_ceiling=5_000,
            keep_recent_steps=2,
            excerpt_chars=100,
            max_inline_chars=5_000,
        )
        steps = self._make_steps(5, 2_000)
        steps[0].observations = spill_header + ("y" * 3_000)
        agent = self._fake_agent(steps, policy)

        agent._maybe_one_shot_compact_observations()

        self.assertIn(RUN_COMPACT_MARKER, steps[0].observations)
        self.assertIn(
            "scratch/tool-outputs/run/0016-run_shell.txt",
            steps[0].observations,
        )
        self.assertIn("5,000-char inline limit", steps[0].observations)


class TestObservationSpill(unittest.TestCase):
    def test_spill_creates_file_and_stub(self):
        import tempfile
        from pathlib import Path

        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            SPILL_MARKER_PREFIX,
            maybe_spill_and_stub,
        )

        policy = ObservationPolicy(
            max_inline_chars=100,
            head_chars=20,
            tail_chars=10,
        )
        text = "HEAD" + ("body" * 500) + "TAIL_END!!"
        with tempfile.TemporaryDirectory() as tmp:
            stub = maybe_spill_and_stub(
                text,
                tool_name="run_shell",
                workspace=Path(tmp),
                run_id="test-run",
                policy=policy,
            )
            self.assertIn(SPILL_MARKER_PREFIX, stub)
            self.assertIn("scratch/tool-outputs/test-run/", stub)
            self.assertIn("Inline limit is 100 chars", stub)
            self.assertIn("do not cat the whole file", stub)
            self.assertLess(len(stub), len(text))
            # Full payload on disk
            spill_files = list(Path(tmp).joinpath("scratch/tool-outputs/test-run").glob("*"))
            self.assertEqual(len(spill_files), 1)
            self.assertEqual(spill_files[0].read_text(), text)

    def test_under_inline_limit_unchanged(self):
        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            maybe_spill_and_stub,
        )

        policy = ObservationPolicy(max_inline_chars=1_000)
        text = "short result"
        out = maybe_spill_and_stub(
            text,
            tool_name="get_asset",
            workspace=None,
            run_id="r",
            policy=policy,
        )
        self.assertEqual(out, text)

    def test_exempt_tools_never_spill(self):
        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            SPILL_MARKER_PREFIX,
            maybe_spill_and_stub,
        )

        policy = ObservationPolicy(
            max_inline_chars=100,
            exempt_tools=("load_skill",),
        )
        text = "SKILL BODY " + ("x" * 5_000)
        out = maybe_spill_and_stub(
            text,
            tool_name="load_skill",
            workspace=None,
            run_id="r",
            policy=policy,
        )
        self.assertEqual(out, text)
        self.assertNotIn(SPILL_MARKER_PREFIX, out)

    def test_empty_exempt_list_spills_skills(self):
        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            SPILL_MARKER_PREFIX,
            maybe_spill_and_stub,
        )

        policy = ObservationPolicy(max_inline_chars=100, exempt_tools=())
        text = "SKILL BODY " + ("x" * 5_000)
        out = maybe_spill_and_stub(
            text,
            tool_name="load_skill",
            workspace=None,
            run_id="r",
            policy=policy,
        )
        self.assertIn(SPILL_MARKER_PREFIX, out)

    def test_step_budget_spills_combined(self):
        import tempfile
        from pathlib import Path

        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            SPILL_MARKER_PREFIX,
            enforce_step_budget,
        )

        policy = ObservationPolicy(
            max_inline_chars=50,
            max_step_chars=80,
            head_chars=15,
            tail_chars=10,
        )
        combined = "a" * 5_000
        with tempfile.TemporaryDirectory() as tmp:
            out = enforce_step_budget(
                combined,
                tool_name="step",
                workspace=Path(tmp),
                run_id="r",
                policy=policy,
            )
            self.assertIn(SPILL_MARKER_PREFIX, out)
            self.assertLess(len(out), len(combined))
            spill_files = list(Path(tmp).joinpath("scratch/tool-outputs/r").glob("*"))
            self.assertEqual(len(spill_files), 1)
            self.assertEqual(spill_files[0].read_text(), combined)

    def test_labeled_step_budget_preserves_sections(self):
        import tempfile
        from pathlib import Path

        from ouro_agents.tools.observation_policy import (
            ObservationPolicy,
            SPILL_MARKER_PREFIX,
            enforce_step_budget,
        )

        policy = ObservationPolicy(
            max_inline_chars=50,
            max_step_chars=80,
            head_chars=15,
            tail_chars=10,
            exempt_tools=("load_skill",),
        )
        skill_body = "SKILL-" + ("s" * 200)
        shell_body = "SHELL-" + ("x" * 200)
        labeled = (
            "=== Tool result: load_skill (id=call_skill) ===\n"
            f"{skill_body}\n"
            "=== Tool result: run_shell (id=call_shell) ===\n"
            f"{shell_body}"
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = enforce_step_budget(
                labeled,
                tool_name="step",
                workspace=Path(tmp),
                run_id="r",
                policy=policy,
            )
            self.assertIn("=== Tool result: load_skill (id=call_skill) ===", out)
            self.assertIn(skill_body, out)  # exempt stays full
            self.assertIn("=== Tool result: run_shell (id=call_shell) ===", out)
            self.assertIn(SPILL_MARKER_PREFIX, out)
            self.assertNotIn(shell_body, out)  # oversized non-exempt spilled


class TestParallelObservationLabeling(unittest.TestCase):
    """Parallel tool results get per-call headers before concatenation."""

    @staticmethod
    def _tool_call(call_id: str, name: str):
        from smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction

        return ChatMessageToolCall(
            id=call_id,
            type="function",
            function=ChatMessageToolCallFunction(name=name, arguments={}),
        )

    def test_labels_each_parallel_observation(self):
        from smolagents import tool
        from smolagents.memory import ActionStep, Timing

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent
        from ouro_agents.utils.tool_observations import get_step_tool_results

        @tool
        def alpha() -> str:
            """First parallel tool."""
            return "alpha-result"

        @tool
        def beta() -> str:
            """Second parallel tool."""
            return "beta-result"

        agent = SanitizedToolCallingAgent(
            tools=[alpha, beta],
            model=_FakeModelWithToolCalls(),
        )
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[
                self._tool_call("call_a", "alpha"),
                self._tool_call("call_b", "beta"),
            ],
        )
        memory_step = ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.0),
        )

        outputs = list(agent.process_tool_calls(message, memory_step))
        tool_outputs = [o for o in outputs if getattr(o, "observation", None)]
        self.assertEqual(len(tool_outputs), 2)
        by_id = {o.id: o.observation for o in tool_outputs}
        self.assertTrue(
            by_id["call_a"].startswith("=== Tool result: alpha (id=call_a) ===")
        )
        self.assertIn("alpha-result", by_id["call_a"])
        self.assertTrue(
            by_id["call_b"].startswith("=== Tool result: beta (id=call_b) ===")
        )
        self.assertIn("beta-result", by_id["call_b"])
        self.assertIn("=== Tool result: alpha (id=call_a) ===", memory_step.observations)
        self.assertIn("=== Tool result: beta (id=call_b) ===", memory_step.observations)
        stored = get_step_tool_results(memory_step)
        self.assertEqual(stored["call_a"], "alpha-result")
        self.assertEqual(stored["call_b"], "beta-result")

    def test_single_call_is_unlabeled(self):
        from smolagents import tool
        from smolagents.memory import ActionStep, Timing

        from ouro_agents.tools.agent_base import SanitizedToolCallingAgent

        @tool
        def alone() -> str:
            """Single tool call."""
            return "solo"

        agent = SanitizedToolCallingAgent(
            tools=[alone],
            model=_FakeModelWithToolCalls(),
        )
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[self._tool_call("call_1", "alone")],
        )
        memory_step = ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.0),
        )

        outputs = list(agent.process_tool_calls(message, memory_step))
        tool_outputs = [o for o in outputs if getattr(o, "observation", None)]
        self.assertEqual(len(tool_outputs), 1)
        self.assertEqual(tool_outputs[0].observation, "solo")
        self.assertNotIn("=== Tool result:", memory_step.observations or "")


if __name__ == "__main__":
    unittest.main()
