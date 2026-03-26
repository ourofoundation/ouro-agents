import unittest
from types import SimpleNamespace

from ouro_agents.usage import UsageTracker, _wrap_stream, record_usage_from_response


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


if __name__ == "__main__":
    unittest.main()
