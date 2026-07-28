"""Automatic names for previously unnamed AI conversations."""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import Future
from typing import Any, Callable


logger = logging.getLogger(__name__)

TITLE_PROMPT = """Generate a short title for this AI conversation.

Return only the title, with no quotes, label, or explanation.
Use 3-7 words that summarize the user's primary intent.
Prefer specific nouns and verbs. Do not use generic titles such as
"New conversation", "Chat", "Help request", or "User question"."""

MAX_TITLE_LENGTH = 72
_MESSAGE_REPR_RE = re.compile(r"^(?:ChatMessage|ChatMessageStreamDelta)\s*\(", re.I)


def _read_field(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _clean_title(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        raw = value
    elif isinstance(value, dict) and "content" in value:
        raw = value["content"]
    elif hasattr(value, "content"):
        raw = value.content
    else:
        raw = value

    if raw is None:
        return None

    line = next((line.strip() for line in str(raw).splitlines() if line.strip()), "")
    line = line.strip(" \t#`\"'")
    line = re.sub(r"^(?:title|conversation title)\s*:\s*", "", line, flags=re.I)
    line = line.strip(" \t#`\"'")
    line = re.sub(r"\s+", " ", line).rstrip(".")
    if not line or _MESSAGE_REPR_RE.match(line):
        return None
    if len(line) > MAX_TITLE_LENGTH:
        line = line[: MAX_TITLE_LENGTH + 1].rsplit(" ", 1)[0].rstrip(" ,:;—-")
    return line or None


def generate_conversation_name(
    model: Callable[..., Any],
    user_message: str,
    assistant_response: str | None = None,
) -> str | None:
    content = f"User:\n{user_message[:1200]}"
    if assistant_response and assistant_response.strip():
        content += f"\n\nAssistant:\n{assistant_response[:800]}"

    result = model(
        [
            {"role": "system", "content": TITLE_PROMPT},
            {"role": "user", "content": content},
        ]
    )
    return _clean_title(result)


def name_conversation_if_needed(
    ouro_client: Any,
    conversation_id: str,
    user_message: str,
    model_factory: Callable[[], Callable[..., Any]],
    assistant_response: str | None = None,
) -> str | None:
    conversation = ouro_client.conversations.retrieve(conversation_id)
    if _clean_title(_read_field(conversation, "name")):
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


def start_name_conversation_if_needed(
    ouro_client: Any,
    conversation_id: str,
    user_message: str,
    model_factory: Callable[[], Callable[..., Any]],
) -> Future[str | None]:
    """Kick off naming in a daemon thread so it can overlap the agent run.

    Titles are derived from the user message alone so naming does not wait
    for the assistant reply. Callers should ``future.result()`` before
    persisting the final message / sending email.
    """
    future: Future[str | None] = Future()

    def _run() -> None:
        try:
            future.set_result(
                name_conversation_if_needed(
                    ouro_client,
                    conversation_id,
                    user_message,
                    model_factory,
                )
            )
        except Exception as exc:
            future.set_exception(exc)

    threading.Thread(
        target=_run,
        name=f"ouro-name-conversation-{conversation_id[:8]}",
        daemon=True,
    ).start()
    return future


def await_conversation_naming(
    future: Future[str | None] | None,
    *,
    conversation_id: str | None = None,
    timeout_s: float = 30.0,
) -> str | None:
    """Wait for a naming future; log and return None on failure/timeout."""
    if future is None:
        return None
    try:
        title = future.result(timeout=timeout_s)
        if title:
            logger.info(
                "Named conversation %s: %s",
                conversation_id or "?",
                title,
            )
        return title
    except Exception as e:
        logger.warning(
            "Failed to name conversation %s: %s",
            conversation_id or "?",
            e,
        )
        return None
