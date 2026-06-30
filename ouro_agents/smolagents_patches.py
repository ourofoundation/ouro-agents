"""Smolagents tweaks for Ouro console output and provider metadata.

ToolCallingAgent's streaming Live view calls ChatMessage.render_as_markdown(), which
by default appends one JSON line per tool call. OuroLogger already prints a compact
``> tool_name(args)`` line when the tool runs, so the JSON duplicates noise.
"""

import ast
import json
import logging
import re
import uuid
from typing import Any, Optional

import smolagents.models as smol_models
from smolagents.models import ChatMessage, MessageRole

from .provider_reasoning import replayable_reasoning_fields

logger = logging.getLogger(__name__)


_ORIGINAL_GET_CLEAN_MESSAGE_LIST = getattr(
    smol_models,
    "_ouro_original_get_clean_message_list",
    smol_models.get_clean_message_list,
)
smol_models._ouro_original_get_clean_message_list = _ORIGINAL_GET_CLEAN_MESSAGE_LIST


def _render_as_markdown_ouro(self: ChatMessage) -> str:
    return str(self.content) or ""


def _role_value(role: Any) -> str:
    return getattr(role, "value", role)


def _message_role(message: ChatMessage | dict, role_conversions: dict) -> str:
    role = message.get("role") if isinstance(message, dict) else message.role
    try:
        role = MessageRole(role)
    except (TypeError, ValueError):
        pass
    role = role_conversions.get(role, role_conversions.get(_role_value(role), role))
    return _role_value(role)


def _detail_dedupe_key(entry: Any) -> tuple:
    """Stable identity for a ``reasoning_details`` entry, used for dedupe.

    Includes the most distinctive payload fields. Falls back to the entry's
    string repr for non-dict shapes so we never crash here.
    """
    if not isinstance(entry, dict):
        return ("__nondict__", repr(entry))
    return (
        entry.get("type"),
        entry.get("id"),
        entry.get("text"),
        entry.get("summary"),
        entry.get("data"),
        entry.get("signature"),
    )


def _normalize_reasoning_details(details: list) -> list:
    """Dedupe by content and re-sort by ``index`` for stable replay order.

    Order matters: OpenRouter expects details in the same order the upstream
    provider emitted them. Stable sort preserves insertion order for entries
    without ``index``.
    """
    seen: set = set()
    deduped: list = []
    for entry in details:
        key = _detail_dedupe_key(entry)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)

    def _sort_key(entry: Any) -> tuple[int, int]:
        if isinstance(entry, dict) and isinstance(entry.get("index"), int):
            return (0, entry["index"])
        return (1, 0)

    deduped.sort(key=_sort_key)
    return deduped


def _merge_reasoning_field(existing: Any, new_value: Any) -> Any:
    """Merge two values for the same reasoning field across collapsed messages.

    ``_get_clean_message_list_preserving_reasoning`` walks the original message
    list in lockstep with the cleaned output and aggregates reasoning fields
    onto each collapsed output message. Without dedupe, a TOOL_CALL + ASSISTANT
    pair (which both carry the same reasoning after copy-step) would duplicate
    every entry on round-trip. Single-source cases also normalize so the output
    is always sorted by ``index``.
    """
    if isinstance(existing, list) and isinstance(new_value, list):
        return _normalize_reasoning_details([*existing, *new_value])
    if isinstance(existing, list) and new_value is None:
        return _normalize_reasoning_details(existing)
    if existing is None and isinstance(new_value, list):
        return _normalize_reasoning_details(new_value)
    if existing is None:
        return new_value
    if new_value is None:
        return existing
    if isinstance(existing, str) and isinstance(new_value, str):
        # Keep first non-empty: chain-of-thought is identical across collapsed
        # messages from the same step, so concatenation just inflates context.
        return existing or new_value
    return existing


_OBSERVATION_PREFIX = "Observation:\n"
# smolagents renders a tool-calling step as ``"Calling tools:\n" + repr(list)``
# (see smolagents/memory.py). The list literal always begins with ``[``.
_CALLING_TOOLS_RE = re.compile(r"Calling tools:\s*(?=\[)")


