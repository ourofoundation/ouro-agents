import json
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

from ouro.events import WebhookEvent, parse_webhook_event
from .config import RunMode

ReplyTargetType = Literal["comment", "conversation"]


@dataclass(frozen=True)
class EventRunContext:
    event_type: str
    task: str
    mode: RunMode
    conversation_id: Optional[str]
    user_id: Optional[str]
    reply_target_type: Optional[ReplyTargetType]
    reply_target_id: Optional[str]


def _build_event_task(event: WebhookEvent) -> tuple[str, RunMode]:
    data = event.data
    event_type = event.event_type

    if event_type == "new-message":
        sender = (
            event.sender_username
            or event.actor_user_id
            or "Unknown"
        )
        content = data.get("text") or data.get("content") or ""
        task = f"New conversation message from {sender}:\n\n{content}"
        return task, RunMode.CHAT

    if event_type == "new-conversation":
        conversation_name = data.get("name") or "Untitled conversation"
        task = (
            "A new conversation involving this agent was created.\n\n"
            f"Conversation: {conversation_name}\n"
            f"Conversation ID: {data.get('id', 'unknown')}\n\n"
            "If you should reply in the conversation, return only the reply text to post. "
            "If no reply is needed, return exactly NO_ACTION."
        )
        return task, RunMode.AUTONOMOUS

    if event_type in {"comment", "mention"}:
        source_asset_type = data.get("source_asset_type", "unknown")
        source_id = data.get("source_id", "unknown")
        task = (
            f"Received a {event_type} event from Ouro.\n\n"
            f"Source asset type: {source_asset_type}\n"
            f"Source asset id: {source_id}\n"
            f"Event data:\n{json.dumps(data, indent=2, sort_keys=True)}\n\n"
            "Use available tools to inspect the relevant asset. "
            "If you should reply on Ouro, return only the reply text to post. "
            "If no reply is needed, return exactly NO_ACTION."
        )
        return task, RunMode.AUTONOMOUS

    task = (
        f"Received event from Ouro: {event_type}\n\n"
        f"Event data:\n{json.dumps(data, indent=2, sort_keys=True)}\n\n"
        "If you should reply on Ouro, return only the reply text to post. "
        "If no reply is needed, return exactly NO_ACTION."
    )
    return task, RunMode.AUTONOMOUS


def _build_reply_target(
    event: WebhookEvent,
) -> tuple[Optional[ReplyTargetType], Optional[str]]:
    if event.event_type in {"new-message", "new-conversation"} and event.conversation_id:
        return "conversation", event.conversation_id

    if event.event_type in {"comment", "mention"} and event.source_id:
        return "comment", event.source_id

    return None, None


def build_event_run_context(body: Dict[str, Any]) -> EventRunContext:
    event = parse_webhook_event(body)
    task, mode = _build_event_task(event)
    reply_target_type, reply_target_id = _build_reply_target(event)
    return EventRunContext(
        event_type=event.event_type,
        task=task,
        mode=mode,
        conversation_id=event.conversation_id,
        user_id=event.actor_user_id or event.recipient_user_id,
        reply_target_type=reply_target_type,
        reply_target_id=reply_target_id,
    )
