"""Shared ToolCallingAgent subclass used by both the parent agent and subagents."""

import ast
import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from smolagents import ActionStep, ToolCallingAgent
from smolagents.memory import TaskStep
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    parse_json_if_needed,
)

from .. import smolagents_patches as _smolagents_patches  # noqa: F401
from ..cancellation import RunCancellationToken
from ..display import get_display
from ..provider_reasoning import (
    attach_stream_reasoning_fields,
    copy_reasoning_fields,
)
from ..security.action_gates import observed_action_category
from .observation_policy import (
    ObservationPolicy,
    RUN_COMPACT_MARKER,
    enforce_step_budget,
    fold_observation_excerpt,
    is_exempt_tool,
    maybe_spill_and_stub,
)

logger = logging.getLogger(__name__)

_EMPTY_MODEL_RESPONSE_ANSWER = (
    "MODEL_EMPTY_RESPONSE: model returned no content and no tool calls."
)
_RAW_DEBUG_MAX_CHARS = 4_000

_NUDGE_PREVIEW_CHARS = 240
_EMPTY_NARRATED_TOOL_CALL_NUDGE_OBSERVATION = (
    "[runtime] Your previous step emitted a narrated tool-call block with an empty "
    "tool list:\n"
    "    {preview}\n"
    "That is not a callable action. Now do exactly one of:\n"
    "  - call one real available tool through the native tool-call mechanism, or\n"
    "  - end the turn with a plain final message containing the result or blocker.\n\n"
    "Do not write `Calling tools:` text or an empty list in assistant content."
)

_EMPTY_RESPONSE_MAX_RETRIES = 2
_EMPTY_RESPONSE_NUDGE_OBSERVATION = (
    "[runtime] Your previous model response was completely empty — no content and no "
    "tool calls were returned. This is likely a transient provider issue, not a "
    "problem with your reasoning.\n\n"
    "Resume where you left off. Do exactly one of:\n"
    "  - call the tool you intended to call next, or\n"
    "  - end the turn with a plain final message if you are done.\n\n"
    "Do not repeat work you have already completed."
)
_REASONING_ONLY_NUDGE_OBSERVATION = (
    "[runtime] Your previous model response was an interleaved-thinking step: "
    "it had reasoning, but no assistant content and no tool calls yet:\n"
    "    {preview}\n"
    "Continue from that reasoning and end this step at an action boundary.\n\n"
    "Now do exactly one of:\n"
    "  - call the actual tool you intended, or\n"
    "  - end the turn with a plain final message if you are done."
)


class PlainTaskStep(TaskStep):
    """TaskStep that renders the user content verbatim.

    smolagents' TaskStep prefixes every user message with "New task:\\n",
    which frames casual chat messages as work orders and primes the model
    toward task-execution behavior. Conversational runs use this step for
    both history turns and the live turn so the user's words arrive as-is.
    """

    def to_messages(self, summary_mode: bool = False) -> list[ChatMessage]:
        content = [{"type": "text", "text": self.task}]
        if self.task_images:
            content.extend(
                [{"type": "image", "image": image} for image in self.task_images]
            )
        return [ChatMessage(role=MessageRole.USER, content=content)]


_NULL_STRINGS = {"null", "None", "none", "undefined"}

_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_FUNCTION_RE = re.compile(r"<function=([^>]+)>", re.DOTALL)
_PARAMETER_RE = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)
_KIMI_TOOL_CALLS_SECTION_RE = re.compile(
    r"<\|tool_calls_section_begin\|>(.*?)<\|tool_calls_section_end\|>",
    re.DOTALL,
)
_KIMI_TOOL_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*"
    r"(?P<tool_call_id>[\w.:-]+)\s*"
    r"<\|tool_call_argument_begin\|>\s*"
    r"(?P<function_arguments>.*?)\s*"
    r"<\|tool_call_end\|>",
    re.DOTALL,
)

# DeepSeek "DSML" tool-call format. When vLLM serves DeepSeek-V3.x/V4 with both
# --reasoning-parser and --tool-call-parser enabled, the reasoning parser
# captures the entire output and the DSML tags leak into the ``reasoning``
# field rather than being parsed into structured ``tool_calls`` (see
# https://github.com/vllm-project/vllm/issues/36654). We salvage them ourselves.
# The "｜" character is U+FF5C (fullwidth vertical bar). We accept a regular
# ``|`` as well so we still match if a provider normalizes the byte.
_DSML_BAR = r"[｜|]"
_DSML_FUNCTION_CALLS_RE = re.compile(
    rf"<{_DSML_BAR}DSML{_DSML_BAR}function_calls>(?P<body>.*?)</{_DSML_BAR}DSML{_DSML_BAR}function_calls>",
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    rf"<{_DSML_BAR}DSML{_DSML_BAR}invoke\s+name\s*=\s*\"(?P<name>[^\"]+)\"\s*>"
    rf"(?P<body>.*?)</{_DSML_BAR}DSML{_DSML_BAR}invoke>",
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    rf"<{_DSML_BAR}DSML{_DSML_BAR}parameter\s+name\s*=\s*\"(?P<name>[^\"]+)\""
    rf"(?:\s+string\s*=\s*\"(?P<is_string>[^\"]*)\")?\s*>"
    rf"(?P<value>.*?)</{_DSML_BAR}DSML{_DSML_BAR}parameter>",
    re.DOTALL,
)
# MiniMax M2/M2.1 XML tool-call format. The canonical shape is:
#     <minimax:tool_call>
#     <invoke name="tool-name">
#     <parameter name="param-key">param-value</parameter>
#     </invoke>
#     </minimax:tool_call>
# (see https://huggingface.co/MiniMaxAI/MiniMax-M2 docs/tool_calling_guide.md).
# OpenRouter routes that don't natively parse this format leak the tokens into
# ``content``/``reasoning`` instead of structured ``tool_calls``. We also handle
# a degenerate variant observed in the wild where the model dumps the tool's
# JSON-schema property names as bare XML tags
# (e.g. ``<to><item>x</item></to><subject>...</subject>``) interspersed with a
# repeated ``]<]minimax[>[`` separator-token artifact.
_MINIMAX_SEPARATOR_RE = re.compile(r"\]<\]minimax\[>\[")
_MINIMAX_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*\"(?P<name>[^\"]+)\"\s*>(?P<body>.*?)</invoke>",
    re.DOTALL,
)
_MINIMAX_PARAMETER_RE = re.compile(
    r"<parameter\s+name\s*=\s*\"(?P<name>[^\"]+)\"\s*>(?P<value>.*?)</parameter>",
    re.DOTALL,
)
_MINIMAX_ITEM_RE = re.compile(r"<item>(?P<value>.*?)</item>", re.DOTALL)
_MINIMAX_TAG_RE = re.compile(r"<(?P<close>/?)(?P<name>[A-Za-z_][\w:.-]*)[^>]*>")
_CALLING_TOOLS_RE = re.compile(r"Calling tools:\s*", re.IGNORECASE)


