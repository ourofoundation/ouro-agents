import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ouro.events import WebhookEvent, parse_webhook_event

from .artifacts import PrefetchSpec
from .config import RunMode
from .provenance import AssetProvenance

CHAT_EVENT_TYPES = {"new-message", "new-conversation"}

EVENT_TOOL_PRELOADS: Dict[str, List[str]] = {
    "comment": ["ouro:get_asset", "ouro:create_comment", "ouro:get_comments"],
    "mention": ["ouro:get_asset", "ouro:create_comment", "ouro:get_comments"],
    "new-message": ["ouro:send_message", "ouro:list_messages"],
    "new-conversation": ["ouro:send_message"],
}

_PLAN_FEEDBACK_PRELOADS: List[str] = [
    "ouro:get_comments",
    "ouro:create_comment",
    "ouro:update_post",
    "ouro:get_asset",
]


@dataclass(frozen=True)
class EventRunContext:
    event_type: str
    task: str
    mode: RunMode
    conversation_id: Optional[str]
    user_id: Optional[str]
    preload_tools: tuple = ()
    prefetch: PrefetchSpec = field(default_factory=PrefetchSpec)
    provenance: Optional[AssetProvenance] = None
    source_id: Optional[str] = None
    focus_asset_id: Optional[str] = None
    focus_asset_type: Optional[str] = None
    reply_parent_id: Optional[str] = None
    thread_parent_id: Optional[str] = None
    feedback_text: Optional[str] = None


