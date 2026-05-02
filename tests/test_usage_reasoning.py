import unittest
from types import SimpleNamespace

import smolagents.models as smol_models
from smolagents.models import ChatMessage, MessageRole

import ouro_agents.smolagents_patches  # noqa: F401
from ouro_agents.provider_reasoning import attach_reasoning_from_raw_response
from ouro_agents.tools.agent_base import _copy_step_reasoning_to_messages
from ouro_agents.usage import (
    MirroredUsageTracker,
    RunUsage,
    UsageTracker,
    _wrap_stream,
    record_usage_from_response,
    residual_main_usage,
)


def _message(**kwargs):
    return SimpleNamespace(**kwargs)


def _choice(**kwargs):
    return SimpleNamespace(**kwargs)


def _response(**kwargs):
    return SimpleNamespace(**kwargs)


class TestVisibleReasoningLogging(unittest.TestCase):
    def test_records_visible_reasoning_from_response_message(self):
        tracker = UsageTracker()
        seen: list[str] = []
        response = _response(
            id="resp_1",
            usage={"prompt_tokens": 10, "completion_tokens": 4},
            choices=[
                _choice(
                    message=_message(
                        content="done",
                        reasoning="first inspect the workspace, then call the tool",
                    )
                )
            ],
        )

        record_usage_from_response(
            response,
            tracker,
            reasoning_callback=seen.append,
        )

        self.assertEqual(
            seen,
            ["first inspect the workspace, then call the tool"],
        )
        self.assertEqual(tracker.total_input_tokens, 10)
        self.assertEqual(tracker.total_output_tokens, 4)

    def test_streaming_reasoning_is_assembled_before_logging(self):
        tracker = UsageTracker()
        seen: list[str] = []
        chunks = [
            _response(
                id="resp_stream",
                choices=[_choice(delta=_message(reasoning="inspect "))],
            ),
            _response(
                id="resp_stream",
                choices=[_choice(delta=_message(reasoning="the workspace"))],
            ),
            _response(
                id="resp_stream",
                usage={"prompt_tokens": 7, "completion_tokens": 3},
                choices=[_choice(delta=_message(content="done"))],
            ),
        ]

        streamed = list(
            _wrap_stream(
                iter(chunks),
                tracker,
                reasoning_callback=seen.append,
            )
        )

        self.assertEqual(len(streamed), 3)
        self.assertEqual(seen, ["inspect the workspace"])
        self.assertEqual(tracker.total_input_tokens, 7)
        self.assertEqual(tracker.total_output_tokens, 3)


