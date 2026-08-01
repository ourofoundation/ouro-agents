"""Helpers for preserving provider-specific reasoning fields.

OpenRouter reasoning models may return ``reasoning`` or ``reasoning_details``
alongside normal assistant content/tool calls. Those fields need to be replayed
on later assistant messages, especially in tool-use loops.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)


REASONING_MESSAGE_FIELDS = ("reasoning_details", "reasoning", "reasoning_content")

# OpenAI's Responses API returns **one** reasoning item per turn with a single
# ``encrypted_content`` string. OpenRouter's chat-completions adapter maps that
# to ``reasoning_details`` as roughly:
#   [{type: reasoning.summary, ...}, {type: reasoning.encrypted, data, ...}]
# i.e. a handful of entries — see OpenRouter + Vercel AI Gateway docs.
#
# Bug we see in production: non-streaming GPT-5 responses sometimes arrive with
# thousands of distinct ``reasoning.encrypted`` crumbs (unique indexes / rs_*
# ids sharing one prefix, ~1KB each). Replaying them hits OpenAI's Responses
# ``input`` array max (16384) → ``array_above_max_length``. Truncating that
# sequence is also wrong (docs: do not rearrange/modify reasoning blocks).
# When the payload is clearly malformed, drop encrypted crumbs and keep
# summaries/text so the turn can continue.
MAX_ENCRYPTED_REASONING_DETAILS = 8

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

# Model-id prefixes whose ``format="unknown"`` reasoning_details we DO replay.
# Zhipu GLM (``z-ai/``) returns structured ``reasoning.text`` details that
# OpenRouter merely tags ``unknown``; replaying them is what lets an
# interleaved-thinking model keep its own scratchpad across a tool-use loop
# (without it, GLM re-derives state every step and repeats actions). This is
# only safe when the model is pinned to a single provider so replay never
# crosses providers — see ``openrouter_provider`` in the agent config.
# Moonshot Kimi (``moonshotai/``) likewise requires the complete assistant
# message (including reasoning_details) to be replayed verbatim in tool loops;
# OpenRouter tags its details ``unknown`` since there is no canonical format.
REPLAYABLE_UNKNOWN_REASONING_MODEL_PREFIXES = ("z-ai/", "moonshotai/")

# First-party OpenRouter provider slug for each replay-allowlisted family.
# Replaying ``format="unknown"`` reasoning is only safe same-provider, so
# model builds hard-pin these families to their first-party endpoint
# (overriding any config-level ``allow_fallbacks``).
FIRST_PARTY_PROVIDER_BY_PREFIX = {
    "z-ai/": "z-ai",
    "moonshotai/": "moonshotai",
}


def first_party_provider_slug(model_id: str | None) -> str | None:
    """Provider slug a replay-allowlisted model must be pinned to, if any."""
    if not model_id:
        return None
    for prefix, slug in FIRST_PARTY_PROVIDER_BY_PREFIX.items():
        if model_id.startswith(prefix):
            return slug
    return None

# Set by the model wrapper for the duration of request preparation so the
# (globally patched) message-cleaning pass can tell which model is being built.
active_model_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ouro_active_model_id", default=None
)


def model_allows_unknown_reasoning_replay(model_id: str | None) -> bool:
    """True when ``format="unknown"`` reasoning_details are replayable for this model."""
    if not model_id:
        return False
    return any(
        model_id.startswith(prefix)
        for prefix in REPLAYABLE_UNKNOWN_REASONING_MODEL_PREFIXES
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


def _is_encrypted_reasoning_detail(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    rtype = entry.get("type")
    return isinstance(rtype, str) and "encrypted" in rtype.lower()


def _merge_detail_pair(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge two same-(type, index) fragments (streaming deltas)."""
    merged = dict(existing)
    for key, value in new.items():
        if value is None:
            continue
        if key in ("text", "summary", "data") and isinstance(value, str):
            prev = merged.get(key)
            if not isinstance(prev, str) or not prev:
                merged[key] = value
            elif value.startswith(prev):
                # Growing snapshot delta.
                merged[key] = value
            elif prev.startswith(value):
                # Stale shorter snapshot; keep existing.
                pass
            else:
                merged[key] = prev + value
            continue
        # Prefer later non-empty metadata (signature, id, format, ...).
        if value not in ("", [], {}):
            merged[key] = value
    return merged


def merge_reasoning_details(details: Any) -> Any:
    """Collapse streaming fragments that share ``type`` + ``index``.

    Matches the langchain-openrouter fix: consecutive same-(type, index) entries
    are one logical block split across SSE chunks. Entries without ``index`` are
    left untouched. Distinct indexes stay distinct (legitimate multi-block turns).
    """
    if not isinstance(details, list) or not details:
        return details

    merged: list[Any] = []
    # key -> position in merged
    keyed_positions: dict[tuple[Any, Any], int] = {}
    for entry in details:
        if not isinstance(entry, dict):
            merged.append(entry)
            continue
        index = entry.get("index")
        rtype = entry.get("type")
        if index is None or rtype is None:
            merged.append(entry)
            continue
        key = (rtype, index)
        pos = keyed_positions.get(key)
        if pos is None:
            keyed_positions[key] = len(merged)
            merged.append(dict(entry))
        else:
            prev = merged[pos]
            if isinstance(prev, dict):
                merged[pos] = _merge_detail_pair(prev, entry)
            else:
                merged[pos] = dict(entry)
    return merged