def _build_event_task(
    event: WebhookEvent,
    provenance: Optional[AssetProvenance] = None,
) -> tuple[str, RunMode, tuple, PrefetchSpec]:
    """Build the task string, run mode, preload tools, and prefetch spec.

    Returns (task, mode, preload_tools, prefetch).
    """
    data = event.data
    event_type = event.event_type
    preload_names = list(EVENT_TOOL_PRELOADS.get(event_type, []))
    prefetch = PrefetchSpec()

    def _ready_hint(names: list[str]) -> str:
        if not names:
            return ""
        call_names = [n.split(":", 1)[-1] for n in names]
        return (
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
        hint = _ready_hint(preload_names)
        if hint:
            task += f"\n\n{hint}"
        return task, RunMode.CHAT_REPLY, tuple(preload_names), prefetch

    if event_type == "new-conversation":
        return "", RunMode.CHAT_REPLY, tuple(preload_names), prefetch

    if event_type in {"comment", "mention"}:
        source_asset_type = data.get("source_asset_type", "unknown")
        source_id = data.get("source_id", "unknown")
        focus_asset_id = data.get("focus_asset_id") or data.get("target_id") or source_id
        focus_asset_type = (
            data.get("focus_asset_type")
            or data.get("target_asset_type")
            or source_asset_type
        )
        reply_parent_id = source_id
        thread_parent_id = data.get("target_id") or focus_asset_id

        asset_ids: list[str] = []
        for asset_id in (focus_asset_id, source_id):
            if asset_id and asset_id != "unknown" and asset_id not in asset_ids:
                asset_ids.append(asset_id)
        comment_parent_ids = (
            [thread_parent_id] if thread_parent_id and thread_parent_id != "unknown" else []
        )
        if asset_ids or comment_parent_ids:
            prefetch = PrefetchSpec(
                asset_ids=asset_ids,
                comment_parent_ids=comment_parent_ids,
            )

        # --- Plan feedback: active / pending_review plan ---
        if provenance and provenance.is_plan_feedback:
            preload_names = _PLAN_FEEDBACK_PRELOADS
            pc = provenance.plan_cycle
            comment_text = (
                data.get("text") or data.get("content") or data.get("body") or ""
            )
            reply_instruction = (
                f"Reply in the same thread by calling create_comment with parent_id "
                f"`{reply_parent_id}`."
                if reply_parent_id and reply_parent_id != pc.post_id
                else "Reply on the plan post with create_comment."
            )
            task = (
                f"You received feedback on your current plan "
                f"(cycle {pc.cycle_id[:8]}, status: {pc.status}, "
                f"post id: {pc.post_id or focus_asset_id}).\n\n"
                f"## Feedback\n{comment_text}\n\n"
                f"## Your Current Plan\n{pc.plan_text}\n\n"
                f"Review the feedback, revise your plan if needed, and update "
                f"the post (update_post). {reply_instruction}\n\n"
                f"Return a JSON summary:\n"
                f'```json\n{{"revised_plan": "<updated plan text>", '
                f'"feedback_summary": "<brief summary of changes>"}}\n```\n\n'
                f"{_ready_hint(preload_names)}"
            )
            return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch

        # --- Historical plan feedback: completed plan ---
        if provenance and provenance.is_historical_plan_feedback:
            pc = provenance.plan_cycle
            comment_text = (
                data.get("text") or data.get("content") or data.get("body") or ""
            )
            task = (
                f"You received feedback on a completed plan "
                f"(cycle {pc.cycle_id[:8]}, post id: {pc.post_id or focus_asset_id}).\n\n"
                f"## Feedback\n{comment_text}\n\n"
                f"This plan has already been executed. Acknowledge the feedback "
                f"and note any insights that should inform future planning.\n\n"
                f"{_ready_hint(preload_names)}"
            )
            return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch

        # --- Event in the agent's planning space ---
        if provenance and provenance.in_planning_space:
            task = (
                f"Received a {event_type} in your planning space.\n\n"
                f"Source asset type: {source_asset_type}\n"
                f"Source asset id: {source_id}\n"
                f"Focus asset type: {focus_asset_type}\n"
                f"Focus asset id: {focus_asset_id}\n"
                f"Event data:\n{json.dumps(data, indent=2, sort_keys=True)}\n\n"
                f"Consider whether this is relevant to your current plan. "
                f"Reply on Ouro if appropriate.\n\n"
                f"{_ready_hint(preload_names)}"
            )
            return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch

        # --- Default comment/mention handling ---
        comment_text = (
            data.get("text") or data.get("content") or data.get("body") or ""
        )
        commenter = (
            data.get("sender_username")
            or data.get("sender")
            or data.get("username")
            or "someone"
        )
        task = (
            f"Received a {event_type} on a {focus_asset_type} (id: {focus_asset_id}).\n\n"
            f"**@{commenter}** wrote:\n> {comment_text}\n\n"
            f"The full asset content and comment thread are provided below as "
            f"pre-loaded context — no need to call get_asset or get_comments.\n\n"
        )
        hint = _ready_hint(preload_names)
        if hint:
            task += f"{hint}\n"
        if provenance and provenance.is_own_asset:
            task += (
                "This is your asset. Respond as the author — with context "
                "about what you created and why. "
            )
        task += (
            f"Reply on Ouro (create_comment on `{reply_parent_id}`). "
            "If no reply or other action is needed, return exactly NO_ACTION."
        )
        return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch

    task = (
        f"Received event from Ouro: {event_type}\n\n"
        f"Event data:\n{json.dumps(data, indent=2, sort_keys=True)}\n\n"
        "Use MCP tools to act or reply on Ouro when appropriate. "
        "If nothing is needed, return exactly NO_ACTION."
    )
    return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch


def build_event_run_context(
    body: Dict[str, Any],
    provenance: Optional[AssetProvenance] = None,
) -> EventRunContext:
    event = parse_webhook_event(body)
    task, mode, preload, prefetch = _build_event_task(event, provenance=provenance)
    return EventRunContext(
        event_type=event.event_type,
        task=task,
        mode=mode,
        conversation_id=event.conversation_id,
        user_id=event.actor_user_id or event.recipient_user_id,
        preload_tools=preload,
        prefetch=prefetch,
        provenance=provenance,
        source_id=event.source_id,
        focus_asset_id=(event.data or {}).get("focus_asset_id"),
        focus_asset_type=(event.data or {}).get("focus_asset_type"),
        reply_parent_id=event.source_id if event.event_type in {"comment", "mention"} else None,
        thread_parent_id=(
            (event.data or {}).get("target_id") if event.event_type in {"comment", "mention"} else None
        ),
        feedback_text=(
            (event.data or {}).get("text")
            or (event.data or {}).get("content")
            or (event.data or {}).get("body")
        ) if event.event_type in {"comment", "mention"} else None,
    )
