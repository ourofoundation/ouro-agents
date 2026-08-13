"""Tests for the intermediate content streamer + observer wiring.

Covers the path that turns each step's assistant content into a distinct
conversation message — both as live websocket chunks and as a persisted
Ouro message — so users see a chat-style timeline matching the work the
agent actually did. The last step is the same path, persisted as turn_final.
"""

import unittest
from types import SimpleNamespace

from smolagents import ChatMessageStreamDelta

from ouro_agents.observer import AgentObserver
from ouro_agents.utils.streaming import IntermediateContentStreamer


def _content_delta(text: str) -> ChatMessageStreamDelta:
    return ChatMessageStreamDelta(content=text)


def _tool_arg_delta(
    *, index: int = 0, name: str | None = None, arguments: str | None = None
) -> ChatMessageStreamDelta:
    function = SimpleNamespace(name=name, arguments=arguments)
    tool_call = SimpleNamespace(index=index, function=function)
    return ChatMessageStreamDelta(tool_calls=[tool_call])


class TestIntermediateContentStreamer(unittest.TestCase):
    def test_emits_chunks_in_order_for_plain_content(self):
        streamer = IntermediateContentStreamer()

        chunks = []
        for text in ("Looking at ", "recent quests", " first."):
            chunk = streamer.consume(_content_delta(text))
            if chunk:
                chunks.append(chunk)

        self.assertEqual(
            "".join(chunks),
            "Looking at recent quests first.",
        )
        self.assertTrue(streamer.has_streamed)
        self.assertEqual(
            streamer.buffered_text,
            "Looking at recent quests first.",
        )

    def test_returns_none_on_empty_or_tool_only_deltas(self):
        streamer = IntermediateContentStreamer()
        self.assertIsNone(streamer.consume(_content_delta("")))
        self.assertIsNone(streamer.consume(_content_delta(None)))
        self.assertIsNone(
            streamer.consume(_tool_arg_delta(name="search_assets")),
        )
        self.assertFalse(streamer.has_streamed)

    def test_flush_returns_full_text_and_resets(self):
        streamer = IntermediateContentStreamer()
        streamer.consume(_content_delta("Found 3 candidates."))

        full = streamer.flush()

        self.assertEqual(full, "Found 3 candidates.")
        self.assertFalse(streamer.has_streamed)
        self.assertEqual(streamer.buffered_text, "")

        # After flush the streamer is reusable for the next step.
        streamer.consume(_content_delta("Next step text."))
        self.assertEqual(streamer.buffered_text, "Next step text.")

    def test_reset_drops_buffer_without_returning(self):
        streamer = IntermediateContentStreamer()
        streamer.consume(_content_delta("hello"))
        streamer.reset()
        self.assertFalse(streamer.has_streamed)
        self.assertEqual(streamer.buffered_text, "")

    def test_streams_plain_content_that_mentions_final_answer(self):
        # Content is the reply now; mentioning the old tool name must not hide it.
        streamer = IntermediateContentStreamer()
        chunk = streamer.consume(
            _content_delta('{"name": "final_answer", "arguments": {"answer": "Hi."}}')
        )
        self.assertEqual(
            chunk, '{"name": "final_answer", "arguments": {"answer": "Hi."}}'
        )
        self.assertTrue(streamer.has_streamed)

    def test_suppresses_narrated_calling_tools_block(self):
        # GLM (and other models) imitate smolagents' own
        # "Calling tools:\n[{...}]" step rendering and emit it in the content
        # channel alongside real native tool calls. The preamble before it is
        # genuine commentary and should survive; the narrated block must not
        # reach the conversation.
        streamer = IntermediateContentStreamer()

        first = streamer.consume(
            _content_delta("Let me get the details on the main activity runs from today.")
        )
        self.assertEqual(
            first, "Let me get the details on the main activity runs from today."
        )

        suppressed = streamer.consume(
            _content_delta(
                "\nCalling tools:\n[{'id': 'call_2b5c', 'type': 'function', "
                "'function': {'name': 'get_run_detail', 'arguments': {}}}]"
            )
        )
        self.assertIsNone(suppressed)
        # Persisted text is the clean preamble only — no narrated block.
        self.assertEqual(
            streamer.buffered_text,
            "Let me get the details on the main activity runs from today.",
        )
        # Remains suppressed for the rest of the step.
        self.assertIsNone(streamer.consume(_content_delta(" more")))

    def test_suppresses_narrated_block_in_single_delta(self):
        # Whole content (preamble + narrated block) arrives at once.
        streamer = IntermediateContentStreamer()
        chunk = streamer.consume(
            _content_delta(
                "Checking the run.\n**Calling tools:**\n[{'id': 'call_x'}]"
            )
        )
        self.assertEqual(chunk, "Checking the run.")
        self.assertEqual(streamer.buffered_text, "Checking the run.")

    def test_holds_back_partial_marker_prefix(self):
        # The marker can arrive split across deltas; never stream a partial
        # "Calling" that later resolves to the marker.
        streamer = IntermediateContentStreamer()
        first = streamer.consume(_content_delta("Done. Calling"))
        # "Calling" is a prefix of the marker, so it's withheld.
        self.assertEqual(first, "Done. ")
        second = streamer.consume(_content_delta(" tools:\n[{'id': 'call_y'}]"))
        self.assertIsNone(second)
        self.assertEqual(streamer.buffered_text, "Done.")

    def test_does_not_double_emit_overlapping_buffers(self):
        # The streamer must only emit each character once even if upstream
        # accidentally re-sends an overlapping prefix.
        streamer = IntermediateContentStreamer()

        chunks: list[str] = []
        chunks.append(streamer.consume(_content_delta("Hello")) or "")
        # Same buffer state plus a tail — caller's bug, but the streamer
        # should not double-emit ``Hello``.
        same = streamer.consume(_content_delta(" world."))
        chunks.append(same or "")

        self.assertEqual("".join(chunks), "Hello world.")