@dataclass
class _ToolCallRecovery:
    tool_calls: list[ChatMessageToolCall]
    thought_text: str = ""


def _make_tool_call(
    func_name: str,
    arguments,
    *,
    tool_id: str | None = None,
) -> ChatMessageToolCall:
    return ChatMessageToolCall(
        id=str(tool_id or uuid.uuid4()),
        type="function",
        function=ChatMessageToolCallFunction(
            name=func_name,
            arguments=arguments,
        ),
    )


def _format_recovered_thought(value) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value).strip()


def _coerce_tool_arguments(func_name: str, arguments) -> tuple[dict | None, str]:
    if isinstance(arguments, str):
        parsed = parse_json_if_needed(arguments)
        arguments = parsed if isinstance(parsed, (dict, list)) else arguments

    if isinstance(arguments, dict):
        return arguments, ""

    if isinstance(arguments, list):
        dict_items = [item for item in arguments if isinstance(item, dict)]
        noise = [item for item in arguments if not isinstance(item, dict)]

        if func_name == "memory_recall" and dict_items:
            if len(dict_items) == 1 and "queries" in dict_items[0]:
                return dict_items[0], _format_recovered_thought(noise)
            if all(isinstance(item.get("query"), str) for item in dict_items):
                return {"queries": dict_items}, _format_recovered_thought(noise)

        if len(dict_items) == 1:
            return dict_items[0], _format_recovered_thought(noise)

        return None, _format_recovered_thought(arguments)

    return None, _format_recovered_thought(arguments)


def _extract_tool_call_fields(item: dict) -> tuple[str | None, dict | None, str]:
    function = item.get("function", item)
    if not isinstance(function, dict):
        return None, None, ""

    func_name = function.get("name")
    arguments = function.get("arguments", {})

    # Accept a few common near-miss shapes from weaker models.
    if not func_name:
        func_name = function.get("tool") or function.get("recipient_name")
    if arguments == {}:
        arguments = function.get("args", function.get("parameters", {}))

    if not func_name:
        return None, None, ""

    coerced, thought_text = _coerce_tool_arguments(func_name, arguments)
    if coerced is None:
        return None, None, thought_text

    return func_name, coerced, thought_text


def _tool_call_recovery_from_data(data) -> _ToolCallRecovery | None:
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None

    result = []
    thought_parts: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            thought = _format_recovered_thought(item)
            if thought:
                thought_parts.append(thought)
            continue
        func_name, arguments, thought = _extract_tool_call_fields(item)
        if thought:
            thought_parts.append(thought)
        if not func_name:
            continue

        result.append(_make_tool_call(func_name, arguments, tool_id=item.get("id")))

    if not result:
        return None
    thought_text = "\n".join(part for part in thought_parts if part)
    return _ToolCallRecovery(tool_calls=result, thought_text=thought_text)


def _tool_calls_from_data(data) -> list[ChatMessageToolCall] | None:
    recovery = _tool_call_recovery_from_data(data)
    return recovery.tool_calls if recovery else None


def _extract_balanced_block(
    content: str,
    start_idx: int,
    opening: str,
    closing: str,
) -> str | None:
    if start_idx < 0 or start_idx >= len(content) or content[start_idx] != opening:
        return None

    depth = 0
    in_string = False
    string_quote = ""
    escape = False

    for idx in range(start_idx, len(content)):
        ch = content[idx]

        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == string_quote:
                in_string = False
            continue

        if ch in ("'", '"'):
            in_string = True
            string_quote = ch
            continue
        if ch == opening:
            depth += 1
            continue
        if ch == closing:
            depth -= 1
            if depth == 0:
                return content[start_idx : idx + 1]

    return None


def _function_name_from_kimi_id(tool_call_id: str) -> str | None:
    # Kimi documents IDs as functions.{func_name}:{idx}. Be a little tolerant
    # of provider variants while still requiring a real function name.
    if tool_call_id.startswith("functions."):
        remainder = tool_call_id[len("functions.") :]
    else:
        remainder = tool_call_id
    func_name = remainder.rsplit(":", 1)[0].strip()
    return func_name or None


