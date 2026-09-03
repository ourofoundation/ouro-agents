import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ouro.events import WebhookEvent, parse_webhook_event

from .artifacts import PrefetchSpec, format_untrusted_evidence
from .config import RunMode
from .constants import FETCHABLE_ASSET_TYPES
from .event_registry import (
    event_surface_for,
    surface_capability_override_for,
)
from .tool_preloads import (
    attached_asset_ids,
    attached_asset_task_hint,
    preloads_for_event,
)
from .provenance import AssetProvenance
from .security.policy import Capability, EventSurface

# Shared guidance injected into comment tasks so agents default to silence
# unless a reply genuinely adds value.
_COMMENT_ENGAGEMENT_GUIDANCE = """\
## Decision: Respond or Do Nothing

First, decide whether a reply adds value. Doing nothing is a valid \
and often correct outcome. Call `no_action` unless your reply would \
meaningfully advance the conversation.

**Do nothing (`no_action`) when:**
- The comment is an acknowledgment, agreement, or thanks with no question
- You have nothing substantive to add beyond what's already been said
- The thread is a back-and-forth that has reached a natural conclusion
- Replying would just be restating your earlier point
- The comment is from another agent and doesn't ask you anything directly

**Respond when:**
- Someone asks you a direct question
- You have new information, evidence, or a correction to offer
- The comment misunderstands something you said and clarification matters
- You're the author of the asset and the commenter needs a response"""

# Mentions are a direct summons, not ambient conversation: the full
# silence-by-default rubric would contradict the prime directive there.
_MENTION_ENGAGEMENT_GUIDANCE = """\
## Decision

You were mentioned by name — treat this as a request addressed directly to you. \
Complete what was asked, then reply in-thread with the results. Call \
`no_action` only if the mention is purely social (thanks, an FYI, a passing \
reference) with nothing asked of you."""

# Injected into chat tasks when the sender is another agent. Two agents in one
# conversation will otherwise answer each other forever: every reply is a new
# message event for the peer, and the only thing that ends the exchange is one
# side declining to post.
_AGENT_MESSAGE_ENGAGEMENT_GUIDANCE = """\
## Decision: Respond or Do Nothing

This message was sent by another agent, not a person. Their reply to you will \
trigger you again, so the exchange only ends when one of you stops posting. \
Default to calling `no_action` as the only tool (nothing is posted) unless the \
message asks you for something concrete that you \
can deliver now.

**Do nothing (`no_action`) when:**
- It is an acknowledgment, agreement, thanks, sign-off, or a placeholder (a \
period, an emoji, "noted")
- It confirms, restates, or hands back something you already said
- It is addressed to a person in the conversation rather than to you
- You would only be confirming receipt, agreeing to be quiet, or explaining \
that you have nothing to add — do not post that; call `no_action`

**Respond when:**
- It asks you a direct question or requests work you can complete in this turn
- It hands you an artifact or result that the humans in the conversation are \
waiting on you to act on
- It contains an error about your work that matters to the people here

When you do respond, deliver the substance once and stop. Do not ask the other \
agent to confirm, do not propose protocols for how you will talk to each other, \
and do not thank them."""

_NO_ENGAGEMENT_BAIT = (
    "End your reply when the substance is done — no closing offers of further "
    'help ("let me know if...", "happy to dive deeper...").'
)

_THREAD_REPLY_CAUTION = (
    "**Thread reply caution:** This is a reply within an existing thread. "
    "Threads that go back and forth too many times become noise. "
    "Check the thread context below — if you've already made your point "
    "or the other person is wrapping up, let the thread end. "
    "Prefer silence over a redundant reply."
)

# Comments on quests often change item state (unblock, skip, reassign). Agents
# that only reply leave items disagreeing with the feedback — close the loop.
_QUEST_COMMENT_GUIDANCE = """\
## Quest items

This comment is on a quest. If it changes what should happen on an item, \
update the item in the same turn as any reply — do not stop at a verbal \
acknowledgment while items still disagree with the feedback. Use \
`list_quest_items` if you need item ids.

- **Unblocks** parked work (`waiting_on`, e.g. approval to send): do the \
work **and** clear waiting fields with `update_quest_item` (pass empty \
strings for `waiting_on` / `waiting_until` / `waiting_check_every`). Call \
`complete_quest_item` when the item's Done criteria are met.
- **Cancels, skips, or hands off** work ("skip this", "I'll handle it", \
"stand down"): set that item's status to `skipped` with \
`update_quest_item`, clear waiting fields, and note why in `notes`. Do not \
leave cancelled work as `in_progress`.
- **Revises** the plan (change description, add/remove items): use \
`update_quest` / `update_quest_item` / `create_quest_items` / \
`delete_quest_item` as needed."""

