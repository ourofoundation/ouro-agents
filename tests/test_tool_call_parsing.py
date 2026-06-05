import unittest
from types import SimpleNamespace

from smolagents.models import ChatMessage, MessageRole

from ouro_agents.tools.agent_base import (
    _EMPTY_MODEL_RESPONSE_ANSWER,
    _EMPTY_RESPONSE_MAX_RETRIES,
    _EMPTY_RESPONSE_NUDGE_OBSERVATION,
    _build_empty_narrated_tool_call_nudge_observation,
    _build_preamble_nudge_observation,
    _extract_raw_reasoning_text,
    _looks_like_empty_narrated_tool_call,
    _looks_like_preamble,
    _parse_dsml_tool_call_recovery,
    _parse_dsml_tool_calls,
    _parse_kimi_tool_call_recovery,
    _parse_minimax_tool_call_recovery,
    _parse_minimax_tool_calls,
    _parse_inline_tool_call,
    _parse_structured_tool_call_recovery,
    _parse_structured_tool_calls,
    _patch_model_for_xml_tool_calls,
    _recover_chat_final_answer,
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
        nudge = getattr(parsed, "_ouro_preamble_nudge_observation", None)
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
        nudge = getattr(parsed, "_ouro_preamble_nudge_observation", None)
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


class TestPreambleDetection(unittest.TestCase):
    """The agent should not auto-finalize on intermediate-thought content.

    Regression: hermes run 019dfdf9-6bf4-7fa8-a620-956309bf2962 emitted
    "Not pulling enough detail from memory — let me surface the actual
    results." as plain content; the chat-mode auto-coerce path turned that
    into a final_answer and ended the run before the actual search_assets
    call could execute.
    """

    def test_hermes_regression_phrase_is_preamble(self):
        self.assertTrue(
            _looks_like_preamble(
                "Not pulling enough detail from memory — let me surface the actual results."
            )
        )

    def test_classic_preamble_starts_match(self):
        for text in (
            "Let me check that.",
            "Let me look up the asset first.",
            "Let me pull up the screening data.",
            "Let's see what memory has.",
            "I'll search the team for related posts.",
            "I will run that route now.",
            "I'm going to verify the schema first.",
            "I need to load search_assets first.",
            "I should grab the latest results.",
            "Now let me fetch that.",
            "Next, I'll check the action logs.",
            "First, I need to inspect the route.",
            "Then I'll send the message.",
            "Okay, let me try a different query.",
            "Alright, I'll start by listing teams.",
            "So I'll pull up the post first.",
            "Wait, that's not quite right — let me retry.",
            "Hmm, that returned nothing useful.",
            "Actually, I should check memory first.",
            "One moment while I look that up.",
            "Give me a sec to pull that data.",
            "Thinking through the next step…",
            "Working on that now.",
            "Checking the team list.",
            "Looking up the route schema.",
            "Pulling up the most recent runs.",
            "Surfacing the actual screening data.",
            "Searching memory for prior results.",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    _looks_like_preamble(text),
                    f"Expected preamble: {text!r}",
                )

    def test_short_fragment_without_punctuation_is_preamble(self):
        # Status-update style: short, no terminal punctuation, no clear reply.
        self.assertTrue(_looks_like_preamble("checking memory now"))
        self.assertTrue(_looks_like_preamble("one sec"))

    def test_trailing_ellipsis_or_colon_is_preamble(self):
        self.assertTrue(
            _looks_like_preamble("Pulling up the results from last week...")
        )
        self.assertTrue(
            _looks_like_preamble("Pulling up the results from last week…")
        )
        self.assertTrue(
            _looks_like_preamble("Here are the candidates I want to check:")
        )

    def test_real_replies_are_not_preamble(self):
        for text in (
            "Hey! I can help with <that> if you want.",
            "Sure - use {'key': 'value'} as an example payload.",
            (
                "Correct — no active quests. The last three plans all hit "
                "\"success\" and wrapped up. Want me to scope a new direction?"
            ),
            "The screening returned 4 candidates: Mn2Sb, MnAlGe, MgMnGe, KMnP.",
            "Done. Posted the update at https://example.com/post/abc.",
            "No, that asset doesn't exist on this team.",
            "Yes — Fe2AlB2 passed Gate 1 with hull energy 0.0 eV/atom.",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    _looks_like_preamble(text),
                    f"Did not expect preamble: {text!r}",
                )

    def test_long_reply_with_incidental_let_me_phrase_is_not_preamble(self):
        text = (
            "Here's the summary: the Cu2Sb pipeline returned 4 candidates and "
            "the MAB-phase work is complete. The next interesting direction is "
            "Fe-based MAX phases, where Fe2AlB2 has a published hull energy of "
            "0.0 eV/atom and a measured Tc above room temperature. Let me know "
            "if you want me to scope a screening campaign there, or pick a "
            "different family entirely. I'm happy to run either."
        )
        self.assertFalse(_looks_like_preamble(text))

    def test_empty_or_whitespace_is_not_preamble(self):
        self.assertFalse(_looks_like_preamble(""))
        self.assertFalse(_looks_like_preamble("   \n\t  "))


class TestRecoverChatFinalAnswer(unittest.TestCase):
    def test_returns_none_for_preamble_text(self):
        self.assertIsNone(
            _recover_chat_final_answer(
                "Not pulling enough detail from memory — let me surface the actual results.",
                None,
            )
        )

    def test_returns_text_for_real_reply(self):
        self.assertEqual(
            _recover_chat_final_answer("The answer is 42.", None),
            "The answer is 42.",
        )

    def test_returns_none_when_tool_calls_present(self):
        self.assertIsNone(
            _recover_chat_final_answer("any text", ["tool"])
        )

    def test_returns_none_for_empty(self):
        self.assertIsNone(_recover_chat_final_answer("", None))
        self.assertIsNone(_recover_chat_final_answer("   ", None))


class TestPreambleNudgePath(unittest.TestCase):
    """The patched parser should route preamble content to the nudge path
    (empty tool_calls + observation marker) instead of auto-finalizing.
    """

    def test_chat_mode_preamble_does_not_become_final_answer(self):
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model, is_chat_mode=True)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="Not pulling enough detail from memory — let me surface the actual results.",
        )

        parsed = model.parse_tool_calls(message)

        # Critical: NOT auto-coerced into final_answer.
        self.assertEqual(parsed.role, MessageRole.ASSISTANT)
        self.assertEqual(parsed.tool_calls, [])
        self.assertEqual(
            parsed.content,
            "Not pulling enough detail from memory — let me surface the actual results.",
        )

        # And: a nudge observation is stashed on the message for
        # SanitizedToolCallingAgent to surface on the next inference.
        nudge = getattr(parsed, "_ouro_preamble_nudge_observation", None)
        self.assertIsNotNone(nudge)
        self.assertIn("[runtime]", nudge)
        self.assertIn("intermediate thought", nudge)
        self.assertIn("final_answer", nudge)
        self.assertIn("Not pulling enough detail", nudge)

    def test_chat_mode_real_reply_still_coerced_to_final_answer(self):
        # Regression guard: the original auto-coerce still runs for genuine
        # replies (e.g. simple chat answers that don't trip preamble heuristics).
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model, is_chat_mode=True)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="Hey! I can help with <that> if you want.",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].function.name, "final_answer")
        self.assertEqual(
            parsed.tool_calls[0].function.arguments,
            {"answer": "Hey! I can help with <that> if you want."},
        )
        self.assertIsNone(
            getattr(parsed, "_ouro_preamble_nudge_observation", None),
        )

    def test_autonomous_mode_preamble_also_routes_to_nudge_path(self):
        # Preamble leakage is a model bug regardless of mode. In autonomous
        # mode we previously hit the reasoning-only branch which silently
        # continued; the nudge path is strictly better because it surfaces
        # an actionable hint to the model on the next step.
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model, is_chat_mode=False)

        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="Let me check the action logs first.",
        )

        parsed = model.parse_tool_calls(message)

        self.assertEqual(parsed.tool_calls, [])
        nudge = getattr(parsed, "_ouro_preamble_nudge_observation", None)
        self.assertIsNotNone(nudge)
        self.assertIn("Let me check", nudge)

    def test_nudge_observation_truncates_long_preview(self):
        long_text = "Let me " + ("really " * 100) + "check that."
        nudge = _build_preamble_nudge_observation(long_text)
        self.assertIn("[runtime]", nudge)
        self.assertIn("…", nudge)
        # Preview itself must be capped (independent of surrounding template).
        preview_marker = "    Let me really"
        self.assertIn(preview_marker, nudge)
        # The full original text is too long to fit in the preview.
        self.assertNotIn(long_text, nudge)

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
        message._ouro_preamble_nudge_observation = (
            "[runtime] preamble nudge for testing"
        )
        memory_step = SimpleNamespace(
            model_output_message=message,
            observations=None,
        )

        agent._inject_preamble_nudge_observation(memory_step)

        self.assertEqual(
            memory_step.observations,
            "[runtime] preamble nudge for testing",
        )
        # Marker is consumed so a re-run does not stack duplicates.
        self.assertFalse(
            hasattr(message, "_ouro_preamble_nudge_observation"),
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
        message._ouro_preamble_nudge_observation = "NUDGE"
        memory_step = SimpleNamespace(
            model_output_message=message,
            observations="prior observation",
        )

        agent._inject_preamble_nudge_observation(memory_step)

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

        agent._inject_preamble_nudge_observation(memory_step)

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
            "]<]minimax[>[<html><!DOCTYPE html>\n<html>\n  <body style=\"padding: 24px;\">"
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
        for blank in ("cc", "bcc", "scheduledAt", "attachments", "tags", "topicId", "replyTo"):
            self.assertNotIn(blank, args)

    def test_ignores_non_minimax_content(self):
        self.assertIsNone(
            _parse_minimax_tool_call_recovery("Just a normal reply with no tool call.")
        )

    def test_patched_parser_recovers_send_email_instead_of_final_answer(self):
        # The actual regression: in chat mode, a leaked MiniMax send_email must
        # be recovered as a real tool call, NOT coerced into final_answer text.
        model = _AlwaysFailsModel()
        _patch_model_for_xml_tool_calls(model, is_chat_mode=True)

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


if __name__ == "__main__":
    unittest.main()