def _parse_kimi_tool_call_recovery(content: str) -> _ToolCallRecovery | None:
    """Parse Kimi K2 raw special-token tool calls when provider parsing fails."""

    sections = _KIMI_TOOL_CALLS_SECTION_RE.findall(content)
    if not sections:
        return None

    result: list[ChatMessageToolCall] = []
    thought_parts: list[str] = []
    for section in sections:
        for match in _KIMI_TOOL_CALL_RE.finditer(section):
            tool_call_id = match.group("tool_call_id").strip()
            func_name = _function_name_from_kimi_id(tool_call_id)
            if not func_name:
                continue

            arguments, thought = _coerce_tool_arguments(
                func_name,
                match.group("function_arguments").strip(),
            )
            if thought:
                thought_parts.append(thought)
            if arguments is None:
                continue

            result.append(_make_tool_call(func_name, arguments, tool_id=tool_call_id))

    if not result:
        return None
    return _ToolCallRecovery(result, "\n".join(thought_parts))


def _parse_kimi_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
    recovery = _parse_kimi_tool_call_recovery(content)
    return recovery.tool_calls if recovery else None


def _coerce_dsml_parameter_value(raw: str, is_string: str | None) -> object:
    """Decode a DSML parameter body into the appropriate Python type.

    DSML parameters carry an optional ``string="true|false"`` attribute. When
    explicitly true, keep the value as a string. Otherwise try to parse as
    JSON first (so booleans, numbers, lists, and dicts come through
    correctly) and fall back to the raw string.
    """
    raw = raw.strip()
    if (is_string or "").strip().lower() == "true":
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _parse_dsml_tool_call_recovery(content: str) -> _ToolCallRecovery | None:
    """Parse DeepSeek DSML tool-call blocks leaked by vLLM (#36654)."""
    sections = list(_DSML_FUNCTION_CALLS_RE.finditer(content))
    if not sections:
        return None

    result: list[ChatMessageToolCall] = []
    for section in sections:
        body = section.group("body")
        for invoke in _DSML_INVOKE_RE.finditer(body):
            func_name = invoke.group("name").strip()
            arguments: dict = {}
            for param in _DSML_PARAMETER_RE.finditer(invoke.group("body")):
                arguments[param.group("name").strip()] = _coerce_dsml_parameter_value(
                    param.group("value"),
                    param.group("is_string"),
                )
            if not func_name:
                continue
            result.append(_make_tool_call(func_name, arguments))

    if not result:
        return None
    return _ToolCallRecovery(result)


def _parse_dsml_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
    recovery = _parse_dsml_tool_call_recovery(content)
    return recovery.tool_calls if recovery else None


def _coerce_minimax_value(raw: str) -> object:
    """Decode a MiniMax XML parameter/tag body into the appropriate type.

    Values come through as raw strings. Only JSON-decode when the value looks
    like a list or object so we keep prose fields (``subject``, ``text``,
    ``html``) verbatim instead of mangling something like ``"123 reasons"`` or
    an HTML document.
    """
    text = raw.strip()
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text
    return text


def _add_minimax_bare_param(params: dict, name: str, inner: str) -> None:
    """Record one bare-tag parameter, treating ``<item>`` children as a list.

    Empty tags (e.g. ``<cc></cc>``) are dropped: in the degenerate output the
    model emits the entire JSON schema as tags, leaving unused fields blank.
    """
    items = [v.strip() for v in _MINIMAX_ITEM_RE.findall(inner) if v.strip()]
    if items:
        params[name] = items
        return
    value = inner.strip()
    if value:
        params[name] = _coerce_minimax_value(value)


def _parse_minimax_bare_tag_params(body: str) -> dict:
    """Parse a degenerate ``<invoke>`` body that uses schema names as bare tags.

    Each direct child element of the invoke body becomes a parameter. A
    depth-counting tag scanner is used (rather than naive non-greedy regex) so
    values that themselves contain same-named tags — most importantly an
    ``<html>`` parameter whose value is a full ``<html>...</html>`` document —
    are captured intact. Declarations like ``<!DOCTYPE html>`` are ignored
    because they do not match a tag-name pattern.
    """
    params: dict = {}
    stack: list[tuple[str, int]] = []
    for match in _MINIMAX_TAG_RE.finditer(body):
        name = match.group("name")
        if not match.group("close"):
            stack.append((name, match.end()))
            continue
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth][0] != name:
                continue
            open_name, content_start = stack[depth]
            inner = body[content_start : match.start()]
            del stack[depth:]
            if not stack:
                _add_minimax_bare_param(params, open_name, inner)
            break
    return params


def _parse_minimax_tool_call_recovery(content: str) -> _ToolCallRecovery | None:
    """Parse MiniMax M2/M2.1 XML tool calls leaked into content/reasoning."""
    if "<invoke" not in content and "minimax" not in content:
        return None

    cleaned = _MINIMAX_SEPARATOR_RE.sub("", content)
    result: list[ChatMessageToolCall] = []
    for invoke in _MINIMAX_INVOKE_RE.finditer(cleaned):
        func_name = invoke.group("name").strip()
        if not func_name:
            continue
        body = invoke.group("body")
        arguments: dict = {}
        for param in _MINIMAX_PARAMETER_RE.finditer(body):
            key = param.group("name").strip()
            if key:
                arguments[key] = _coerce_minimax_value(param.group("value"))
        if not arguments:
            arguments = _parse_minimax_bare_tag_params(body)
        result.append(_make_tool_call(func_name, arguments))

    if not result:
        return None
    return _ToolCallRecovery(result)


def _parse_minimax_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
    recovery = _parse_minimax_tool_call_recovery(content)
    return recovery.tool_calls if recovery else None


def _parse_xml_tool_call_recovery(content: str) -> _ToolCallRecovery | None:
    """Parse XML-style tool calls emitted by models that don't use native function calling.

    Format:
        <tool_call>
        <function=tool_name>
        <parameter=key>value</parameter>
        </function>
        </tool_call>
    """
    blocks = _TOOL_CALL_RE.findall(content)
    if not blocks:
        return None

    result = []
    for block in blocks:
        func_match = _FUNCTION_RE.search(block)
        if not func_match:
            continue
        func_name = func_match.group(1).strip()

        arguments: dict = {}
        for param_match in _PARAMETER_RE.finditer(block):
            arguments[param_match.group(1).strip()] = param_match.group(2).strip()

        result.append(
            ChatMessageToolCall(
                id=str(uuid.uuid4()),
                type="function",
                function=ChatMessageToolCallFunction(
                    name=func_name,
                    arguments=arguments,
                ),
            )
        )
    return _ToolCallRecovery(result) if result else None