class _RecordingObserver(AgentObserver):
    def __init__(self):
        self.intermediate_chunks: list[tuple[str, str]] = []
        self.intermediate_ends: list[tuple[str, str, bool]] = []

    def on_intermediate_chunk(self, message_id: str, chunk: str) -> None:
        self.intermediate_chunks.append((message_id, chunk))

    def on_intermediate_end(
        self, message_id: str, full_text: str, turn_final: bool = False
    ) -> None:
        self.intermediate_ends.append((message_id, full_text, turn_final))


class TestRunLoopFlushBehaviour(unittest.TestCase):
    """Reproduce the agent.py loop's flush logic in isolation.

    The actual loop lives inline in ``OuroAgent._run_in_inner_agent``;
    rather than spinning up a real model, this test inlines the same
    flush/dispatch contract so we can verify message-id and persistence
    semantics independently of smolagents internals.
    """

    def _run(self, events, *, observer):
        from ouro_agents.uuid_v7 import uuid7_str

        streamer = IntermediateContentStreamer()
        current_id: str | None = None

        def flush(*, turn_final: bool) -> None:
            nonlocal current_id
            if current_id is None:
                streamer.reset()
                return
            text = streamer.flush()
            mid = current_id
            current_id = None
            if not text.strip():
                return
            observer.on_intermediate_end(mid, text, turn_final)

        for ev in events:
            if isinstance(ev, ChatMessageStreamDelta):
                chunk = streamer.consume(ev)
                if chunk:
                    if current_id is None:
                        current_id = uuid7_str()
                    observer.on_intermediate_chunk(current_id, chunk)
            elif isinstance(ev, _StepBoundary):
                flush(turn_final=ev.is_final_answer)
        flush(turn_final=False)

    def test_each_non_final_step_persists_with_unique_message_id(self):
        observer = _RecordingObserver()
        events = [
            _content_delta("Looking at recent quests."),
            _StepBoundary(is_final_answer=False),
            _content_delta("Found three candidates — checking each."),
            _StepBoundary(is_final_answer=False),
        ]

        self._run(events, observer=observer)

        # Two end events, one per step, with distinct ids.
        self.assertEqual(len(observer.intermediate_ends), 2)
        ids = [mid for mid, _, _ in observer.intermediate_ends]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(
            observer.intermediate_ends[0][1],
            "Looking at recent quests.",
        )
        self.assertEqual(
            observer.intermediate_ends[1][1],
            "Found three candidates — checking each.",
        )
        self.assertFalse(observer.intermediate_ends[0][2])
        self.assertFalse(observer.intermediate_ends[1][2])

        # Streamed chunks share the message id of the step they belong to.
        chunk_ids_step1 = {
            mid for mid, _ in observer.intermediate_chunks if mid == ids[0]
        }
        chunk_ids_step2 = {
            mid for mid, _ in observer.intermediate_chunks if mid == ids[1]
        }
        self.assertEqual(chunk_ids_step1, {ids[0]})
        self.assertEqual(chunk_ids_step2, {ids[1]})

    def test_final_step_persists_content_as_turn_final(self):
        observer = _RecordingObserver()
        events = [
            _content_delta("Looking at recent quests."),
            _StepBoundary(is_final_answer=False),
            _content_delta("Here's the answer."),
            _StepBoundary(is_final_answer=True),
        ]

        self._run(events, observer=observer)

        self.assertEqual(len(observer.intermediate_ends), 2)
        self.assertEqual(observer.intermediate_ends[0][1], "Looking at recent quests.")
        self.assertFalse(observer.intermediate_ends[0][2])
        self.assertEqual(observer.intermediate_ends[1][1], "Here's the answer.")
        self.assertTrue(observer.intermediate_ends[1][2])

    def test_step_with_no_content_persists_nothing(self):
        observer = _RecordingObserver()
        events = [_StepBoundary(is_final_answer=False)]

        self._run(events, observer=observer)

        self.assertEqual(observer.intermediate_chunks, [])
        self.assertEqual(observer.intermediate_ends, [])

    def test_trailing_step_without_action_step_marker_is_flushed(self):
        # Safety net: even if the run terminates without a final ActionStep
        # (e.g. AgentMaxStepsError), the flush at end-of-loop persists any
        # streamed commentary so the user does not lose it on reload.
        observer = _RecordingObserver()
        events = [_content_delta("Half-finished commentary.")]

        self._run(events, observer=observer)

        self.assertEqual(len(observer.intermediate_ends), 1)
        self.assertEqual(
            observer.intermediate_ends[0][1],
            "Half-finished commentary.",
        )


class _StepBoundary:
    def __init__(self, *, is_final_answer: bool):
        self.is_final_answer = is_final_answer


if __name__ == "__main__":
    unittest.main()
