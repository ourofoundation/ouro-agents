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


class FinalAnswerStreamer:
    def __init__(self):
        self._tool_names: dict[int, str] = {}
        self._arguments_by_index: dict[int, str] = {}
        self._streamed_text = ""

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

            current_text = extract_streamed_answer_text(
                self._arguments_by_index.get(index, "")
            )
            if current_text is None:
                continue

            if current_text.startswith(self._streamed_text):
                chunk = current_text[len(self._streamed_text) :]
            else:
                chunk = current_text

            self._streamed_text = current_text
            if chunk:
                emitted.append(chunk)

        if emitted:
            return "".join(emitted)
        return None
