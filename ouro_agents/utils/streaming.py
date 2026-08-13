"""Stream assistant content deltas as per-step conversation messages."""

from typing import Optional

from smolagents import ChatMessageStreamDelta

# smolagents renders a prior tool-calling step back into the message list as
# ``"Calling tools:\n" + str([tc.dict() ...])`` (see smolagents/memory.py).
# Models — GLM in particular — imitate that format and emit a narrated
# tool-call block in their content channel alongside the real native calls.
# That block is internal plumbing, not commentary, so we must never surface it
# to the conversation. Detection is case-insensitive; some models bold it
# (``**Calling tools:**``).
_NARRATED_TOOL_CALL_MARKER = "calling tools:"


def _narrated_tool_call_index(text: str) -> Optional[int]:
    """Index of the narrated tool-call marker in ``text`` (case-insensitive)."""
    idx = text.lower().find(_NARRATED_TOOL_CALL_MARKER)
    return idx if idx != -1 else None


def _safe_intermediate_len(text: str) -> int:
    """Length of ``text`` safe to emit without splitting a forming marker.

    Holds back a trailing run that is a case-insensitive prefix of the narrated
    tool-call marker so we never stream half of ``Calling tools:`` and then
    discover (next delta) that it was the marker.
    """
    marker = _NARRATED_TOOL_CALL_MARKER
    for hold in range(min(len(text), len(marker) - 1), 0, -1):
        if text[-hold:].lower() == marker[:hold]:
            return len(text) - hold
    return len(text)


class IntermediateContentStreamer:
    """Stream the assistant content channel as chunks per step.

    Every step — including the last — emits plain ``delta.content``. The last
    step is the user-facing reply (persisted with ``turn_final``); earlier
    steps are narration alongside tool calls.

    The streamer is per-step: call :meth:`reset` (or :meth:`flush`) on every
    step boundary so each step gets its own accumulator and message id.
    """

    def __init__(self):
        self._buffer = ""
        self._streamed = ""
        self._suppressed = False

    def consume(self, delta: ChatMessageStreamDelta) -> Optional[str]:
        if not delta.content:
            return None
        if self._suppressed:
            return None

        new_buffer = self._buffer + str(delta.content)
        self._buffer = new_buffer

        marker_idx = _narrated_tool_call_index(new_buffer)
        if marker_idx is not None:
            # A narrated "Calling tools:" block started. Keep any genuine
            # commentary that preceded it (stripping trailing decoration like
            # newlines or ``**``) and suppress the rest of this step.
            self._suppressed = True
            safe_text = new_buffer[:marker_idx].rstrip(" \t\r\n*")
        else:
            safe_text = new_buffer[: _safe_intermediate_len(new_buffer)]

        if len(safe_text) <= len(self._streamed):
            # The marker truncated text we'd already optimistically streamed;
            # pin the buffer to the clean prefix so flush()/buffered_text omit
            # the narrated block.
            if marker_idx is not None:
                self._streamed = safe_text
            return None

        chunk = safe_text[len(self._streamed) :]
        self._streamed = safe_text
        return chunk or None

    @property
    def buffered_text(self) -> str:
        """The full content streamed for this step so far."""
        return self._streamed

    @property
    def has_streamed(self) -> bool:
        return bool(self._streamed)

    def reset(self) -> None:
        self._buffer = ""
        self._streamed = ""
        self._suppressed = False

    def flush(self) -> str:
        """Return the full streamed text and reset for the next step."""
        text = self._streamed
        self.reset()
        return text
