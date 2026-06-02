from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ouro.resources.conversations import Messages


@dataclass(frozen=True)
class ConversationSummary:
    id: str
    name: str
    summary: str = ""
    updated_at: str = ""


def _read_attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _conversation_name(conversation: Any) -> str:
    explicit = _read_attr(conversation, "name")
    if explicit:
        return str(explicit)
    summary = _read_attr(conversation, "summary")
    if summary:
        return str(summary)[:72]
    created_at = _read_attr(conversation, "created_at")
    if created_at:
        return f"Chat {created_at}"
    return f"Chat {_read_attr(conversation, 'id', '')}"


def summarize_conversation(conversation: Any) -> ConversationSummary:
    return ConversationSummary(
        id=str(_read_attr(conversation, "id", "")),
        name=_conversation_name(conversation),
        summary=str(_read_attr(conversation, "summary", "") or ""),
        updated_at=str(
            _read_attr(conversation, "updated_at", "")
            or _read_attr(conversation, "created_at", "")
            or ""
        ),
    )


def create_conversation(
    user_client,
    *,
    user_id: str,
    agent_id: str,
    org_id: str | None = None,
    team_id: str | None = None,
    name: str | None = None,
):
    title = name or f"Ouro Agents Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    return user_client.conversations.create(
        member_user_ids=[user_id, agent_id],
        name=title,
        org_id=org_id,
        team_id=team_id,
    )


def list_conversations(
    user_client,
    *,
    org_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ConversationSummary]:
    conversations = user_client.conversations.list(
        org_id=org_id,
        limit=limit,
        offset=offset,
    )
    return [summarize_conversation(conversation) for conversation in conversations]


def retrieve_conversation(user_client, conversation_id: str):
    return user_client.conversations.retrieve(conversation_id)


def list_messages(user_client, conversation_id: str, *, limit: int = 100) -> list[dict]:
    return list(Messages(user_client).list(conversation_id, limit=limit) or [])


def send_user_message(
    user_client,
    conversation_id: str,
    *,
    user_id: str,
    text: str,
) -> dict:
    return Messages(user_client).create(
        conversation_id,
        type="message",
        text=text,
        json=_plain_text_doc(text),
        user_id=user_id,
    )


def _plain_text_doc(text: str) -> dict:
    paragraphs = text.split("\n") if text else [""]
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}] if line else [],
            }
            for line in paragraphs
        ],
    }

