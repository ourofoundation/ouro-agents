"""Shared ToolCallingAgent subclass used by both the parent agent and subagents."""

import ast
import json
import logging
import re
import uuid
from dataclasses import dataclass

from smolagents import ActionStep, ToolCallingAgent
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
from ..provider_reasoning import copy_reasoning_fields

logger = logging.getLogger(__name__)

# Trigger compaction when a tool result exceeds this size (~12k tokens).
_MAX_TOOL_OUTPUT_CHARS = 50_000
# Compacted summaries are targeted at this size (~2k tokens).
_COMPACT_TARGET_CHARS = 8_000

_COMPACT_SYSTEM_PROMPT = """\
A tool returned output that is too large to include verbatim in context.
Compress it into a concise but faithful summary. Preserve all specific facts, numbers, \
names, URLs, code snippets, error messages, and structured data. Omit filler, repetition, \
and boilerplate. Do not add commentary — output only the compressed content."""

_EMPTY_MODEL_RESPONSE_ANSWER = (
    "MODEL_EMPTY_RESPONSE: model returned no content and no tool calls."
)
_RAW_DEBUG_MAX_CHARS = 4_000

# When the model emits plain content (no tool call) and salvage parsers fail,
# we used to coerce the text into ``final_answer`` so chat-mode runs would not
# spiral into empty-tool-call loops. That terminated the agent prematurely
# whenever the model's "let me check that next" preamble slipped out of the
# reasoning channel into content. The patterns below identify text that is
# almost certainly an intermediate thought rather than a user-facing reply,
# so we can route those cases to the corrective-nudge path instead.
#
# Anchored to the start of the trimmed content. Matched case-insensitively.
_PREAMBLE_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^let'?s\b", re.IGNORECASE),
    re.compile(r"^let\s+me\b", re.IGNORECASE),
    re.compile(r"^i'?ll\b", re.IGNORECASE),
    re.compile(r"^i\s+will\b", re.IGNORECASE),
    re.compile(r"^i'?m\s+going\s+to\b", re.IGNORECASE),
    re.compile(r"^i\s+need\s+to\b", re.IGNORECASE),
    re.compile(r"^i\s+should\b", re.IGNORECASE),
    re.compile(r"^now\s+(?:let|i'?ll|i\s+will|i\s+need)\b", re.IGNORECASE),
    re.compile(r"^next,?\s+(?:i'?ll|i\s+will|let)\b", re.IGNORECASE),
    re.compile(r"^first,?\s+(?:i'?ll|i\s+will|let|i\s+need)\b", re.IGNORECASE),
    re.compile(r"^then\s+(?:i'?ll|i\s+will)\b", re.IGNORECASE),
    re.compile(r"^okay,?\s+(?:so|let|i'?ll|now)\b", re.IGNORECASE),
    re.compile(r"^alright,?\s+(?:so|let|i'?ll|now)\b", re.IGNORECASE),
    re.compile(r"^so\s+(?:let|i'?ll|i\s+will|i\s+need|first)\b", re.IGNORECASE),
    re.compile(r"^wait,?\s+", re.IGNORECASE),
    re.compile(r"^hmm,?\s+", re.IGNORECASE),
    re.compile(r"^actually,?\s+", re.IGNORECASE),
    re.compile(r"^one\s+(?:moment|sec(?:ond)?)\b", re.IGNORECASE),
    re.compile(
        r"^(?:give|just)\s+me\s+(?:a\s+)?(?:moment|sec(?:ond)?|minute)\b", re.IGNORECASE
    ),
    re.compile(r"^thinking\b", re.IGNORECASE),
    re.compile(r"^working\s+on\b", re.IGNORECASE),
    re.compile(r"^checking\b", re.IGNORECASE),
    re.compile(r"^looking\b", re.IGNORECASE),
    re.compile(r"^pulling\s+(?:up|that|those)\b", re.IGNORECASE),
    re.compile(r"^surfacing\b", re.IGNORECASE),
    re.compile(r"^searching\b", re.IGNORECASE),
)

