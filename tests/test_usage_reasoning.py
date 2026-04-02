import unittest
from types import SimpleNamespace

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