def _ready_hint(preload_names: list[str]) -> str:
    if not preload_names:
        return ""
    call_names = [n.split(":", 1)[-1] for n in preload_names]
    return (
        f"The following tools are already loaded and ready to call directly: "
        f"{', '.join(call_names)}. No need to call load_tool for these."
    )


def _untrusted_comment_evidence(ctx: "CommentContext", title: str) -> str:
    provenance = {
        "author": ctx.commenter,
        "root_asset_id": ctx.root_asset_id,
        "root_asset_type": ctx.root_asset_type,
    }
    if ctx.source_id != "unknown":
        provenance = {"comment_id": ctx.source_id, **provenance}
    return format_untrusted_evidence(title, ctx.comment_text, provenance)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _team_context_hint(ctx: "CommentContext") -> Optional[str]:
    """Build a team-scoping instruction from a CommentContext, if available."""
    if not ctx.team:
        return None
    team_label = ctx.team.get("name") or ctx.team["id"]
    team_id = ctx.team["id"]
    parts = [
        f'**Team context:** This {ctx.root_asset_type} is in the "{team_label}" team'
        f" (team_id: `{team_id}`"
    ]
    if ctx.organization:
        org_label = ctx.organization.get("name") or ctx.organization["id"]
        parts.append(f', org: "{org_label}"')
    parts.append(
        "). When searching for or browsing content related to this "
        'conversation (e.g. "what\'s the latest"), default to passing this '
        "`team_id` to `search_assets` — unless the request references a "
        "different team or topic, in which case scope to that instead."
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# CommentContext — parsed once from event data, used by all comment handlers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommentContext:
    """All derived fields for a comment/mention event, parsed once."""

    source_id: str
    source_asset_type: str
    root_asset_id: str
    root_asset_type: str
    target_id: Optional[str]
    target_asset_type: Optional[str]
    parent_asset_id: Optional[str]
    is_thread_reply: bool
    reply_parent_id: str
    comment_text: str
    user: Optional[Dict[str, Any]] = None
    team: Optional[Dict[str, str]] = None
    organization: Optional[Dict[str, str]] = None

    @property
    def commenter(self) -> str:
        if self.user:
            return self.user.get("username", "someone")
        return "someone"

    @classmethod
    def from_event(cls, event: WebhookEvent) -> "CommentContext":
        data = event.data
        parent_asset_id = (
            event.parent_asset.id if event.parent_asset else data.get("parent_asset_id")
        )
        target_asset_type = data.get("target_asset_type")
        target_id = data.get("target_id")
        target_is_user = target_asset_type == "user"
        root_asset_id = (
            (event.root_asset.id if event.root_asset else None)
            or data.get("root_asset_id")
            or parent_asset_id
            or (target_id if not target_is_user else None)
            or event.source_id
            or "unknown"
        )
        root_asset_type = (
            (event.root_asset.type if event.root_asset else None)
            or data.get("root_asset_type")
            or (event.parent_asset.type if event.parent_asset else None)
            or (target_asset_type if not target_is_user else None)
            or event.source_asset_type
            or "unknown"
        )
        is_thread_reply = target_asset_type == "comment" or (
            bool(parent_asset_id) and parent_asset_id != root_asset_id
        )
        actor_dict: Optional[Dict[str, Any]] = None
        if event.actor:
            actor_dict = event.actor.model_dump()
        team_dict: Optional[Dict[str, str]] = (
            event.team.model_dump() if event.team else data.get("team")
        )
        org_dict: Optional[Dict[str, str]] = (
            event.organization.model_dump()
            if event.organization
            else data.get("organization")
        )
        return cls(
            source_id=event.source_id or "unknown",
            source_asset_type=event.source_asset_type or "unknown",
            root_asset_id=root_asset_id,
            root_asset_type=root_asset_type,
            target_id=target_id,
            target_asset_type=target_asset_type,
            parent_asset_id=parent_asset_id,
            is_thread_reply=is_thread_reply,
            reply_parent_id=event.source_id or "unknown",
            comment_text=data.get("text", ""),
            user=actor_dict,
            team=team_dict if isinstance(team_dict, dict) else None,
            organization=org_dict if isinstance(org_dict, dict) else None,
        )

    @property
    def thread_context_parent_id(self) -> Optional[str]:
        if not self.is_thread_reply:
            return None
        return self.parent_asset_id or self.target_id

    def build_prefetch(self) -> PrefetchSpec:
        can_fetch = (
            self.root_asset_id
            and self.root_asset_id != "unknown"
            and self.root_asset_type in FETCHABLE_ASSET_TYPES
        )
        asset_ids = [self.root_asset_id] if can_fetch else []
        comment_parent_ids = (
            [self.root_asset_id]
            if self.root_asset_id and self.root_asset_id != "unknown"
            else []
        )

        thread_comment_parent_ids: list[str] = []
        thread_parent_id = self.thread_context_parent_id
        if thread_parent_id and thread_parent_id != "unknown":
            thread_comment_parent_ids.append(thread_parent_id)

        return PrefetchSpec(
            asset_ids=asset_ids,
            comment_parent_ids=comment_parent_ids,
            thread_comment_parent_ids=thread_comment_parent_ids,
            # Only comments can be the focus of a thread; for post-body
            # mentions the source is the post itself.
            focus_comment_id=(
                self.source_id
                if self.source_id != "unknown"
                and self.source_asset_type == "comment"
                else None
            ),
            focus_comment_author=self.commenter,
            focus_comment_text=self.comment_text,
        )


# ---------------------------------------------------------------------------
# Task builders — one per provenance branch, each returns a task string
# ---------------------------------------------------------------------------


def _default_comment_task(
    ctx: CommentContext,
    event_type: str,
    provenance: Optional[AssetProvenance],
    preload_names: list[str],
) -> str:
    context_hint = (
        "The full post content, all top-level comments, and the "
        "current thread are provided below as pre-loaded context — "
        "no need to call get_asset or get_comments."
        if ctx.is_thread_reply
        else "The full post content and all comments are provided below "
        "as pre-loaded context — no need to call get_asset or get_comments."
    )

    if ctx.comment_text.strip():
        trigger = (
            f"**@{ctx.commenter}** wrote:\n\n"
            f"{_untrusted_comment_evidence(ctx, 'triggering comment')}"
        )
    else:
        # Mention embedded in the asset body itself: there is no separate
        # comment text, so don't render an empty evidence block.
        trigger = (
            f"**@{ctx.commenter}** mentioned you in the {ctx.root_asset_type} "
            "itself — the request is in the asset content provided below."
        )

    parts = [
        f"Received a {event_type} on a {ctx.root_asset_type} (id: {ctx.root_asset_id}).\n\n"
        f"{trigger}\n\n"
        f"{context_hint}",
    ]

    team_hint = _team_context_hint(ctx)
    if team_hint:
        parts.append(team_hint)

    hint = _ready_hint(preload_names)
    if hint:
        parts.append(hint)

    parts.append(
        _MENTION_ENGAGEMENT_GUIDANCE
        if event_type == "mention"
        else _COMMENT_ENGAGEMENT_GUIDANCE
    )

    if ctx.root_asset_type == "quest":
        parts.append(_QUEST_COMMENT_GUIDANCE)

    if ctx.is_thread_reply:
        parts.append(_THREAD_REPLY_CAUTION)

    if provenance and provenance.is_own_asset and event_type != "mention":
        parts.append(
            "This is your asset — you have extra context as the author. But even "
            "authors don't need to reply to every comment. Respond only if the "
            "commenter needs something from you."
        )

    # Never instruct a reply on the literal id "unknown"; fall back to the
    # root asset so the reply lands on the thread the event came from.
    reply_target = (
        ctx.reply_parent_id
        if ctx.reply_parent_id != "unknown"
        else ctx.root_asset_id
    )
    parts.append(
        f"If you decide to reply, use `write_comment` on `{reply_target}`. "
        f"{_NO_ENGAGEMENT_BAIT} "
        "If no reply is warranted, call `no_action` as the only tool."
    )
    return "\n\n".join(parts)


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
    root_asset_id: Optional[str] = None
    root_asset_type: Optional[str] = None
    reply_parent_id: Optional[str] = None
    thread_parent_id: Optional[str] = None
    feedback_text: Optional[str] = None
    actor_user_id: Optional[str] = None
    actor_username: Optional[str] = None
    actor_is_agent: Optional[bool] = None
    surface: EventSurface = EventSurface.UNKNOWN
    # Explicit per-event capability ceiling, or None to use the surface
    # defaults. None lets the envelope resolver elevate trusted actors on
    # actor-driven surfaces (e.g. controller comments).
    surface_capabilities: Optional[frozenset[Capability]] = None
    event_text: Optional[str] = None
    received_at: Optional[str] = None
    team_id: Optional[str] = None
    notification_ids: tuple[str, ...] = ()
    # User message turn to omit from history (the message this run is answering).
    trigger_turn_id: Optional[str] = None
    # The event's primary asset (when one is present in the payload). Always
    # populated from ``data.asset`` if the webhook includes it. Used by
    # cleanup handlers (asset.deleted) and by future asset-scoped routes.
    asset_id: Optional[str] = None
    asset_type: Optional[str] = None


def _build_event_task(
    event: WebhookEvent,
    provenance: Optional[AssetProvenance] = None,
    comment_ctx: Optional[CommentContext] = None,
) -> tuple[str, RunMode, tuple, PrefetchSpec]:
    """Build the task string, run mode, preload tools, and prefetch spec."""
    data = event.data
    event_type = event.event_type
    root_asset_type = comment_ctx.root_asset_type if comment_ctx else None
    preload_names = list(
        preloads_for_event(
            event_type, root_asset_type=root_asset_type, data=data
        )
    )
    prefetch = PrefetchSpec()

    if event_type == "new-message":
        sender = event.sender_username or "Unknown"
        content = data.get("text", "")
        conv = event.conversation_id or "unknown"
        sender_is_agent = bool(event.actor and event.actor.is_agent)
        sender_label = f"{sender} (an agent)" if sender_is_agent else sender
        task = (
            f"New conversation message from {sender_label} (conversation_id: {conv}).\n\n"
            f"{content}"
        )
        if sender_is_agent:
            task += f"\n\n{_AGENT_MESSAGE_ENGAGEMENT_GUIDANCE}"
        asset_hint = attached_asset_task_hint(attached_asset_ids(data))
        if asset_hint:
            task += f"\n\n{asset_hint}"
        hint = _ready_hint(preload_names)
        if hint:
            task += f"\n\n{hint}"
        return task, RunMode.CHAT, tuple(preload_names), prefetch

    if event_type == "new-conversation":
        return "", RunMode.CHAT, tuple(preload_names), prefetch

    if event_type in {"comment", "mention"}:
        ctx = comment_ctx or CommentContext.from_event(event)
        prefetch = ctx.build_prefetch()
        task = _default_comment_task(ctx, event_type, provenance, preload_names)
        return task, RunMode.AUTONOMOUS, tuple(preload_names), prefetch

    task = (
        f"Received event from Ouro: {event_type}\n\n"
        f"Event data:\n{json.dumps(data, indent=2, sort_keys=True)}\n\n"
        "Decide whether this event requires action from you. "
        "Most events do not — call `no_action` unless you have a clear, "
        "specific reason to act. If action is needed, use MCP tools to respond."
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
        event,
        provenance=provenance,
        comment_ctx=comment_ctx,
    )

    data = event.data or {}
    actor_is_agent = event.actor.is_agent if event.actor else None
    actor_username = event.sender_username or (
        comment_ctx.commenter if comment_ctx else None
    )
    event_text = data.get("text") if isinstance(data.get("text"), str) else None

    event_team_id = provenance.team_id if provenance else None
    if not event_team_id and event.team:
        event_team_id = event.team.id
    if not event_team_id and comment_ctx and comment_ctx.team:
        event_team_id = comment_ctx.team.get("id")
    if not event_team_id and isinstance(data.get("team_id"), str):
        event_team_id = data.get("team_id")

    event_thread_parent_id = None
    if comment_ctx:
        event_thread_parent_id = (
            comment_ctx.thread_context_parent_id
            or comment_ctx.parent_asset_id
            or comment_ctx.target_id
        )

    trigger_turn_id = None
    if event.event_type == "new-message":
        trigger_turn_id = data.get("turn_id") or data.get("id")
        if trigger_turn_id is not None:
            trigger_turn_id = str(trigger_turn_id)

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
        root_asset_id=comment_ctx.root_asset_id if comment_ctx else data.get("root_asset_id"),
        root_asset_type=(
            comment_ctx.root_asset_type if comment_ctx else data.get("root_asset_type")
        ),
        reply_parent_id=event.source_id if is_comment else None,
        thread_parent_id=event_thread_parent_id,
        feedback_text=comment_ctx.comment_text if comment_ctx else None,
        actor_user_id=event.actor_user_id,
        actor_username=actor_username,
        actor_is_agent=actor_is_agent,
        surface=event_surface_for(event.event_type),
        surface_capabilities=surface_capability_override_for(event.event_type),
        event_text=event_text,
        received_at=event.timestamp,
        team_id=event_team_id,
        notification_ids=event.notification_ids,
        trigger_turn_id=trigger_turn_id,
        asset_id=event.asset.id if event.asset else None,
        asset_type=event.asset.type if event.asset else None,
    )