# Substring fragments that, if present anywhere, strongly indicate an
# intermediate thought rather than a complete reply.
_PREAMBLE_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\blet\s+me\s+(?:check|look|search|pull|surface|see|verify|confirm|grab|fetch|run|try)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi'?ll\s+(?:check|look|search|pull|surface|see|verify|confirm|grab|fetch|run|try|now)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi'?m\s+going\s+to\s+(?:check|look|search|pull|surface|verify|run|try)\b",
        re.IGNORECASE,
    ),
)

_PREAMBLE_NUDGE_OBSERVATION = (
    "[runtime] Your previous step ended on a preamble — content without a tool call "
    "and without final_answer:\n"
    "    {preview}\n"
    'That looked like an intermediate thought (e.g. "let me…", "I\'ll…"), so '
    "it has been treated as an unfinished step rather than a final reply.\n\n"
    "Now do exactly one of:\n"
    "  - call the actual tool you intended (e.g. memory_recall, search_assets, send_message); "
    "you can include a brief content line alongside it as narration, or\n"
    "  - call final_answer with your real user-facing reply.\n\n"
    "Don't end a turn on a 'let me…' / 'I'll…' line — pair it with the tool call, or "
    "finish the work and reply."
)
_PREAMBLE_NUDGE_PREVIEW_CHARS = 240
_EMPTY_NARRATED_TOOL_CALL_NUDGE_OBSERVATION = (
    "[runtime] Your previous step emitted a narrated tool-call block with an empty "
    "tool list:\n"
    "    {preview}\n"
    "That is not a callable action. Now do exactly one of:\n"
    "  - call one real available tool through the native tool-call mechanism, or\n"
    "  - call final_answer with the result or blocker.\n\n"
    "Do not write `Calling tools:` text or an empty list in assistant content."
)

_EMPTY_RESPONSE_MAX_RETRIES = 2
_EMPTY_RESPONSE_NUDGE_OBSERVATION = (
    "[runtime] Your previous model response was completely empty — no content and no "
    "tool calls were returned. This is likely a transient provider issue, not a "
    "problem with your reasoning.\n\n"
    "Resume where you left off. Do exactly one of:\n"
    "  - call the tool you intended to call next, or\n"
    "  - call final_answer with your result if you are done.\n\n"
    "Do not repeat work you have already completed."
)
_REASONING_ONLY_NUDGE_OBSERVATION = (
    "[runtime] Your previous model response was an interleaved-thinking step: "
    "it had reasoning, but no assistant content and no tool calls yet:\n"
    "    {preview}\n"
    "Continue from that reasoning and end this step at an action boundary.\n\n"
    "Now do exactly one of:\n"
    "  - call the actual tool you intended, or\n"
    "  - call final_answer with your result if you are done."
)


def _looks_like_preamble(content: str) -> bool:
    """Return True when content looks like a mid-thought continuation cue.

    These are sentences a reasoning model produces when it intended to call
    another tool but accidentally leaked the lead-in into the content channel.
    Coercing them into ``final_answer`` ends the run with the user seeing only
    "let me check that next…" — exactly the bug we are guarding against.
    """
    if not content:
        return False
    text = content.strip()
    if not text:
        return False

    # Single short fragment with no terminal punctuation reads as a status
    # update, not a reply. ``len(text) <= 60`` is roughly one short clause.
    has_terminal = any(ch in text for ch in (".", "!", "?"))
    if len(text) <= 60 and not has_terminal:
        return True

    # Trailing ellipsis or colon is a hard signal of "more to come".
    if text.endswith(("…", "...", ":")):
        return True

    if any(p.match(text) for p in _PREAMBLE_PREFIX_PATTERNS):
        return True

    # Single-sentence intent declarations like "Not pulling enough detail from
    # memory — let me surface the actual results." This is the original
    # hermes regression: an intent fragment after a brief observation.
    if any(p.search(text) for p in _PREAMBLE_INTENT_PATTERNS):
        # Only flag when the whole content is intent-flavored. A long reply
        # that happens to contain "let me check" mid-paragraph is fine.
        # Use a generous threshold so multi-paragraph replies are exempt.
        if len(text) <= 400:
            return True

    return False