def sanitize_reasoning_details(
    details: Any,
    *,
    max_encrypted: int = MAX_ENCRYPTED_REASONING_DETAILS,
) -> Any:
    """Normalize provider ``reasoning_details`` for safe storage and replay.

    1. Merge streaming fragments by ``(type, index)``.
    2. If encrypted count is still pathological, drop **all** encrypted entries
       and keep summaries/text. Do not truncate mid-sequence — OpenRouter/OpenAI
       docs require an unmodified block sequence, and a half-cipher is useless.
    """
    if not isinstance(details, list) or not details:
        return details

    normalized = merge_reasoning_details(details)
    encrypted = [e for e in normalized if _is_encrypted_reasoning_detail(e)]
    if len(encrypted) <= max_encrypted:
        return normalized

    logger.warning(
        "Dropping malformed reasoning.encrypted details: got %d after merge "
        "(expected ≤ %d). OpenAI Responses uses one encrypted_content per "
        "reasoning item; OpenRouter chat completions sometimes expands that "
        "into thousands of crumbs that trip array_above_max_length on replay. "
        "Keeping summaries/text only.",
        len(encrypted),
        max_encrypted,
    )
    return [e for e in normalized if not _is_encrypted_reasoning_detail(e)]


def extract_reasoning_fields(source: Any) -> dict[str, Any]:
    """Return provider reasoning fields present on a message-like object."""
    fields: dict[str, Any] = {}
    for name in REASONING_MESSAGE_FIELDS:
        value = object_field(source, name)
        if value is not None:
            fields[name] = jsonable_provider_value(value)
    details = fields.get("reasoning_details")
    if details is not None:
        sanitized = sanitize_reasoning_details(details)
        if sanitized:
            fields["reasoning_details"] = sanitized
        else:
            fields.pop("reasoning_details", None)
    return fields


def _filter_replayable_details(details: Any, *, allow_unknown: bool = False) -> list[Any]:
    """Drop ``reasoning_details`` entries with formats we can't safely replay.

    Returns a (possibly empty) list. Non-list inputs are returned unchanged so
    callers don't have to second-guess shape. When ``allow_unknown`` is set
    (a provider-pinned model on our replay allowlist), ``format="unknown"``
    entries are kept instead of dropped.
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
            if allow_unknown and fmt == "unknown":
                keep.append(entry)
            continue
        keep.append(entry)
    return keep


def replayable_reasoning_fields(source: Any) -> dict[str, Any]:
    """Like :func:`extract_reasoning_fields` but strips unreplayable details.

    Specifically: ``reasoning_details`` entries with ``format`` outside
    :data:`KNOWN_REPLAYABLE_REASONING_FORMATS` are dropped. If the resulting
    list is empty, we drop the field entirely so we fall back to the plain
    ``reasoning`` string (which most providers accept verbatim).

    Exception: for models on :data:`REPLAYABLE_UNKNOWN_REASONING_MODEL_PREFIXES`
    (resolved via the :data:`active_model_id` contextvar), ``format="unknown"``
    details are kept so the model's own chain-of-thought survives the tool-use
    loop. Those models must be provider-pinned so replay stays same-provider.

    Details are also passed through :func:`sanitize_reasoning_details` so
    streaming fragments are merged and OpenRouter encrypted crumb-storms are
    dropped rather than truncated.
    """
    fields = extract_reasoning_fields(source)
    details = fields.get("reasoning_details")
    if details is not None:
        allow_unknown = model_allows_unknown_reasoning_replay(active_model_id.get())
        kept = _filter_replayable_details(details, allow_unknown=allow_unknown)
        kept = sanitize_reasoning_details(kept) if kept else kept
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


def accumulate_stream_reasoning_delta(
    detail_fragments: list[Any],
    reasoning_text: str,
    delta: Any,
) -> str:
    """Fold one stream ``delta`` into running reasoning state.

    Returns the updated plaintext ``reasoning`` string. ``detail_fragments`` is
    mutated in place with any ``reasoning_details`` from this chunk (merged later
    via :func:`sanitize_reasoning_details`).
    """
    details = object_field(delta, "reasoning_details")
    if isinstance(details, list) and details:
        detail_fragments.extend(jsonable_provider_value(details))
    elif isinstance(details, dict):
        detail_fragments.append(jsonable_provider_value(details))

    text = object_field(delta, "reasoning")
    if text is None:
        text = object_field(delta, "reasoning_content")
    if not isinstance(text, str) or not text:
        return reasoning_text
    if not reasoning_text:
        return text
    if text == reasoning_text:
        return reasoning_text
    if text.startswith(reasoning_text):
        return text
    if reasoning_text.startswith(text):
        return reasoning_text
    return reasoning_text + text


def finalize_stream_reasoning_fields(
    detail_fragments: list[Any],
    reasoning_text: str,
) -> dict[str, Any]:
    """Build attachable reasoning fields after a stream completes."""
    fields: dict[str, Any] = {}
    if detail_fragments:
        sanitized = sanitize_reasoning_details(detail_fragments)
        if sanitized:
            fields["reasoning_details"] = sanitized
    cleaned = (reasoning_text or "").strip()
    if cleaned:
        fields["reasoning"] = cleaned
    return fields


def attach_stream_reasoning_fields(chat_message: Any, fields: dict[str, Any]) -> None:
    """Attach finalized stream reasoning onto an agglomerated ChatMessage."""
    if not fields or chat_message is None:
        return
    attach_reasoning_fields(chat_message, fields)


def apply_reasoning_fields_to_message_dict(
    message_dict: dict[str, Any],
    fields: dict[str, Any],
) -> None:
    """Add reasoning fields to an outgoing API message dict."""
    for name, value in fields.items():
        if value is not None and name not in message_dict:
            message_dict[name] = value
