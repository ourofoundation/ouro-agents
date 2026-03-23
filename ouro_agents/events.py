import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ouro.events import WebhookEvent, parse_webhook_event

from .config import RunMode

CHAT_EVENT_TYPES = {"new-message", "new-conversation"}

EVENT_TOOL_PRELOADS: Dict[str, List[str]] = {
    "comment":          ["ouro:get_asset", "ouro:create_comment", "ouro:get_comments"],
    "mention":          ["ouro:get_asset", "ouro:create_comment", "ouro:get_comments"],
    "new-message":      ["ouro:send_message", "ouro:list_messages"],
    "new-conversation": ["ouro:send_message"],
}


@dataclass(frozen=True)
class EventRunContext:
    event_type: str
    task: str
    mode: RunMode
    conversation_id: Optional[str]
    user_id: Optional[str]
    preload_tools: tuple = ()


def _build_event_task(event: WebhookEvent) -> tuple[str, RunMode]:
    data = event.data
    event_type = event.event_type
    preload_names = EVENT_TOOL_PRELOADS.get(event_type, [])
    ready_hint = ""
    if preload_names:
        call_names = [n.split(":", 1)[-1] for n in preload_names]
        ready_hint = (
            f"The following tools are already loaded and ready to call directly: "
            f"{', '.join(call_names)}. No need to call load_tool for these."
        )

    if event_type == "new-message":
        sender = event.sender_username or event.actor_user_id or "Unknown"
        content = data.get("text") or data.get("content") or ""
        conv = event.conversation_id or "unknown"
        task = (
            f"New conversation message from {sender} (conversation_id: {conv}).\n\n"
            f"{content}"
        )
        if ready_hint:
            task += f"\n\n{ready_hint}"
        return task, RunMode.CHAT

    if event_type == "new-conversation":
        return None, RunMode.CHAT

    if event_type in {"comment", "mention"}:
        source_asset_type = data.get("source_asset_type", "unknown")
        source_id = data.get("source_id", "unknown")
        task = (
            f"Received a {event_type} event from Ouro.\n\n"
            f"Source asset type: {source_asset_type}\n"
            f"Source asset id: {source_id}\n"
            f"Event data:\n{json.dumps(data, indent=2, sort_keys=True)}\n\n"
        )
        if ready_hint:
            task += f"{ready_hint}\n"
        task += (
            "Inspect the relevant asset and reply on Ouro "
            "(e.g. create_comment on the appropriate parent). "
            "If no reply or other action is needed, return exactly NO_ACTION."
        )
        return task, RunMode.AUTONOMOUS

    task = (
        f"Received event from Ouro: {event_type}\n\n"
        f"Event data:\n{json.dumps(data, indent=2, sort_keys=True)}\n\n"
        "Use MCP tools to act or reply on Ouro when appropriate. "
        "If nothing is needed, return exactly NO_ACTION."
    )
    return task, RunMode.AUTONOMOUS


def build_event_run_context(body: Dict[str, Any]) -> EventRunContext:
    event = parse_webhook_event(body)
    task, mode = _build_event_task(event)
    preload = tuple(EVENT_TOOL_PRELOADS.get(event.event_type, []))
    return EventRunContext(
        event_type=event.event_type,
        task=task,
        mode=mode,
        conversation_id=event.conversation_id,
        user_id=event.actor_user_id or event.recipient_user_id,
        preload_tools=preload,
    )