def _compact_tool_output(
    tool_name: str,
    output: str,
    task: str,
    model,
    target_chars: int = _COMPACT_TARGET_CHARS,
) -> str | None:
    """Ask a cheap LLM to summarize a large tool result.

    Returns the compacted string, or None if compaction fails (caller should
    fall back to truncation).
    """
    user_content = (
        f"Agent task: {task}\n"
        f"Tool: {tool_name}\n"
        f"Target length: under {target_chars:,} characters\n\n"
        f"Raw output:\n{output}"
    )
    try:
        result = model(
            [
                {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]
        )
        text = result.content if hasattr(result, "content") else str(result)
        logger.info(
            "Compacted tool '%s' output: %d → %d chars",
            tool_name,
            len(output),
            len(text),
        )
        return text
    except Exception as e:
        logger.warning("Tool output compaction failed for '%s': %s", tool_name, e)
        return None


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
_INLINE_TOOL_CALL_RE = re.compile(
    r"(?:^|[\n\r`:]|\btool\s+)\s*(?P<name>[a-z][a-z0-9_:-]*)\s*\(",
    re.IGNORECASE,
)


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

    This is a separate failure mode from a generic preamble: the model has
    copied the fallback-parser-friendly narrated format but supplied no actual
    calls. A targeted nudge is clearer than telling it only that it ended on a
    preamble.
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


def _python_literal(node: ast.AST):
    return ast.literal_eval(node)


def _parse_inline_tool_call_recovery(content: str) -> _ToolCallRecovery | None:
    for match in _INLINE_TOOL_CALL_RE.finditer(content):
        func_name = match.group("name").strip()
        open_idx = content.find("(", match.start("name"))
        payload = _extract_balanced_block(content, open_idx, "(", ")")
        if not payload:
            continue

        try:
            parsed = ast.parse(f"f{payload}", mode="eval")
        except Exception:
            continue

        call = parsed.body
        if not isinstance(call, ast.Call):
            continue

        try:
            if len(call.args) > 1:
                continue
            if any(keyword.arg is None for keyword in call.keywords):
                continue

            arguments = {}
            if call.args:
                only_arg = _python_literal(call.args[0])
                if not isinstance(only_arg, dict):
                    continue
                arguments.update(only_arg)

            for keyword in call.keywords:
                arguments[keyword.arg] = _python_literal(keyword.value)
        except Exception:
            continue

        return _ToolCallRecovery([_make_tool_call(func_name, arguments)])

    return None


def _parse_inline_tool_call(content: str) -> list[ChatMessageToolCall] | None:
    recovery = _parse_inline_tool_call_recovery(content)
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


def _treat_as_reasoning_only(exc: Exception, preview: str) -> bool:
    return bool(preview) and (
        "does not contain any JSON blob" in str(exc)
        or "Could not parse tool call" in str(exc)
    )


def _is_empty_model_response(exc: Exception, content: str) -> bool:
    return (
        not content.strip()
        and "Message contains no content and no tool calls" in str(exc)
    )


def _recover_chat_final_answer(content: str, tool_calls) -> str | None:
    """Treat raw chat text as a final answer after all tool-call salvage fails.

    In chat/chat-reply modes, plain assistant text is usually the intended user-
    facing reply. If we fail to parse any tool calls after all recovery attempts,
    returning that text as ``final_answer`` is safer than handing smolagents an
    empty tool-call list, which causes another step and can spiral into loops.

    Exception: when the content reads as a mid-thought continuation cue
    ("let me check…", "I'll search next…"), DO NOT coerce it. That kind of
    content means the model intended to call another tool but leaked the
    lead-in into content; auto-finalizing on it terminates the run with the
    user only seeing the preamble. Those cases route to the corrective-nudge
    path in :func:`_patch_model_for_xml_tool_calls` instead.
    """
    if tool_calls:
        return None
    answer = content.strip()
    if not answer:
        return None
    if _looks_like_preamble(answer):
        return None
    return answer


def _build_preamble_nudge_observation(content: str) -> str:
    preview = content.strip()
    if len(preview) > _PREAMBLE_NUDGE_PREVIEW_CHARS:
        preview = preview[:_PREAMBLE_NUDGE_PREVIEW_CHARS].rstrip() + "…"
    return _PREAMBLE_NUDGE_OBSERVATION.format(preview=preview)


def _build_empty_narrated_tool_call_nudge_observation(content: str) -> str:
    preview = content.strip()
    if len(preview) > _PREAMBLE_NUDGE_PREVIEW_CHARS:
        preview = preview[:_PREAMBLE_NUDGE_PREVIEW_CHARS].rstrip() + "…"
    return _EMPTY_NARRATED_TOOL_CALL_NUDGE_OBSERVATION.format(preview=preview)


def _build_reasoning_only_nudge_observation(content: str) -> str:
    preview = content.strip()
    if len(preview) > _PREAMBLE_NUDGE_PREVIEW_CHARS:
        preview = preview[:_PREAMBLE_NUDGE_PREVIEW_CHARS].rstrip() + "…"
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
        _parse_inline_tool_call_recovery,
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


def _patch_model_for_xml_tool_calls(model, is_chat_mode=False):
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
                    message._ouro_preamble_nudge_observation = (
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
                    message._ouro_preamble_nudge_observation = (
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
                message._ouro_preamble_nudge_observation = (
                    _build_empty_narrated_tool_call_nudge_observation(content)
                )
                message.content = ""
                return message

            # If salvage failed and the content reads as an intermediate thought
            # ("let me check that next", "I'll search now…"), treat it as a
            # recoverable empty step and attach a corrective nudge that the
            # SanitizedToolCallingAgent will surface as a TOOL_RESPONSE on the
            # next inference. Runs in BOTH chat and autonomous modes — leaking
            # preamble into content is a model bug regardless of mode.
            if not tool_calls and _looks_like_preamble(content):
                preview = _message_preview(content)
                if preview:
                    get_display().thought(preview)
                logger.warning(
                    "Model emitted intermediate-thought content without a tool call; "
                    "skipping auto-final-answer and nudging on next step: %r",
                    _message_preview(content),
                )
                message.role = MessageRole.ASSISTANT
                message.tool_calls = []
                # Stash the nudge text on the message; SanitizedToolCallingAgent
                # picks this up after _step_stream completes and converts it
                # into a memory_step observation so the next inference sees it.
                message._ouro_preamble_nudge_observation = (
                    _build_preamble_nudge_observation(content)
                )
                return message

            # In chat modes, if all salvage parsers fail and the model emitted
            # plain text, treat it as the intended assistant reply instead of
            # continuing with an empty tool-call list. Preamble-style content
            # has already been routed above and will not reach this branch.
            chat_final_answer = None
            if is_chat_mode:
                chat_final_answer = _recover_chat_final_answer(content, tool_calls)
            if chat_final_answer is not None:
                logger.info("Recovered raw chat text as final_answer tool call")
                message.role = MessageRole.ASSISTANT
                message.tool_calls = [
                    _make_tool_call("final_answer", {"answer": chat_final_answer})
                ]
                return message

            if not tool_calls:
                preview = _message_preview(content)
                if preview:
                    get_display().thought(preview)
                if _treat_as_reasoning_only(exc, preview):
                    logger.info(
                        "Treating non-tool model output as reasoning-only text and continuing."
                    )
                    message.role = MessageRole.ASSISTANT
                    message.tool_calls = []
                    return message
                raise
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
    model._ouro_parse_tool_calls_is_chat_mode = is_chat_mode


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
        compactor_model=None,
        is_chat_mode=False,
        cancellation_token: RunCancellationToken | None = None,
        **kwargs,
    ):
        self._compactor_model = compactor_model
        self._cancellation_token = cancellation_token
        self._reasoning_only_streak = 0
        self._reasoning_only_warned = False
        configured_max_steps = kwargs.get("max_steps")
        super().__init__(*args, **kwargs)
        self._step_budget_max_steps = configured_max_steps or getattr(
            self, "max_steps", None
        )
        _patch_model_for_xml_tool_calls(self.model, is_chat_mode=is_chat_mode)

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

    def _step_stream(self, memory_step: ActionStep):
        self._raise_if_cancelled()
        for output in super()._step_stream(memory_step):
            self._raise_if_cancelled()
            yield output
        self._track_reasoning_only_step(memory_step)
        self._inject_preamble_nudge_observation(memory_step)
        self._inject_step_budget_observation(memory_step)

    def _inject_preamble_nudge_observation(self, memory_step: ActionStep) -> None:
        """Surface a nudge when the patched parser flagged preamble content.

        The patched ``parse_tool_calls`` cannot itself add to the agent's
        memory — it only sees the ChatMessage. So it stashes the nudge text
        on the message; we read it here and append it to ``observations`` so
        smolagents' ``ActionStep.to_messages`` emits a TOOL_RESPONSE on the
        next inference. The model then sees a clear corrective hint instead
        of just its own dangling preamble in context.
        """
        message = getattr(memory_step, "model_output_message", None)
        if message is None:
            return
        nudge = getattr(message, "_ouro_preamble_nudge_observation", None)
        if not nudge:
            return
        existing = getattr(memory_step, "observations", None) or ""
        if nudge in existing:
            return
        memory_step.observations = (
            f"{existing}\n\n{nudge}".strip() if existing else nudge
        )
        try:
            delattr(message, "_ouro_preamble_nudge_observation")
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
                "or multi-tool chain; call final_answer, or make one decisive "
                "create/update/comment call only if that single call completes "
                "the deliverable."
            )
        elif remaining <= 3:
            guidance = (
                "You are near the end. Stop broad exploration; make the next "
                "tool call produce or save the artifact, then finish."
            )
        else:
            guidance = (
                "Begin converging now: prioritize a concrete artifact, platform "
                "update, comment, or final_answer over further exploration."
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
        self._raise_if_cancelled()
        for output in super().process_tool_calls(chat_message, memory_step):
            self._raise_if_cancelled()
            yield output

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
        result = super().execute_tool_call(tool_name, arguments)
        self._raise_if_cancelled()
        if isinstance(result, str) and len(result) > _MAX_TOOL_OUTPUT_CHARS:
            logger.warning(
                "Tool '%s' returned %d chars (limit %d); compacting...",
                tool_name,
                len(result),
                _MAX_TOOL_OUTPUT_CHARS,
            )
            if self._compactor_model is not None:
                task = getattr(self, "task", "") or ""
                compacted = _compact_tool_output(
                    tool_name, result, task, self._compactor_model
                )
                if compacted:
                    return compacted
            # Compactor unavailable or failed — fall back to hard truncation.
            truncated = result[:_MAX_TOOL_OUTPUT_CHARS]
            suffix = (
                f"\n\n[Output truncated: {len(result):,} chars total,"
                f" showing first {_MAX_TOOL_OUTPUT_CHARS:,}]"
            )
            logger.warning("Fell back to truncation for tool '%s'", tool_name)
            return truncated + suffix
        return result