def _parse_xml_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
    recovery = _parse_xml_tool_call_recovery(content)
    return recovery.tool_calls if recovery else None


def _extract_bracketed_block(content: str, start_idx: int) -> str | None:
    return _extract_balanced_block(content, start_idx, "[", "]")


def _parse_narrated_tool_call_recovery(content: str) -> _ToolCallRecovery | None:
    match = _CALLING_TOOLS_RE.search(content)
    if not match:
        return None

    list_start = content.find("[", match.end())
    payload = _extract_bracketed_block(content, list_start)
    if not payload:
        return None

    try:
        parsed = ast.literal_eval(payload)
    except Exception:
        return None

    return _tool_call_recovery_from_data(parsed)


def _looks_like_empty_narrated_tool_call(content: str) -> bool:
    """Detect pseudo-tool output that explicitly contains ``Calling tools: []``.

    The model has copied the fallback-parser-friendly narrated format but
    supplied no actual calls. A targeted nudge is clearer than treating the
    broken syntax as an intra-turn message.
    """
    match = _CALLING_TOOLS_RE.search(content)
    if not match:
        return False

    list_start = content.find("[", match.end())
    payload = _extract_bracketed_block(content, list_start)
    if not payload:
        return False

    try:
        parsed = ast.literal_eval(payload)
    except Exception:
        return False
    return parsed == []


def _parse_narrated_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
    recovery = _parse_narrated_tool_call_recovery(content)
    return recovery.tool_calls if recovery else None


def _parse_structured_tool_call_recovery(content: str) -> _ToolCallRecovery | None:
    seen_blocks: set[str] = set()

    for idx, ch in enumerate(content):
        if ch not in "[{":
            continue
        block = _extract_balanced_block(content, idx, ch, "]" if ch == "[" else "}")
        if not block or block in seen_blocks:
            continue
        seen_blocks.add(block)

        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(block)
            except Exception:
                continue
            recovery = _tool_call_recovery_from_data(parsed)
            if recovery:
                return recovery

    return None


def _parse_structured_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
    recovery = _parse_structured_tool_call_recovery(content)
    return recovery.tool_calls if recovery else None


def _message_preview(content: str, max_chars: int = 600) -> str:
    preview = content.strip()
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "..."
    return preview


