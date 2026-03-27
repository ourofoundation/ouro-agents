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


def _ready_hint(preload_names: list[str]) -> str:
    if not preload_names:
        return ""
    call_names = [n.split(":", 1)[-1] for n in preload_names]
    return (
        f"The following tools are already loaded and ready to call directly: "
        f"{', '.join(call_names)}. No need to call load_tool for these."
    )


# ---------------------------------------------------------------------------
# CommentContext — parsed once from event data, used by all comment handlers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommentContext:
    """All derived fields for a comment/mention event, parsed once."""

    source_id: str
    source_asset_type: str
    focus_asset_id: str
    focus_asset_type: str
    target_id: Optional[str]
    target_asset_type: Optional[str]
    is_thread_reply: bool
    reply_parent_id: str
    comment_text: str
    commenter: str

    @classmethod
    def from_event(cls, event: WebhookEvent) -> "CommentContext":
        data = event.data
        source_id = data.get("source_id", "unknown")
        source_asset_type = data.get("source_asset_type", "unknown")
        target_id = data.get("target_id")
        target_asset_type = data.get("target_asset_type")
        focus_asset_id = (
            data.get("focus_asset_id") or target_id or source_id
        )
        focus_asset_type = (
            data.get("focus_asset_type") or target_asset_type or source_asset_type
        )
        return cls(
            source_id=source_id,
            source_asset_type=source_asset_type,
            focus_asset_id=focus_asset_id,
            focus_asset_type=focus_asset_type,
            target_id=target_id,
            target_asset_type=target_asset_type,
            is_thread_reply=target_asset_type == "comment",
            reply_parent_id=source_id,
            comment_text=(
                data.get("text") or data.get("content") or data.get("body") or ""
            ),
            commenter=(
                data.get("sender_username")
                or data.get("sender")
                or data.get("username")
                or "someone"
            ),
        )

    def build_prefetch(self) -> PrefetchSpec:
        asset_ids = (
            [self.focus_asset_id]
            if self.focus_asset_id and self.focus_asset_id != "unknown"
            else []
        )
        comment_parent_ids = list(asset_ids)

        thread_comment_parent_ids: list[str] = []
        if self.is_thread_reply and self.target_id and self.target_id != "unknown":
            thread_comment_parent_ids.append(self.target_id)

        return PrefetchSpec(
            asset_ids=asset_ids,
            comment_parent_ids=comment_parent_ids,
            thread_comment_parent_ids=thread_comment_parent_ids,
        )


# ---------------------------------------------------------------------------
# Task builders — one per provenance branch, each returns a task string
# ---------------------------------------------------------------------------


def _plan_feedback_task(ctx: CommentContext, provenance: AssetProvenance) -> str:
    pc = provenance.plan_cycle
    reply_instruction = (
        f"Reply in the same thread by calling create_comment with parent_id "
        f"`{ctx.reply_parent_id}`."
        if ctx.reply_parent_id and ctx.reply_parent_id != pc.post_id
        else "Reply on the plan post with create_comment."
    )
    return (
        f"You received feedback on your current plan "
        f"(cycle {pc.cycle_id[:8]}, status: {pc.status}, "
        f"post id: {pc.post_id or ctx.focus_asset_id}).\n\n"
        f"## Feedback\n{ctx.comment_text}\n\n"
        f"## Your Current Plan\n{pc.plan_text}\n\n"
        f"Review the feedback, revise your plan if needed, and update "
        f"the post (update_post). {reply_instruction}\n\n"
        f"Return a JSON summary:\n"
        f'```json\n{{"revised_plan": "<updated plan text>", '
        f'"feedback_summary": "<brief summary of changes>"}}\n```\n\n'
        f"{_ready_hint(list(_PLAN_FEEDBACK_PRELOADS))}"
    )


def _historical_feedback_task(
    ctx: CommentContext,
    provenance: AssetProvenance,
    preload_names: list[str],
) -> str:
    pc = provenance.plan_cycle
    return (
        f"You received feedback on a completed plan "
        f"(cycle {pc.cycle_id[:8]}, post id: {pc.post_id or ctx.focus_asset_id}).\n\n"
        f"## Feedback\n{ctx.comment_text}\n\n"
        f"This plan has already been executed. Acknowledge the feedback "
        f"and note any insights that should inform future planning.\n\n"
        f"{_ready_hint(preload_names)}"
    )


def _planning_space_task(
    ctx: CommentContext,
    raw_data: dict,
    preload_names: list[str],
    event_type: str,
) -> str:
    return (
        f"Received a {event_type} in your planning space.\n\n"
        f"Source asset type: {ctx.source_asset_type}\n"
        f"Source asset id: {ctx.source_id}\n"
        f"Focus asset type: {ctx.focus_asset_type}\n"
        f"Focus asset id: {ctx.focus_asset_id}\n"
        f"Event data:\n{json.dumps(raw_data, indent=2, sort_keys=True)}\n\n"
        f"Consider whether this is relevant to your current plan. "
        f"Reply on Ouro if appropriate.\n\n"
        f"{_ready_hint(preload_names)}"
    )


def _default_comment_task(
    ctx: CommentContext,
    event_type: str,
    provenance: Optional[AssetProvenance],
    preload_names: list[str],
) -> str:
    if ctx.is_thread_reply:
        context_hint = (
            "The full post content, all top-level comments, and the "
            "current thread are provided below as pre-loaded context — "
            "no need to call get_asset or get_comments."
        )
    else:
        context_hint = (
            "The full post content and all comments are provided below "
            "as pre-loaded context — no need to call get_asset or get_comments."
        )
    task = (
        f"Received a {event_type} on a {ctx.focus_asset_type} (id: {ctx.focus_asset_id}).\n\n"
        f"**@{ctx.commenter}** wrote:\n> {ctx.comment_text}\n\n"
        f"{context_hint}\n\n"
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
        f"Reply on Ouro (create_comment on `{ctx.reply_parent_id}`). "
        "If no reply or other action is needed, return exactly NO_ACTION."
    )
    return task


# ---------------------------------------------------------------------------
# EventRunContext and builders
# ---------------------------------------------------------------------------


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
    comment_ctx: Optional[CommentContext] = None,
) -> tuple[str, RunMode, tuple, PrefetchSpec]:
    """Build the task string, run mode, preload tools, and prefetch spec."""
    data = event.data
    event_type = event.event_type
    preload_names = list(EVENT_TOOL_PRELOADS.get(event_type, []))
    prefetch = PrefetchSpec()

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
        ctx = comment_ctx or CommentContext.from_event(event)
        prefetch = ctx.build_prefetch()

        if provenance and provenance.is_plan_feedback:
            task = _plan_feedback_task(ctx, provenance)
            return task, RunMode.AUTONOMOUS, tuple(_PLAN_FEEDBACK_PRELOADS), prefetch

        if provenance and provenance.is_historical_plan_feedback:
            task = _historical_feedback_task(ctx, provenance, preload_names)
            return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch

        if provenance and provenance.in_planning_space:
            task = _planning_space_task(ctx, data, preload_names, event_type)
            return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch

        task = _default_comment_task(ctx, event_type, provenance, preload_names)
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
    is_comment = event.event_type in {"comment", "mention"}

    comment_ctx = CommentContext.from_event(event) if is_comment else None
    task, mode, preload, prefetch = _build_event_task(
        event, provenance=provenance, comment_ctx=comment_ctx,
    )

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
        reply_parent_id=event.source_id if is_comment else None,
        thread_parent_id=comment_ctx.target_id if comment_ctx else None,
        feedback_text=comment_ctx.comment_text if comment_ctx else None,
    )
