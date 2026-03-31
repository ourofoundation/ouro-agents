"""Shared ToolCallingAgent subclass used by both the parent agent and subagents."""

import ast
import json
import logging
import re
import uuid

from smolagents import ToolCallingAgent
from smolagents.models import (
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    parse_json_if_needed,
)

from .. import smolagents_patches as _smolagents_patches  # noqa: F401
from ..display import get_display

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
_CALLING_TOOLS_RE = re.compile(r"Calling tools:\s*", re.IGNORECASE)
_INLINE_TOOL_CALL_RE = re.compile(
    r"(?:^|[\n\r`:]|\btool\s+)\s*(?P<name>[a-z][a-z0-9_:-]*)\s*\(",
    re.IGNORECASE,
)


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


def _extract_tool_call_fields(item: dict) -> tuple[str | None, object]:
    function = item.get("function", item)
    if not isinstance(function, dict):
        return None, {}

    func_name = function.get("name")
    arguments = function.get("arguments", {})

    # Accept a few common near-miss shapes from weaker models.
    if not func_name:
        func_name = function.get("tool") or function.get("recipient_name")
    if arguments == {}:
        arguments = function.get("args", function.get("parameters", {}))

    return func_name, arguments


def _tool_calls_from_data(data) -> list[ChatMessageToolCall] | None:
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None

    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        func_name, arguments = _extract_tool_call_fields(item)
        if not func_name:
            continue

        result.append(_make_tool_call(func_name, arguments, tool_id=item.get("id")))

    return result or None


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


def _parse_xml_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
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
    return result or None


def _extract_bracketed_block(content: str, start_idx: int) -> str | None:
    return _extract_balanced_block(content, start_idx, "[", "]")


def _parse_narrated_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
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

    return _tool_calls_from_data(parsed)


def _parse_structured_tool_calls(content: str) -> list[ChatMessageToolCall] | None:
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
            tool_calls = _tool_calls_from_data(parsed)
            if tool_calls:
                return tool_calls

    return None


def _python_literal(node: ast.AST):
    return ast.literal_eval(node)


def _parse_inline_tool_call(content: str) -> list[ChatMessageToolCall] | None:
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

        return [_make_tool_call(func_name, arguments)]

    return None


def _message_preview(content: str, max_chars: int = 600) -> str:
    preview = content.strip()
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "..."
    return preview


def _treat_as_reasoning_only(exc: Exception, preview: str) -> bool:
    return bool(preview) and (
        "does not contain any JSON blob" in str(exc)
        or "Could not parse tool call" in str(exc)
    )


def _patch_model_for_xml_tool_calls(model, is_chat_mode=False):
    """Wrap model.parse_tool_calls to fall back to salvage parsers."""
    original = model.parse_tool_calls

    def patched(message):
        try:
            return original(message)
        except Exception as exc:
            content = message.content or ""
            
            # If the model explicitly output NO_ACTION as raw text, wrap it in a tool call
            # so the agent doesn't get stuck in a loop.
            if content.strip() == "NO_ACTION":
                logger.info("Recovered raw NO_ACTION text as final_answer tool call")
                message.role = MessageRole.ASSISTANT
                message.tool_calls = [_make_tool_call("final_answer", {"answer": "NO_ACTION"})]
                return message
                
            tool_calls = _parse_xml_tool_calls(content)
            if not tool_calls:
                tool_calls = _parse_narrated_tool_calls(content)
            if not tool_calls:
                tool_calls = _parse_structured_tool_calls(content)
            if not tool_calls:
                tool_calls = _parse_inline_tool_call(content)
            
            # If we still don't have tool calls, check if the model just output raw text
            # that looks like a final answer (no JSON/XML at all).
            # Only do this in chat mode to avoid swallowing legitimate reasoning in autonomous modes.
            if is_chat_mode and not tool_calls and content.strip() and "{" not in content and "<" not in content:
                logger.info("Recovered raw text as final_answer tool call")
                message.role = MessageRole.ASSISTANT
                message.tool_calls = [_make_tool_call("final_answer", {"answer": content.strip()})]
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
            logger.info(
                "Recovered tool call via fallback parser: %s",
                [tc.function.name for tc in tool_calls],
            )
            message.role = MessageRole.ASSISTANT
            message.tool_calls = tool_calls
            for tc in message.tool_calls:
                tc.function.arguments = parse_json_if_needed(tc.function.arguments)
            return message

    model.parse_tool_calls = patched


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

    def __init__(self, *args, compactor_model=None, is_chat_mode=False, **kwargs):
        self._compactor_model = compactor_model
        super().__init__(*args, **kwargs)
        _patch_model_for_xml_tool_calls(self.model, is_chat_mode=is_chat_mode)

    def execute_tool_call(self, tool_name, arguments):
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
            logger.warning(
                "Fell back to truncation for tool '%s'", tool_name
            )
            return truncated + suffix
        return result