def _object_field(obj, name: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _serialize_debug_value(value, max_chars: int = _RAW_DEBUG_MAX_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            if hasattr(value, "model_dump_json"):
                text = value.model_dump_json()
            elif hasattr(value, "model_dump"):
                text = json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
            elif hasattr(value, "dict"):
                text = json.dumps(value.dict(), ensure_ascii=False, default=str)
            else:
                text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = repr(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def _raw_choice_message(chat_message):
    raw = getattr(chat_message, "raw", None)
    choices = _object_field(raw, "choices")
    if not choices:
        return None
    first_choice = choices[0]
    return _object_field(first_choice, "message")


def _extract_raw_reasoning_text(chat_message) -> str:
    raw_message = _raw_choice_message(chat_message)
    if raw_message is None:
        return ""

    parts: list[str] = []
    for field in ("reasoning", "reasoning_details", "reasoning_content", "thinking"):
        value = _object_field(raw_message, field)
        if value:
            parts.append(f"{field}: {_serialize_debug_value(value, 1_200)}")
    return "\n".join(parts)


def _debug_empty_model_response(chat_message, exc: Exception) -> None:
    raw = getattr(chat_message, "raw", None)
    raw_message = _raw_choice_message(chat_message)
    choices = _object_field(raw, "choices")
    first_choice = choices[0] if choices else None
    snapshot = {
        "error": str(exc),
        "parsed_content": getattr(chat_message, "content", None),
        "parsed_tool_calls": _serialize_debug_value(
            getattr(chat_message, "tool_calls", None),
            1_000,
        ),
        "finish_reason": _object_field(first_choice, "finish_reason"),
        "raw_message": _serialize_debug_value(raw_message),
        "raw_response": _serialize_debug_value(raw),
    }
    logger.warning(
        "Empty model response parse failure debug snapshot: %s",
        json.dumps(snapshot, ensure_ascii=False),
    )


def _extract_terminal_no_action(content: str) -> str | None:
    stripped = content.strip()
    if stripped == "NO_ACTION":
        return "NO_ACTION"

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return None

    last_line = lines[-1]
    if last_line in {"NO_ACTION", "`NO_ACTION`"}:
        return "NO_ACTION"
    return None


def _is_empty_model_response(exc: Exception, content: str) -> bool:
    return (
        not content.strip()
        and "Message contains no content and no tool calls" in str(exc)
    )


def _build_empty_narrated_tool_call_nudge_observation(content: str) -> str:
    preview = content.strip()
    if len(preview) > _NUDGE_PREVIEW_CHARS:
        preview = preview[:_NUDGE_PREVIEW_CHARS].rstrip() + "…"
    return _EMPTY_NARRATED_TOOL_CALL_NUDGE_OBSERVATION.format(preview=preview)


def _build_reasoning_only_nudge_observation(content: str) -> str:
    preview = content.strip()
    if len(preview) > _NUDGE_PREVIEW_CHARS:
        preview = preview[:_NUDGE_PREVIEW_CHARS].rstrip() + "…"
    return _REASONING_ONLY_NUDGE_OBSERVATION.format(preview=preview)


def _run_recovery_cascade(content: str) -> _ToolCallRecovery | None:
    """Try every salvage parser in priority order on a single text blob."""
    if not content:
        return None
    for parser in (
        _parse_kimi_tool_call_recovery,
        _parse_dsml_tool_call_recovery,
        _parse_minimax_tool_call_recovery,
        _parse_xml_tool_call_recovery,
        _parse_narrated_tool_call_recovery,
        _parse_structured_tool_call_recovery,
    ):
        recovery = parser(content)
        if recovery:
            return recovery
    return None


def _raw_reasoning_text_for_salvage(message) -> str:
    """Pull reasoning text suitable for a tool-call salvage attempt.

    ``_extract_raw_reasoning_text`` formats with ``"<field>: <value>"`` prefixes
    that would interfere with regex matching of native tool-call tokens.
    Concatenate the raw values directly instead.
    """
    raw_message = _raw_choice_message(message)
    if raw_message is None:
        return ""
    parts: list[str] = []
    seen: set[str] = set()

    def append_part(value: str) -> None:
        text = value.strip()
        if not text or text in seen:
            return
        seen.add(text)
        parts.append(text)

    for field in ("reasoning", "reasoning_content"):
        value = _object_field(raw_message, field)
        if isinstance(value, str) and value:
            append_part(value)
    details = _object_field(raw_message, "reasoning_details")
    if isinstance(details, list):
        for entry in details:
            if isinstance(entry, dict):
                text = entry.get("text") or entry.get("summary")
                if isinstance(text, str) and text:
                    append_part(text)
    return "\n".join(parts)


def _install_recovered_tool_calls(message, recovery: _ToolCallRecovery) -> None:
    message.role = MessageRole.ASSISTANT
    message.tool_calls = recovery.tool_calls
    # Do not replay malformed tool-call syntax (e.g. "Calling tools: [...]") in
    # the next prompt. Once recovered into structured tool_calls, the raw text is
    # only a source of imitation drift for later steps.
    message.content = recovery.thought_text or ""
    for tc in message.tool_calls:
        tc.function.arguments = parse_json_if_needed(tc.function.arguments)


def _patch_model_for_xml_tool_calls(model):
    """Wrap model.parse_tool_calls to fall back to salvage parsers."""
    original = getattr(model, "_ouro_base_parse_tool_calls", None)
    if original is None:
        original = model.parse_tool_calls
        model._ouro_base_parse_tool_calls = original

    model._ouro_empty_response_streak = 0

    def patched(message):
        try:
            result = original(message)
            model._ouro_empty_response_streak = 0
            return result
        except Exception as exc:
            content = message.content or ""

            if _is_empty_model_response(exc, content):
                # vLLM (#36654) sometimes leaks the entire DSML tool-call block
                # into ``reasoning`` when both reasoning and tool-call parsers
                # are enabled. Try recovering from there before giving up.
                reasoning_text = _raw_reasoning_text_for_salvage(message)
                reasoning_recovery = _run_recovery_cascade(reasoning_text)
                if reasoning_recovery and reasoning_recovery.tool_calls:
                    logger.info(
                        "Recovered tool call from reasoning channel: %s",
                        [tc.function.name for tc in reasoning_recovery.tool_calls],
                    )
                    model._ouro_empty_response_streak = 0
                    _install_recovered_tool_calls(message, reasoning_recovery)
                    return message

                if reasoning_text.strip():
                    # This is not truly empty. Newer reasoning models can emit
                    # an interleaved thinking step after tool results before
                    # committing to the next tool call or final answer. The
                    # smolagents loop needs each step to end at an action
                    # boundary, so surface the reasoning back as an observation
                    # and ask the model to continue from it.
                    get_display().thought(_message_preview(reasoning_text))
                    logger.info(
                        "Model returned interleaved-thinking output without "
                        "content or tool calls; continuing to next action "
                        "boundary: %r",
                        _message_preview(reasoning_text),
                    )
                    model._ouro_empty_response_streak = 0
                    message.role = MessageRole.ASSISTANT
                    message.tool_calls = []
                    message.content = ""
                    message._ouro_nudge_observation = (
                        _build_reasoning_only_nudge_observation(reasoning_text)
                    )
                    return message

                _debug_empty_model_response(message, exc)
                model._ouro_empty_response_streak += 1
                if model._ouro_empty_response_streak < _EMPTY_RESPONSE_MAX_RETRIES:
                    logger.warning(
                        "Model returned no content and no tool calls "
                        "(attempt %d/%d); nudging to retry.",
                        model._ouro_empty_response_streak,
                        _EMPTY_RESPONSE_MAX_RETRIES,
                    )
                    message.role = MessageRole.ASSISTANT
                    message.tool_calls = []
                    message.content = ""
                    message._ouro_nudge_observation = (
                        _EMPTY_RESPONSE_NUDGE_OBSERVATION
                    )
                    return message

                logger.warning(
                    "Model returned no content and no tool calls %d times; "
                    "terminating agent loop.",
                    model._ouro_empty_response_streak,
                )
                message.role = MessageRole.ASSISTANT
                message.tool_calls = [
                    _make_tool_call(
                        "final_answer",
                        {"answer": _EMPTY_MODEL_RESPONSE_ANSWER},
                    )
                ]
                return message

            # If the model explicitly ends with a standalone NO_ACTION marker,
            # treat it as a terminal answer so autonomous comment runs can exit
            # cleanly even when the model includes reasoning before the marker.
            no_action_answer = _extract_terminal_no_action(content)
            if no_action_answer is not None:
                logger.info("Recovered raw NO_ACTION text as final_answer tool call")
                message.role = MessageRole.ASSISTANT
                message.tool_calls = [
                    _make_tool_call("final_answer", {"answer": no_action_answer})
                ]
                return message

            recovery = _run_recovery_cascade(content)
            # If salvaging from ``content`` failed, try the reasoning channel
            # too — DeepSeek runs at high effort sometimes emit the tool call
            # tokens inside the thinking block rather than as content.
            if not recovery:
                reasoning_recovery = _run_recovery_cascade(
                    _raw_reasoning_text_for_salvage(message)
                )
                if reasoning_recovery:
                    recovery = reasoning_recovery

            tool_calls = recovery.tool_calls if recovery else None

            if not tool_calls and _looks_like_empty_narrated_tool_call(content):
                preview = _message_preview(content)
                if preview:
                    get_display().thought(preview)
                logger.warning(
                    "Model emitted narrated tool-call text with an empty list; "
                    "nudging on next step: %r",
                    _message_preview(content),
                )
                message.role = MessageRole.ASSISTANT
                message.tool_calls = []
                message._ouro_nudge_observation = (
                    _build_empty_narrated_tool_call_nudge_observation(content)
                )
                message.content = ""
                return message

            # If salvage failed and the model emitted plain text, that
            # assistant message is the terminal reply — the standard
            # tool-calling convention. The synthesized final_answer is an
            # adapter for smolagents' stop signal; it is not a model-visible
            # protocol requirement.
            if not tool_calls:
                preview = _message_preview(content)
                if not preview:
                    raise
                logger.info("Recovered plain assistant content as terminal reply")
                model._ouro_empty_response_streak = 0
                message.role = MessageRole.ASSISTANT
                message.tool_calls = [
                    _make_tool_call("final_answer", {"answer": content.strip()})
                ]
                return message
            if recovery and recovery.thought_text:
                get_display().thought(_message_preview(recovery.thought_text))
            logger.info(
                "Recovered tool call via fallback parser: %s",
                [tc.function.name for tc in tool_calls],
            )
            model._ouro_empty_response_streak = 0
            _install_recovered_tool_calls(message, recovery)
            return message

    model.parse_tool_calls = patched


def _copy_step_reasoning_to_messages(memory_step, messages: list[ChatMessage]) -> None:
    """Replay provider reasoning on every assistant-side message for this step.

    Both ``ActionStep`` and ``PlanningStep`` expose ``model_output_message`` and
    benefit from the replay. We copy onto every ASSISTANT/TOOL_CALL message so
    that even if smolagents' message collapse logic later changes (e.g. stops
    merging consecutive same-role messages), the reasoning still survives the
    round-trip. The dedupe in ``_get_clean_message_list_preserving_reasoning``
    handles any duplicates introduced here.
    """
    source = getattr(memory_step, "model_output_message", None)
    if source is None:
        return

    for message in messages:
        if message.role in (MessageRole.ASSISTANT, MessageRole.TOOL_CALL):
            copy_reasoning_fields(source, message)


class SanitizedToolCallingAgent(ToolCallingAgent):
    """ToolCallingAgent with automatic null cleanup and tool-call fallbacks.

    LLMs (especially smaller ones) frequently emit the literal string "null"
    for optional parameters instead of omitting them.  smolagents' validation
    then rejects the value with a type-mismatch error, burning steps.

    Models routed through OpenRouter may also emit XML-style tool calls
    (e.g. <tool_call><function=name>...) or narrated "Calling tools:" blocks
    instead of native function calling. The fallback parsers handle these
    transparently when possible.
    """

    # Threshold for the diagnostic warning about consecutive reasoning-only
    # steps. Five is enough to flag a stuck loop without firing on the
    # occasional one-off where a strong model genuinely needed to think.
    _REASONING_ONLY_WARN_THRESHOLD = 5
    # Inject step-budget guidance only near the end so normal runs are not
    # cluttered, but the model still gets explicit pressure to close.
    _STEP_BUDGET_WARNING_THRESHOLD = 5

    def __init__(
        self,
        *args,
        observation_policy: ObservationPolicy | None = None,
        workspace: Path | str | None = None,
        run_id: str = "",
        cancellation_token: RunCancellationToken | None = None,
        **kwargs,
    ):
        self._observation_policy = observation_policy or ObservationPolicy()
        self._workspace = Path(workspace).resolve() if workspace else None
        self._run_id = run_id or ""
        self._cancellation_token = cancellation_token
        self._action_gate_mode = kwargs.pop("action_gate_mode", "off")
        self._action_gate_observer: Optional[Callable[[str, str], None]] = kwargs.pop(
            "action_gate_observer", None
        )
        self._reasoning_only_streak = 0
        self._reasoning_only_warned = False
        self._observation_compact_done = False
        configured_max_steps = kwargs.get("max_steps")
        self._plain_task_messages = bool(kwargs.pop("plain_task_messages", False))
        super().__init__(*args, **kwargs)
        self._step_budget_max_steps = configured_max_steps or getattr(
            self, "max_steps", None
        )
        _patch_model_for_xml_tool_calls(self.model)

    @property
    def tools_and_managed_agents(self):
        """Tool schemas advertised to the model.

        ``final_answer`` is excluded: turns end with plain assistant content
        (the standard tool-calling convention). The tool itself stays in
        ``self.tools`` because the parser synthesizes a ``final_answer`` call
        from terminal content as smolagents' internal stop signal.
        """
        return [
            entry
            for entry in super().tools_and_managed_agents
            if getattr(entry, "name", None) != "final_answer"
        ]

    def write_memory_to_messages(self, summary_mode: bool = False) -> list[ChatMessage]:
        messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
        for memory_step in self.memory.steps:
            step_messages = memory_step.to_messages(summary_mode=summary_mode)
            _copy_step_reasoning_to_messages(memory_step, step_messages)
            messages.extend(step_messages)
        return messages

    def _raise_if_cancelled(self) -> None:
        if self._cancellation_token is not None:
            self._cancellation_token.raise_if_cancelled()

    def run(self, *args, **kwargs):
        if self._cancellation_token is None:
            return super().run(*args, **kwargs)
        with self._cancellation_token.registered_agent(self):
            try:
                self._raise_if_cancelled()
                return super().run(*args, **kwargs)
            except KeyboardInterrupt:
                self._cancellation_token.cancel("interrupted")
                raise

    def _run_stream(self, *args, **kwargs):
        # MultiStepAgent.run appends a TaskStep for the live turn before
        # delegating here. Swap it for the prefix-free variant in
        # conversational runs so the user's message is not rewritten as
        # "New task:\n...".
        if (
            self._plain_task_messages
            and self.memory.steps
            and type(self.memory.steps[-1]) is TaskStep
        ):
            last = self.memory.steps[-1]
            self.memory.steps[-1] = PlainTaskStep(
                task=last.task, task_images=last.task_images
            )
        yield from super()._run_stream(*args, **kwargs)

    def _step_stream(self, memory_step: ActionStep):
        self._raise_if_cancelled()
        for output in super()._step_stream(memory_step):
            self._raise_if_cancelled()
            yield output
        self._attach_stream_reasoning(memory_step)
        self._track_reasoning_only_step(memory_step)
        self._inject_nudge_observation(memory_step)
        self._inject_step_budget_observation(memory_step)
        self._maybe_one_shot_compact_observations()

    def _attach_stream_reasoning(self, memory_step: ActionStep) -> None:
        """Attach OpenRouter reasoning_details captured during generate_stream.

        Streaming agglomeration drops provider reasoning fields; without this,
        chat tool-loops never replay encrypted/summary blocks for the next step.
        """
        consume = getattr(self.model, "consume_stream_reasoning_fields", None)
        if not callable(consume):
            return
        fields = consume()
        if not fields:
            return
        message = getattr(memory_step, "model_output_message", None)
        if message is None:
            return
        attach_stream_reasoning_fields(message, fields)

    def _maybe_one_shot_compact_observations(self) -> None:
        """One-shot fold of old observations when cumulative size crosses a ceiling.

        History stays append-only until this fires so prompt cache remains stable.
        Crossing the ceiling rewrites older steps once (accept one cache break),
        then append-only resumes until another material overrun.
        """
        policy = self._observation_policy
        action_steps = [s for s in self.memory.steps if isinstance(s, ActionStep)]
        if len(action_steps) <= policy.keep_recent_steps:
            return

        total = sum(len(s.observations or "") for s in action_steps)
        if total <= policy.run_compact_ceiling:
            return
        if self._observation_compact_done and total <= policy.run_compact_ceiling * 1.5:
            # Already compacted; wait for a material overrun before rewriting again
            # (avoids thrashing the cached prefix every step).
            return

        folded = 0
        for step in action_steps[: -policy.keep_recent_steps]:
            observations = step.observations or ""
            if (
                len(observations) <= policy.excerpt_chars
                or RUN_COMPACT_MARKER in observations
            ):
                continue
            step.observations = fold_observation_excerpt(
                observations,
                excerpt_chars=policy.excerpt_chars,
                max_inline_chars=policy.max_inline_chars,
            )
            folded += 1

        self._observation_compact_done = True
        if folded:
            new_total = sum(len(s.observations or "") for s in action_steps)
            logger.info(
                "One-shot compacted %d old step observation(s); "
                "run observations %d → %d chars (cache prefix rewritten once)",
                folded,
                total,
                new_total,
            )

    def _inject_nudge_observation(self, memory_step: ActionStep) -> None:
        """Surface a runtime nudge stashed by the patched parser.

        The patched ``parse_tool_calls`` cannot itself add to the agent's
        memory — it only sees the ChatMessage. So it stashes the nudge text
        on the message; we read it here and append it to ``observations`` so
        smolagents' ``ActionStep.to_messages`` emits a TOOL_RESPONSE on the
        next inference.
        """
        message = getattr(memory_step, "model_output_message", None)
        if message is None:
            return
        nudge = getattr(message, "_ouro_nudge_observation", None)
        if not nudge:
            return
        existing = getattr(memory_step, "observations", None) or ""
        if nudge in existing:
            return
        memory_step.observations = (
            f"{existing}\n\n{nudge}".strip() if existing else nudge
        )
        try:
            delattr(message, "_ouro_nudge_observation")
        except AttributeError:
            pass

    def _inject_step_budget_observation(self, memory_step: ActionStep) -> None:
        """Tell the next inference how much of its step budget remains.

        Smolagents enforces ``max_steps`` outside the prompt. Some models, especially
        interleaved-thinking models, will keep planning useful work right up to the
        cap unless the budget is explicit in-band. We add a terse observation only
        in the final few steps so the model can choose a closing action instead of
        starting another search/tool chain.
        """
        if getattr(memory_step, "is_final_answer", False):
            return
        max_steps = self._step_budget_max_steps
        if not isinstance(max_steps, int) or max_steps <= 0:
            return
        step_number = getattr(memory_step, "step_number", 0)
        if not isinstance(step_number, int) or step_number <= 0:
            return
        remaining = max_steps - step_number
        if remaining < 0 or remaining > self._STEP_BUDGET_WARNING_THRESHOLD:
            return

        if remaining == 0:
            guidance = (
                "No steps remain after this one. Do not start new work; the run "
                "must end now."
            )
        elif remaining == 1:
            guidance = (
                "This is the last available next step. Do not start a new search "
                "or multi-tool chain; deliver your final reply, or make one decisive "
                "tool call only if that single call completes the required output."
            )
        elif remaining <= 3:
            guidance = (
                "You are near the end. Stop broad exploration; make the next "
                "tool call directly advance the required output, then finish."
            )
        else:
            guidance = (
                "Begin converging now: prioritize your final reply or the decisive "
                "tool call that completes the required output over further exploration."
            )

        observation = (
            "[runtime] Step budget: completed "
            f"{step_number}/{max_steps}; {remaining} step"
            f"{'' if remaining == 1 else 's'} "
            f"{'remains' if remaining == 1 else 'remain'}. {guidance}"
        )
        existing = getattr(memory_step, "observations", None) or ""
        if observation in existing:
            return
        memory_step.observations = (
            f"{existing}\n\n{observation}".strip() if existing else observation
        )

    def _track_reasoning_only_step(self, memory_step: ActionStep) -> None:
        """Warn once when the model loops on reasoning without ever calling tools.

        Concretely: this fires when the parser fell back to the reasoning-only
        path in :func:`_patch_model_for_xml_tool_calls` for many consecutive
        steps — usually a sign that ``reasoning.effort`` is too high for the
        model and the upstream tool-call parser (e.g. vLLM #36654 for DeepSeek)
        never gets a chance to emit structured calls.
        """
        had_tool_calls = bool(getattr(memory_step, "tool_calls", None))
        if had_tool_calls:
            self._reasoning_only_streak = 0
            return
        self._reasoning_only_streak += 1
        if (
            self._reasoning_only_streak >= self._REASONING_ONLY_WARN_THRESHOLD
            and not self._reasoning_only_warned
        ):
            logger.warning(
                "Model returned reasoning-only output for %d consecutive steps; "
                "consider lowering reasoning effort or pinning a different "
                "OpenRouter provider (see vllm-project/vllm#36654 for DeepSeek).",
                self._reasoning_only_streak,
            )
            self._reasoning_only_warned = True

    def process_tool_calls(self, chat_message, memory_step):
        """Run tools, then label parallel observations so each result is attributable.

        smolagents concatenates parallel tool observations into one blob with no
        per-call delimiters. When that blob is later split across native
        ``role:"tool"`` messages, models (especially GLM) lose track of which
        result belongs to which call — e.g. a successful ``create_quest`` buried
        under inspection output. Prefix each observation with a stable header
        before the parent concatenates them.

        Also stores a per-call result map on the ActionStep so chat persistence
        can attribute results even if a later step-budget spill rewrites the
        combined observation string.
        """
        self._raise_if_cancelled()
        parallel = bool(
            chat_message.tool_calls and len(chat_message.tool_calls) > 1
        )
        from ..utils.tool_observations import (
            format_tool_result_header,
            set_step_tool_results,
            strip_tool_result_header,
        )

        per_call: dict[str, str] = {}
        for output in super().process_tool_calls(chat_message, memory_step):
            self._raise_if_cancelled()
            if (
                getattr(output, "observation", None) is not None
                and getattr(output, "tool_call", None) is not None
            ):
                name = output.tool_call.name or "tool"
                call_id = output.id or output.tool_call.id or ""
                body = str(output.observation)
                if parallel and not body.startswith("=== Tool result:"):
                    output.observation = (
                        f"{format_tool_result_header(name, call_id)}\n{body}"
                    )
                if call_id:
                    per_call[str(call_id)] = strip_tool_result_header(
                        str(output.observation)
                    )
            yield output

        if per_call:
            set_step_tool_results(memory_step, per_call)

        # Bound the combined step observation before it is treated as committed
        # history (parallel tools can still sum past per-call spill limits).
        # Skip when every tool in the step is exempt — e.g. a large load_skill
        # batch is intentional context and must stay fully inline.
        observations = getattr(memory_step, "observations", None)
        if observations:
            from ..utils.tool_observations import tool_call_name

            step_tools = getattr(memory_step, "tool_calls", None) or []
            names = [tool_call_name(tc) for tc in step_tools]
            if names and all(
                is_exempt_tool(name, self._observation_policy) for name in names
            ):
                return
            capped = enforce_step_budget(
                observations,
                tool_name="step",
                workspace=self._workspace,
                run_id=self._run_id,
                policy=self._observation_policy,
            )
            if capped != observations:
                memory_step.observations = capped

    def execute_tool_call(self, tool_name, arguments):
        self._raise_if_cancelled()
        if isinstance(arguments, dict):
            available_tools = {**self.tools, **self.managed_agents}
            tool_obj = available_tools.get(tool_name)
            if tool_obj and hasattr(tool_obj, "inputs"):
                cleaned = {}
                for key, value in arguments.items():
                    if key not in tool_obj.inputs:
                        cleaned[key] = value
                        continue
                    schema = tool_obj.inputs[key]
                    is_nullable = schema.get("nullable", False)
                    expected_type = schema.get("type", "any")
                    if (
                        is_nullable
                        and isinstance(value, str)
                        and value in _NULL_STRINGS
                        and expected_type != "string"
                    ):
                        continue
                    if is_nullable and value is None:
                        continue
                    cleaned[key] = value
                arguments = cleaned
        if self._action_gate_mode == "observe":
            category = observed_action_category(str(tool_name))
            if category is not None:
                logger.info(
                    "Ask-controller observe hit: tool=%s category=%s",
                    tool_name,
                    category,
                )
                if self._action_gate_observer is not None:
                    try:
                        self._action_gate_observer(str(tool_name), category)
                    except Exception:
                        logger.warning(
                            "Action-gate observer failed for %s",
                            tool_name,
                            exc_info=True,
                        )
        result = super().execute_tool_call(tool_name, arguments)
        self._raise_if_cancelled()
        if result is None:
            return result
        text = result if isinstance(result, str) else str(result)
        return maybe_spill_and_stub(
            text,
            tool_name=str(tool_name),
            workspace=self._workspace,
            run_id=self._run_id,
            policy=self._observation_policy,
        )
