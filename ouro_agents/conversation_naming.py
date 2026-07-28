"""Automatic names for previously unnamed AI conversations."""

from __future__ import annotations

import re
from typing import Any, Callable


TITLE_PROMPT = """Generate a short title for this AI conversation.

Return only the title, with no quotes, label, or explanation.
Use 3-7 words that summarize the user's primary intent.
Prefer specific nouns and verbs. Do not use generic titles such as
"New conversation", "Chat", "Help request", or "User question"."""

MAX_TITLE_LENGTH = 72


def _read_field(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _clean_title(value: Any) -> str | None:
    content = _read_field(value, "content")
    raw = content if content is not None else value
    line = next((line.strip() for line in str(raw).splitlines() if line.strip()), "")
    line = line.strip(" \t#`\"'")
    line = re.sub(r"^(?:title|conversation title)\s*:\s*", "", line, flags=re.I)
    line = line.strip(" \t#`\"'")
    line = re.sub(r"\s+", " ", line).rstrip(".")
    if not line:
        return None
    if len(line) > MAX_TITLE_LENGTH:
        line = line[: MAX_TITLE_LENGTH + 1].rsplit(" ", 1)[0].rstrip(" ,:;—-")
    return line or None


def generate_conversation_name(
    model: Callable[..., Any],
    user_message: str,
    assistant_response: str,
) -> str | None:
    result = model(
        [
            {"role": "system", "content": TITLE_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User:\n{user_message[:1200]}\n\n"
                    f"Assistant:\n{assistant_response[:800]}"
                ),
            },
        ]
    )
    return _clean_title(result)


def name_conversation_if_needed(
    ouro_client: Any,
    conversation_id: str,
    user_message: str,
    assistant_response: str,
    model_factory: Callable[[], Callable[..., Any]],
) -> str | None:
    conversation = ouro_client.conversations.retrieve(conversation_id)
    if str(_read_field(conversation, "name") or "").strip():
        return None

    title = generate_conversation_name(
        model_factory(),
        user_message,
        assistant_response,
    )
    if not title:
        return None

    ouro_client.conversations.update(conversation_id, name=title)
    return title
