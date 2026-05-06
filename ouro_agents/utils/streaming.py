"""Final-answer streaming helpers for extracting and reassembling streamed tool-call output."""

import json
import re
from typing import Optional

from smolagents import ChatMessageStreamDelta


def extract_streamed_answer_text(arguments_blob: str) -> Optional[str]:
    try:
        parsed = json.loads(arguments_blob)
        if isinstance(parsed, dict) and "answer" in parsed:
            answer = parsed["answer"]
            if isinstance(answer, str):
                return answer
            return json.dumps(answer)
    except Exception:
        pass

    match = re.search(r'"answer"\s*:\s*', arguments_blob)
    if not match:
        return None

    idx = match.end()
    if idx >= len(arguments_blob):
        return ""

    if arguments_blob[idx] != '"':
        return arguments_blob[idx:].strip()

    idx += 1
    chars: list[str] = []
    escape = False

    while idx < len(arguments_blob):
        ch = arguments_blob[idx]
        idx += 1

        if escape:
            if ch == "n":
                chars.append("\n")
            elif ch == "r":
                chars.append("\r")
            elif ch == "t":
                chars.append("\t")
            elif ch == "b":
                chars.append("\b")
            elif ch == "f":
                chars.append("\f")
            elif ch == "u" and idx + 4 <= len(arguments_blob):
                hex_value = arguments_blob[idx : idx + 4]
                if len(hex_value) == 4 and re.fullmatch(r"[0-9a-fA-F]{4}", hex_value):
                    chars.append(chr(int(hex_value, 16)))
                    idx += 4
                else:
                    break
            else:
                chars.append(ch)
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            return "".join(chars)
        chars.append(ch)

    return "".join(chars)


def extract_streamed_answer_from_content(content_blob: str) -> Optional[str]:
    if not content_blob:
        return None

    if "<function=final_answer>" in content_blob:
        match = re.search(r"<parameter=answer>(.*?)(?:</parameter>|$)", content_blob, re.DOTALL)
        if match:
            return match.group(1)

    if "final_answer" not in content_blob:
        return None

    inline_match = re.search(r"final_answer\s*\((.*)\)\s*$", content_blob, re.DOTALL)
    if inline_match:
        extracted = extract_streamed_answer_text(inline_match.group(1))
        if extracted is not None:
            return extracted

    arguments_match = re.search(
        r'"arguments"\s*:\s*"(?P<args>(?:\\.|[^"])*)',
        content_blob,
        re.DOTALL,
    )
    if arguments_match:
        args_blob = arguments_match.group("args").replace('\\"', '"')
        extracted = extract_streamed_answer_text(args_blob)
        if extracted is not None:
            return extracted

    return extract_streamed_answer_text(content_blob)


class IntermediateContentStreamer:
    """Stream the assistant content channel as commentary chunks per step.

    The agent often emits brief narration alongside a tool call ("Looking at
    recent quests first.", "Found three candidates — checking each."). That
    text lives on ``delta.content`` and is separate from final_answer arg
    deltas, which are handled by :class:`FinalAnswerStreamer`. This class
    surfaces those commentary deltas so the server can emit them to the
    websocket and persist them as conversation messages, giving the user the
    same live + replayable experience they'd get from a chat-style coding
    agent.

    The streamer is per-step: call :meth:`reset` (or :meth:`flush`) on every
    step boundary so each step gets its own accumulator and message id.

    Suppression: if the model embeds a legacy ``<function=final_answer>``
    block inside content, :class:`FinalAnswerStreamer` already extracts the
    answer from there. To avoid double-emitting that text as commentary, we
    suppress further intermediate output once we see the marker.
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

        if (
            "<function=final_answer>" in new_buffer
            or extract_streamed_answer_from_content(new_buffer) is not None
        ):
            self._suppressed = True
            return None

        self._buffer = new_buffer
        chunk = new_buffer[len(self._streamed) :]
        if not chunk:
            return None
        self._streamed = new_buffer
        return chunk

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


class FinalAnswerStreamer:
    def __init__(self):
        self._tool_names: dict[int, str] = {}
        self._arguments_by_index: dict[int, str] = {}
        self._content_buffer = ""
        self._streamed_text = ""

    def _emit_new_text(self, current_text: Optional[str]) -> Optional[str]:
        if current_text is None:
            return None

        if current_text.startswith(self._streamed_text):
            chunk = current_text[len(self._streamed_text) :]
        else:
            chunk = current_text

        self._streamed_text = current_text
        return chunk or None

    def consume(self, delta: ChatMessageStreamDelta) -> Optional[str]:
        tool_calls = delta.tool_calls or []
        emitted: list[str] = []

        for tool_call in tool_calls:
            index = tool_call.index or 0
            function = tool_call.function
            if function is None:
                continue
            if function.name:
                self._tool_names[index] = function.name
            if function.arguments:
                self._arguments_by_index[index] = self._arguments_by_index.get(
                    index, ""
                ) + str(function.arguments)

            if self._tool_names.get(index) != "final_answer":
                continue

            chunk = self._emit_new_text(
                extract_streamed_answer_text(self._arguments_by_index.get(index, ""))
            )
            if chunk:
                emitted.append(chunk)

        if delta.content:
            self._content_buffer += str(delta.content)
            chunk = self._emit_new_text(
                extract_streamed_answer_from_content(self._content_buffer)
            )
            if chunk:
                emitted.append(chunk)

        if emitted:
            return "".join(emitted)
        return None
