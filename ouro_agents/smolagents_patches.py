"""Smolagents tweaks for Ouro console output and provider metadata.

ToolCallingAgent's streaming Live view calls ChatMessage.render_as_markdown(), which
by default appends one JSON line per tool call. OuroLogger already prints a compact
``> tool_name(args)`` line when the tool runs, so the JSON duplicates noise.
"""

from typing import Any

import smolagents.models as smol_models
from smolagents.models import ChatMessage, MessageRole

from .provider_reasoning import replayable_reasoning_fields


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

    return output


def apply() -> None:
    ChatMessage.render_as_markdown = _render_as_markdown_ouro  # type: ignore[method-assign]
    smol_models.get_clean_message_list = _get_clean_message_list_preserving_reasoning


apply()
