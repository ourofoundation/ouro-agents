"""Helpers for preserving provider-specific reasoning fields.

OpenRouter reasoning models may return ``reasoning`` or ``reasoning_details``
alongside normal assistant content/tool calls. Those fields need to be replayed
on later assistant messages, especially in tool-use loops.
"""

from __future__ import annotations

from typing import Any


REASONING_MESSAGE_FIELDS = ("reasoning_details", "reasoning", "reasoning_content")

# Formats whose ``reasoning_details`` blocks OpenRouter (or the upstream provider)
# can actually re-attach on the next turn. ``"unknown"`` is what we get when an
# upstream provider — typically a self-hosted vLLM running DeepSeek — emits a
# free-form thinking block that has no canonical replay shape. Sending it back
# only inflates request size and risks confusing other providers in the routing
# pool, so we drop those entries on round-trip.
KNOWN_REPLAYABLE_REASONING_FORMATS = frozenset(
    {
        "anthropic-claude-v1",
        "openai-responses-v1",
        "azure-openai-responses-v1",
        "xai-responses-v1",
        "google-gemini-v1",
    }
)


def object_field(obj: Any, name: str) -> Any:
    """Read a field from either a dict-like or object-like API response."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def jsonable_provider_value(value: Any) -> Any:
    """Convert SDK response objects into request-serializable values."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [jsonable_provider_value(item) for item in value]
    if isinstance(value, tuple):
        return [jsonable_provider_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: jsonable_provider_value(item)
            for key, item in value.items()
            if item is not None
        }
    if hasattr(value, "model_dump"):
        return jsonable_provider_value(value.model_dump(mode="json", exclude_none=True))
    if hasattr(value, "dict"):
        return jsonable_provider_value(value.dict())
    return value


def extract_reasoning_fields(source: Any) -> dict[str, Any]:
    """Return provider reasoning fields present on a message-like object."""
    fields: dict[str, Any] = {}
    for name in REASONING_MESSAGE_FIELDS:
        value = object_field(source, name)
        if value is not None:
            fields[name] = jsonable_provider_value(value)
    return fields


def _filter_replayable_details(details: Any) -> list[Any]:
    """Drop ``reasoning_details`` entries with formats we can't safely replay.

    Returns a (possibly empty) list. Non-list inputs are returned unchanged so
    callers don't have to second-guess shape.
    """
    if not isinstance(details, list):
        return details
    keep: list[Any] = []
    for entry in details:
        if not isinstance(entry, dict):
            keep.append(entry)
            continue
        fmt = entry.get("format")
        if fmt and fmt not in KNOWN_REPLAYABLE_REASONING_FORMATS:
            continue
        keep.append(entry)
    return keep


def replayable_reasoning_fields(source: Any) -> dict[str, Any]:
    """Like :func:`extract_reasoning_fields` but strips unreplayable details.

    Specifically: ``reasoning_details`` entries with ``format`` outside
    :data:`KNOWN_REPLAYABLE_REASONING_FORMATS` are dropped. If the resulting
    list is empty, we drop the field entirely so we fall back to the plain
    ``reasoning`` string (which most providers accept verbatim).
    """
    fields = extract_reasoning_fields(source)
    details = fields.get("reasoning_details")
    if details is not None:
        kept = _filter_replayable_details(details)
        if kept:
            fields["reasoning_details"] = kept
        else:
            fields.pop("reasoning_details", None)
    return fields


def raw_choice_message(chat_message: Any) -> Any:
    raw = object_field(chat_message, "raw")
    choices = object_field(raw, "choices")
    if not choices:
        return None
    return object_field(choices[0], "message")


def attach_reasoning_fields(target: Any, fields: dict[str, Any]) -> None:
    for name, value in fields.items():
        if value is not None:
            setattr(target, name, value)


def attach_reasoning_from_raw_response(chat_message: Any) -> None:
    """Copy reasoning fields from ``chat_message.raw`` onto the ChatMessage."""
    raw_message = raw_choice_message(chat_message)
    if raw_message is None:
        return
    attach_reasoning_fields(chat_message, extract_reasoning_fields(raw_message))


def copy_reasoning_fields(source: Any, target: Any) -> None:
    attach_reasoning_fields(target, extract_reasoning_fields(source))


def apply_reasoning_fields_to_message_dict(
    message_dict: dict[str, Any],
    fields: dict[str, Any],
) -> None:
    """Add reasoning fields to an outgoing API message dict."""
    for name, value in fields.items():
        if value is not None and name not in message_dict:
            message_dict[name] = value
