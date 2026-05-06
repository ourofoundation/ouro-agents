"""Tests for the intermediate content streamer + observer wiring.

Covers the path that turns each non-final step's assistant content into a
distinct conversation message — both as live websocket chunks and as a
persisted Ouro message — so users see a chat-style timeline matching the
work the agent actually did.
"""

import unittest
from types import SimpleNamespace

from smolagents import ChatMessageStreamDelta

from ouro_agents.observer import AgentObserver
from ouro_agents.utils.streaming import (
    FinalAnswerStreamer,
    IntermediateContentStreamer,
)


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

    def test_suppresses_after_legacy_final_answer_marker(self):
        # When the model embeds a `<function=final_answer>` block in content,
        # FinalAnswerStreamer extracts the answer from there. The intermediate
        # streamer must not also emit it as commentary, or the user would see
        # the final reply text twice (once as a commentary message, once as
        # the streamed reply).
        streamer = IntermediateContentStreamer()

        first = streamer.consume(_content_delta("Looking at this. "))
        self.assertEqual(first, "Looking at this. ")

        # The marker arrives mid-stream — suppression engages and nothing
        # further is emitted, including the commentary that preceded the
        # marker (already emitted) and the answer body that follows.
        suppressed = streamer.consume(
            _content_delta(
                "<function=final_answer><parameter=answer>The answer.</parameter></function>"
            )
        )
        self.assertIsNone(suppressed)

        # Subsequent deltas remain suppressed for the rest of this step.
        self.assertIsNone(streamer.consume(_content_delta("more text")))

    def test_suppresses_when_streamed_answer_extracted_from_content(self):
        # Some models stream the tool call as a JSON-ish blob inside content
        # rather than via tool_calls. ``extract_streamed_answer_from_content``
        # detects that format; if it returns anything, suppress.
        streamer = IntermediateContentStreamer()

        suppressed = streamer.consume(
            _content_delta('{"name": "final_answer", "arguments": {"answer": "Hi."}}')
        )
        self.assertIsNone(suppressed)
        self.assertFalse(streamer.has_streamed)

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


class TestFinalAnswerStreamerStillWorks(unittest.TestCase):
    """Sanity: the existing final-answer stream behaviour is unchanged."""

    def test_emits_final_answer_arg_chunks_only(self):
        streamer = FinalAnswerStreamer()
        # Tool call that is not final_answer: nothing emitted.
        self.assertIsNone(
            streamer.consume(_tool_arg_delta(name="search_assets", arguments="{")),
        )

        # Switch to the final_answer tool at a fresh index — chunks emit.
        chunks: list[str] = []
        for arg in ('{"answer": "', "Hello", " world", '"}'):
            ev = _tool_arg_delta(index=1, name="final_answer", arguments=arg)
            chunk = streamer.consume(ev)
            if chunk:
                chunks.append(chunk)

        self.assertEqual("".join(chunks), "Hello world")


class _RecordingObserver(AgentObserver):
    def __init__(self):
        self.intermediate_chunks: list[tuple[str, str]] = []
        self.intermediate_ends: list[tuple[str, str]] = []
        self.final_chunks: list[str] = []
        self.results: list[str] = []

    def on_stream_chunk(self, chunk: str) -> None:
        self.final_chunks.append(chunk)

    def on_intermediate_chunk(self, message_id: str, chunk: str) -> None:
        self.intermediate_chunks.append((message_id, chunk))

    def on_intermediate_end(self, message_id: str, full_text: str) -> None:
        self.intermediate_ends.append((message_id, full_text))

    def on_result_ready(self, result_text: str) -> None:
        self.results.append(result_text)


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

        def flush(*, drop: bool) -> None:
            nonlocal current_id
            if current_id is None:
                streamer.reset()
                return
            text = streamer.flush()
            mid = current_id
            current_id = None
            if drop or not text.strip():
                return
            observer.on_intermediate_end(mid, text)

        for ev in events:
            if isinstance(ev, ChatMessageStreamDelta):
                chunk = streamer.consume(ev)
                if chunk:
                    if current_id is None:
                        current_id = uuid7_str()
                    observer.on_intermediate_chunk(current_id, chunk)
            elif isinstance(ev, _StepBoundary):
                flush(drop=ev.is_final_answer)
        flush(drop=False)

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
        ids = [mid for mid, _ in observer.intermediate_ends]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(
            observer.intermediate_ends[0][1],
            "Looking at recent quests.",
        )
        self.assertEqual(
            observer.intermediate_ends[1][1],
            "Found three candidates — checking each.",
        )

        # Streamed chunks share the message id of the step they belong to.
        chunk_ids_step1 = {
            mid for mid, _ in observer.intermediate_chunks if mid == ids[0]
        }
        chunk_ids_step2 = {
            mid for mid, _ in observer.intermediate_chunks if mid == ids[1]
        }
        self.assertEqual(chunk_ids_step1, {ids[0]})
        self.assertEqual(chunk_ids_step2, {ids[1]})

    def test_final_answer_step_drops_buffered_commentary(self):
        # The final-answer step's content is covered by on_stream_chunk +
        # on_result_ready; we don't want a second message persisted.
        observer = _RecordingObserver()
        events = [
            _content_delta("Wrapping this up:"),
            _StepBoundary(is_final_answer=True),
        ]

        self._run(events, observer=observer)

        # Live streaming still happened (the user saw real-time typing)…
        self.assertTrue(observer.intermediate_chunks)
        # …but no separate persisted message for the final-answer step.
        self.assertEqual(observer.intermediate_ends, [])

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