class TestReasoningRoundTrip(unittest.TestCase):
    def test_attaches_reasoning_fields_from_raw_response(self):
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            raw=_response(
                choices=[
                    _choice(
                        message=_message(
                            reasoning_details=[
                                {"type": "reasoning.text", "text": "choose a lookup tool"}
                            ],
                        )
                    )
                ]
            ),
        )

        attach_reasoning_from_raw_response(message)

        self.assertEqual(
            message.reasoning_details,
            [{"type": "reasoning.text", "text": "choose a lookup tool"}],
        )

    def test_replays_step_reasoning_on_tool_call_message(self):
        source = ChatMessage(role=MessageRole.ASSISTANT, content=None)
        source.reasoning = "choose get_asset before writing"
        step = SimpleNamespace(model_output_message=source)
        messages = [
            ChatMessage(
                role=MessageRole.TOOL_CALL,
                content=[{"type": "text", "text": "Calling tools:\n[]"}],
            )
        ]

        _copy_step_reasoning_to_messages(step, messages)

        self.assertEqual(messages[0].reasoning, "choose get_asset before writing")

    def test_clean_message_list_preserves_reasoning_fields(self):
        message = ChatMessage(
            role=MessageRole.TOOL_CALL,
            content=[{"type": "text", "text": "Calling tools:\n[]"}],
        )
        message.reasoning_details = [
            {
                "type": "reasoning.text",
                "text": "choose search_assets",
                "format": "anthropic-claude-v1",
            }
        ]

        cleaned = smol_models.get_clean_message_list(
            [message],
            role_conversions=smol_models.tool_role_conversions,
        )

        self.assertEqual(cleaned[0]["role"], MessageRole.ASSISTANT)
        self.assertEqual(
            cleaned[0]["reasoning_details"],
            [
                {
                    "type": "reasoning.text",
                    "text": "choose search_assets",
                    "format": "anthropic-claude-v1",
                }
            ],
        )

    def test_clean_message_list_drops_unknown_format_reasoning_details(self):
        # DeepSeek/Parasail leaks reasoning with format="unknown" — those can't
        # be replayed, so we should strip them rather than ship them back.
        message = ChatMessage(
            role=MessageRole.TOOL_CALL,
            content=[{"type": "text", "text": "Calling tools:\n[]"}],
        )
        message.reasoning_details = [
            {"type": "reasoning.text", "text": "raw thinking", "format": "unknown"}
        ]
        message.reasoning = "raw thinking"

        cleaned = smol_models.get_clean_message_list(
            [message],
            role_conversions=smol_models.tool_role_conversions,
        )

        self.assertNotIn("reasoning_details", cleaned[0])
        # The plain reasoning string still survives so providers that accept
        # it ungoverned can use it.
        self.assertEqual(cleaned[0]["reasoning"], "raw thinking")

    def test_clean_message_list_dedupes_collapsed_reasoning_details(self):
        # When _copy_step_reasoning_to_messages sets the same fields onto
        # both the ASSISTANT (model_output) and TOOL_CALL messages of one
        # step, message-list collapse used to duplicate every detail entry.
        # Dedupe + index-sort prevents that and produces a stable order.
        details = [
            {
                "type": "reasoning.text",
                "text": "step b",
                "format": "anthropic-claude-v1",
                "index": 1,
            },
            {
                "type": "reasoning.text",
                "text": "step a",
                "format": "anthropic-claude-v1",
                "index": 0,
            },
        ]
        first = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=[{"type": "text", "text": "thinking..."}],
        )
        first.reasoning_details = list(details)
        second = ChatMessage(
            role=MessageRole.TOOL_CALL,
            content=[{"type": "text", "text": "Calling tools:\n[]"}],
        )
        second.reasoning_details = list(details)

        cleaned = smol_models.get_clean_message_list(
            [first, second],
            role_conversions=smol_models.tool_role_conversions,
        )

        self.assertEqual(len(cleaned), 1)
        merged = cleaned[0]["reasoning_details"]
        self.assertEqual(len(merged), 2)
        self.assertEqual([entry["index"] for entry in merged], [0, 1])

    def test_planning_step_reasoning_is_replayed_onto_assistant_message(self):
        # PlanningStep also has model_output_message; the helper should not
        # silently skip it just because it's not an ActionStep.
        from smolagents.memory import PlanningStep
        from smolagents.monitoring import Timing

        source = ChatMessage(role=MessageRole.ASSISTANT, content=None)
        source.reasoning = "outline three steps"
        step = PlanningStep(
            model_input_messages=[],
            model_output_message=source,
            plan="1. read 2. write 3. reply",
            timing=Timing(start_time=0.0, end_time=0.0),
        )

        messages = step.to_messages()
        _copy_step_reasoning_to_messages(step, messages)

        assistant_msgs = [m for m in messages if m.role == MessageRole.ASSISTANT]
        self.assertTrue(assistant_msgs)
        self.assertEqual(assistant_msgs[0].reasoning, "outline three steps")


class TestMirroredUsageTracking(unittest.TestCase):
    def test_mirrored_tracker_keeps_local_totals_and_updates_shared_tracker(self):
        shared = UsageTracker()
        local_a = MirroredUsageTracker(UsageTracker(), mirrors=[shared])
        local_b = MirroredUsageTracker(UsageTracker(), mirrors=[shared])

        response_a = _response(
            id="resp_a",
            usage={"prompt_tokens": 10, "completion_tokens": 4},
            choices=[],
        )
        response_b = _response(
            id="resp_b",
            usage={"prompt_tokens": 7, "completion_tokens": 3},
            choices=[],
        )

        record_usage_from_response(response_a, local_a)
        record_usage_from_response(response_b, local_b)

        self.assertEqual(local_a.total_input_tokens, 10)
        self.assertEqual(local_a.total_output_tokens, 4)
        self.assertEqual(local_b.total_input_tokens, 7)
        self.assertEqual(local_b.total_output_tokens, 3)
        self.assertEqual(shared.total_input_tokens, 17)
        self.assertEqual(shared.total_output_tokens, 7)

    def test_residual_main_usage_stays_zero_when_total_equals_parallel_subagents(self):
        shared = UsageTracker()
        local_a = MirroredUsageTracker(UsageTracker(), mirrors=[shared])
        local_b = MirroredUsageTracker(UsageTracker(), mirrors=[shared])

        record_usage_from_response(
            _response(id="resp_a", usage={"prompt_tokens": 10, "completion_tokens": 4}, choices=[]),
            local_a,
        )
        record_usage_from_response(
            _response(id="resp_b", usage={"prompt_tokens": 7, "completion_tokens": 3}, choices=[]),
            local_b,
        )

        total = RunUsage.from_tracker(shared, model_id="main-model")
        subagent_ledger = [
            ("research-a", RunUsage.from_tracker(local_a, model_id="sub-a")),
            ("research-b", RunUsage.from_tracker(local_b, model_id="sub-b")),
        ]

        residual = residual_main_usage(total, subagent_ledger, None)

        self.assertEqual(residual.input_tokens, 0)
        self.assertEqual(residual.output_tokens, 0)
        self.assertEqual(residual.total_tokens, 0)


if __name__ == "__main__":
    unittest.main()