def _content_to_text(content: Any) -> str:
    """Flatten a smolagents message ``content`` (str or list of parts) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            el.get("text", "")
            for el in content
            if isinstance(el, dict) and el.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(content)


def _extract_bracketed(text: str, open_idx: int) -> Optional[str]:
    """Return the balanced ``[...]`` literal starting at ``open_idx`` (or None).

    Tracks string state so brackets inside quoted argument values don't throw
    off the depth count.
    """
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in "\"'":
            in_string = True
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    return None


def _to_native_tool_calls(parsed: Any) -> Optional[list[dict[str, Any]]]:
    """Convert a parsed ``[{id,type,function:{name,arguments}}]`` list to the
    native OpenAI tool_calls shape, with ``arguments`` as a JSON string."""
    if not isinstance(parsed, list) or not parsed:
        return None
    calls: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        fn = item.get("function")
        if not isinstance(fn, dict):
            return None
        name = fn.get("name")
        if not name or not isinstance(name, str):
            return None
        args = fn.get("arguments", {})
        if not isinstance(args, str):
            try:
                args = json.dumps(args)
            except (TypeError, ValueError):
                args = "{}"
        call_id = item.get("id") or f"call_{uuid.uuid4().hex[:24]}"
        calls.append(
            {
                "id": str(call_id),
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    return calls


def _parse_tool_call_render(text: str) -> tuple[str, Optional[list[dict[str, Any]]]]:
    """Split assistant ``text`` into (kept_narration, native_tool_calls).

    The authoritative call list is smolagents' final ``Calling tools:`` block.
    A model may *also* narrate its own (sometimes malformed) ``Calling tools:``
    block earlier in content — so we keep only text before the first marker and
    parse the last marker that yields a valid tool-call list.
    """
    matches = list(_CALLING_TOOLS_RE.finditer(text))
    if not matches:
        return text, None
    kept = text[: matches[0].start()].rstrip()
    for match in reversed(matches):
        literal = _extract_bracketed(text, match.end())
        if literal is None:
            continue
        try:
            parsed = ast.literal_eval(literal)
        except (ValueError, SyntaxError):
            continue
        calls = _to_native_tool_calls(parsed)
        if calls:
            return kept, calls
    return kept, None


def _build_tool_messages(
    calls: list[dict[str, Any]], observation: Optional[str]
) -> list[dict[str, Any]]:
    """One ``tool`` message per tool_call_id (API requires exact pairing).

    smolagents concatenates all parallel results into a single observation blob
    with no per-id delimiters, so the combined text rides on the first call and
    later ids get a pointer placeholder (keeps the message array valid)."""
    messages: list[dict[str, Any]] = []
    for idx, call in enumerate(calls):
        if idx == 0:
            content = observation if observation else ""
        elif observation:
            content = "(result included with the first tool call above)"
        else:
            content = ""
        messages.append(
            {"role": "tool", "tool_call_id": call["id"], "content": content}
        )
    return messages


def _rewrite_tool_protocol_as_native(
    output: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace smolagents' text tool-call protocol with native function calling.

    smolagents replays prior tool use as plain text (an assistant message whose
    content ends with ``Calling tools:\n[{...}]`` plus a ``user`` message
    ``Observation:\n...``). Native-function-calling models imitate that text
    instead of emitting real tool_calls. Rewrite those into proper
    ``assistant.tool_calls`` + ``role:"tool"`` messages so the model sees the
    format it was trained on. Any unparseable step is left untouched (status
    quo), bounding the blast radius.
    """
    result: list[dict[str, Any]] = []
    i = 0
    n = len(output)
    while i < n:
        msg = output[i]
        if msg.get("role") != "assistant":
            result.append(msg)
            i += 1
            continue

        kept, calls = _parse_tool_call_render(_content_to_text(msg.get("content")))
        if not calls:
            result.append(msg)
            i += 1
            continue

        new_msg = dict(msg)
        new_msg["content"] = kept or None
        new_msg["tool_calls"] = calls
        result.append(new_msg)
        i += 1

        # The tool response is the immediately following user/tool message
        # (smolagents prefixes "Observation:\n", or "Call id: ...\nError:..").
        observation: Optional[str] = None
        if i < n and output[i].get("role") in ("user", "tool"):
            next_text = _content_to_text(output[i].get("content"))
            if next_text.startswith(_OBSERVATION_PREFIX):
                observation = next_text[len(_OBSERVATION_PREFIX) :]
            else:
                observation = next_text
            i += 1
        result.extend(_build_tool_messages(calls, observation))

    return result


def _get_clean_message_list_preserving_reasoning(
    message_list: list[ChatMessage | dict],
    role_conversions: dict = {},
    convert_images_to_image_urls: bool = False,
    flatten_messages_as_text: bool = False,
) -> list[dict[str, Any]]:
    output = _ORIGINAL_GET_CLEAN_MESSAGE_LIST(
        message_list,
        role_conversions=role_conversions,
        convert_images_to_image_urls=convert_images_to_image_urls,
        flatten_messages_as_text=flatten_messages_as_text,
    )

    output_index = -1
    previous_role = None
    for message in message_list:
        role = _message_role(message, role_conversions)
        if output_index >= 0 and role == previous_role:
            target_index = output_index
        else:
            output_index += 1
            target_index = output_index
            previous_role = role

        if target_index >= len(output):
            continue

        fields = replayable_reasoning_fields(message)
        if not fields:
            continue

        target = output[target_index]
        for name, value in fields.items():
            merged = _merge_reasoning_field(target.get(name), value)
            if merged is not None:
                target[name] = merged

    # Reconstruct native tool_calls AFTER reasoning is merged onto the collapsed
    # output, so the lockstep role-collapse mapping above stays valid.
    try:
        output = _rewrite_tool_protocol_as_native(output)
    except Exception:
        logger.warning(
            "Native tool-call reconstruction failed; using text protocol",
            exc_info=True,
        )

    return output


def apply() -> None:
    ChatMessage.render_as_markdown = _render_as_markdown_ouro  # type: ignore[method-assign]
    smol_models.get_clean_message_list = _get_clean_message_list_preserving_reasoning


apply()
