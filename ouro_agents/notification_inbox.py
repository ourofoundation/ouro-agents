"""Heartbeat Notification Inbox — fetch, expire, group, and render unread notifications.

Unread notifications are the triage queue. The digest groups them by thread so a
burst of comments on one asset costs one line. The agent then handle / dismiss /
defer each thread; handled and dismissed ids are marked read via the existing
``read_notification`` MCP tool (batch-capable). Deferred ids stay unread and
reappear next tick. Stale unread items older than ``expire_after_hours`` are
marked read automatically so the queue stays bounded.

Two guards keep the agent from answering the same comment twice. Each digest
thread carries the live comment thread, with the agent's own replies flagged,
so an already-answered item is visible before any reply is drafted. And every
successful ``write_comment`` marks the notifications raised by the comment it
answered as read, whatever mode the run was in (chat runs never see the digest).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from ouro import Ouro
from ouro.models import Notification

from .config import NotificationInboxConfig

logger = logging.getLogger(__name__)

_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class InboxThread:
    thread_key: str
    asset_name: str
    asset_type: str
    notification_ids: list[str]
    count: int
    latest_actor: str
    latest_snippet: str
    latest_type: str
    oldest_at: datetime
    newest_at: datetime
    # Live comment thread per notifying comment, filled by attach_thread_context.
    context: list["ThreadContext"] = field(default_factory=list)
    # (source comment id, parent comment id or None, notified at), oldest first.
    sources: list[tuple[str, Optional[str], datetime]] = field(default_factory=list)


@dataclass
class ContextComment:
    comment_id: str
    author: str
    text: str
    created_at: datetime
    is_own: bool = False
    is_trigger: bool = False


@dataclass
class ThreadContext:
    parent_id: str
    comments: list[ContextComment]
    omitted: int = 0
    # The agent commented in this thread after the notification was raised.
    already_replied: bool = False


@dataclass
class NotificationInbox:
    section: Optional[str] = None
    notification_ids: list[str] = field(default_factory=list)
    thread_count: int = 0


def _created_at(n: Notification) -> datetime:
    created = n.created_at
    if created is None:
        return _MIN_DATETIME
    if created.tzinfo is None:
        return created.replace(tzinfo=timezone.utc)
    return created


def _content_field(n: Notification, key: str) -> dict:
    value = (n.content or {}).get(key)
    return value if isinstance(value, dict) else {}


def _asset_ref_type(ref: dict) -> str:
    return str(ref.get("assetType") or ref.get("asset_type") or "")


def _asset_ref_id(ref: dict) -> Optional[str]:
    value = ref.get("assetId") or ref.get("asset_id") or ref.get("id")
    return str(value) if value else None


def source_comment_id(n: Notification) -> Optional[str]:
    """Id of the comment that raised this notification, if a comment did."""
    content_asset = _content_field(n, "asset")
    if _asset_ref_type(content_asset) == "comment":
        return _asset_ref_id(content_asset) or (str(n.asset_id) if n.asset_id else None)
    if n.asset and n.asset.asset_type == "comment":
        return str(n.asset_id or n.asset.id)
    return None


def parent_comment_id(n: Notification) -> Optional[str]:
    """Id of the comment thread the notifying comment sits in, if it is a reply."""
    parent = _content_field(n, "parent")
    if _asset_ref_type(parent) != "comment":
        return None
    return _asset_ref_id(parent) or (
        str(n.parent_asset_id) if n.parent_asset_id else None
    )


def fetch_unread(
    ouro: Ouro,
    max_fetch: int,
    categories: Sequence[str],
) -> list[Notification]:
    """Fetch unread notifications, optionally filtered by backend categories."""
    category = ",".join(categories) if categories else None
    page = ouro.notifications.list(
        unread_only=True,
        limit=max_fetch,
        category=category,
    )
    return [n for n in page if not n.viewed]


def expire_stale(
    ouro: Ouro,
    notifications: Sequence[Notification],
    expire_after_hours: int,
    *,
    now: Optional[datetime] = None,
) -> tuple[list[Notification], int]:
    """Mark unread notifications older than the cutoff as read.

    Returns ``(remaining, expired_count)``. Per-id failures are logged and
    skipped so one bad notification cannot abort the whole expiry pass.
    """
    if expire_after_hours <= 0:
        return list(notifications), 0

    clock = now or datetime.now(timezone.utc)
    cutoff = clock - timedelta(hours=expire_after_hours)
    remaining: list[Notification] = []
    expired = 0

    for n in notifications:
        if n.created_at is not None and _created_at(n) < cutoff:
            try:
                ouro.notifications.read(str(n.id))
                expired += 1
            except Exception:
                logger.warning(
                    "Failed to expire stale notification %s", n.id, exc_info=True
                )
                remaining.append(n)
            continue
        remaining.append(n)

    return remaining, expired


def thread_key_for(n: Notification) -> str:
    """Stable grouping key for a notification's conversation thread."""
    parent = _content_field(n, "parent")
    content_asset = _content_field(n, "asset")

    for candidate in (
        parent.get("assetId"),
        parent.get("asset_id"),
        content_asset.get("assetId"),
        content_asset.get("id"),
        content_asset.get("asset_id"),
        n.asset_id,
        n.asset.id if n.asset else None,
    ):
        if candidate:
            return str(candidate)

    return str(n.id)


def _format_actor(n: Notification) -> str:
    source = n.source_user
    username = source and (source.username or source.name)
    label = f"@{username}" if username else "unknown"
    if source and source.is_agent:
        return f"{label} (agent)"
    return label


def _snippet_for(n: Notification, snippet_chars: int) -> str:
    content = n.content or {}
    text = content.get("text") or content.get("message") or ""
    if not isinstance(text, str):
        text = str(text)
    compact = " ".join(text.split())
    if not compact:
        return "(no text)"
    if len(compact) <= snippet_chars:
        return compact
    return f"{compact[: snippet_chars - 3]}..."


def _short_thread_id(thread_key: str) -> str:
    return thread_key[:8] if len(thread_key) > 8 else thread_key


def group_threads(
    notifications: Sequence[Notification],
    snippet_chars: int,
) -> list[InboxThread]:
    """Group notifications by thread; oldest-waiting threads first."""
    buckets: dict[str, list[Notification]] = defaultdict(list)
    for n in notifications:
        buckets[thread_key_for(n)].append(n)

    threads: list[InboxThread] = []
    for key, items in buckets.items():
        items_sorted = sorted(items, key=_created_at)
        newest = items_sorted[-1]
        oldest = items_sorted[0]
        content_asset = _content_field(newest, "asset")

        asset_name = (
            (newest.asset and newest.asset.name)
            or content_asset.get("name")
            or _short_thread_id(key)
        )
        asset_type = (
            (newest.asset and newest.asset.asset_type)
            or content_asset.get("asset_type")
            or "asset"
        )

        threads.append(
            InboxThread(
                thread_key=key,
                asset_name=str(asset_name),
                asset_type=str(asset_type),
                notification_ids=[str(n.id) for n in items_sorted],
                count=len(items_sorted),
                latest_actor=_format_actor(newest),
                latest_snippet=_snippet_for(newest, snippet_chars),
                latest_type=newest.type or "notification",
                oldest_at=_created_at(oldest),
                newest_at=_created_at(newest),
                sources=[
                    (source, parent_comment_id(n), _created_at(n))
                    for n in items_sorted
                    if (source := source_comment_id(n))
                ],
            )
        )

    threads.sort(key=lambda t: t.oldest_at)
    return threads


def _own_user_id(ouro: Ouro) -> Optional[str]:
    user = getattr(ouro, "user", None)
    value = getattr(user, "id", None) or getattr(user, "user_id", None)
    return str(value) if value else None


def _compact(text: str, limit: int) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def _context_comment(
    comment: Any,
    *,
    own_user_id: Optional[str],
    trigger_ids: set[str],
    chars: int,
) -> ContextComment:
    user = getattr(comment, "user", None)
    username = user and (user.username or user.name)
    return ContextComment(
        comment_id=str(comment.id),
        author=f"@{username}" if username else "unknown",
        text=_compact(comment.text, chars) or "(no text)",
        created_at=_created_at(comment),
        is_own=bool(own_user_id) and str(comment.user_id) == own_user_id,
        is_trigger=str(comment.id) in trigger_ids,
    )


def attach_thread_context(
    ouro: Ouro,
    threads: Sequence[InboxThread],
    *,
    own_user_id: Optional[str] = None,
    max_comments: int = 8,
    comment_chars: int = 400,
    max_sources: int = 3,
) -> None:
    """Load the live comment thread behind each inbox thread, in place.

    A reply to a comment is a child of that comment, so the thread for a
    top-level notifying comment is the comment plus its replies; for a
    notifying reply it is the parent comment plus all sibling replies. The
    agent's own comments are flagged, and ``already_replied`` is set when one
    of them is newer than the notification. Per-thread failures are logged and
    skipped: missing context must never hide the notification itself.
    """
    own_id = own_user_id or _own_user_id(ouro)

    for thread in threads:
        # Newest notifications first; several may share one comment thread.
        wanted: dict[str, tuple[set[str], datetime]] = {}
        for source_id, parent_id, notified_at in reversed(thread.sources):
            context_parent = parent_id or source_id
            if context_parent in wanted:
                triggers, first_notified = wanted[context_parent]
                triggers.add(source_id)
                wanted[context_parent] = (triggers, min(first_notified, notified_at))
            elif len(wanted) < max_sources:
                wanted[context_parent] = ({source_id}, notified_at)

        for context_parent, (trigger_ids, notified_at) in wanted.items():
            try:
                replies = list(ouro.comments.list(context_parent, limit=50))
                head = ouro.comments.retrieve(context_parent)
            except Exception:
                logger.warning(
                    "Failed to load thread context for %s", context_parent, exc_info=True
                )
                continue

            comments = [
                _context_comment(
                    c, own_user_id=own_id, trigger_ids=trigger_ids, chars=comment_chars
                )
                for c in [head, *replies]
            ]
            already_replied = any(
                c.is_own and c.created_at > notified_at for c in comments
            )
            # Keep the head comment and the most recent replies.
            omitted = max(0, len(comments) - max_comments)
            if omitted:
                comments = [comments[0], *comments[omitted + 1 :]]
            thread.context.append(
                ThreadContext(
                    parent_id=context_parent,
                    comments=comments,
                    omitted=omitted,
                    already_replied=already_replied,
                )
            )


def _render_thread_context(
    context: ThreadContext, *, now: Optional[datetime] = None
) -> list[str]:
    lines = [f"   Thread on comment {context.parent_id}:"]
    for index, comment in enumerate(context.comments):
        if index == 1 and context.omitted:
            lines.append(f"   - (... {context.omitted} earlier replies omitted)")
        marks = ""
        if comment.is_own:
            marks += " (you)"
        if comment.is_trigger:
            marks += " [notified you]"
        age = _format_age(comment.created_at, now=now)
        lines.append(
            f'   - {comment.author}{marks}, {age} ago: "{comment.text}" '
            f"[id: {comment.comment_id}]"
        )
    if context.already_replied:
        lines.append(
            "   ALREADY ANSWERED: you commented in this thread after the "
            "notification. Dismiss it unless you have something new that your "
            "earlier reply did not cover."
        )
    return lines


def _format_age(when: datetime, *, now: Optional[datetime] = None) -> str:
    clock = now or datetime.now(timezone.utc)
    delta = clock - when
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        return f"{minutes}m"
    hours = seconds // 3600
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def render_inbox(
    threads: Sequence[InboxThread],
    expired_count: int,
    max_threads: int,
    *,
    expire_after_hours: int = 72,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Render the Notification Inbox markdown section, or None if empty."""
    if not threads and not expired_count:
        return None

    shown = list(threads[:max_threads])
    overflow = max(0, len(threads) - len(shown))
    lines = [
        "## Notification Inbox",
        (
            f"{len(threads)} thread(s) with unread notifications await triage. "
            "This is SECONDARY to the work above — only let an inbox item preempt "
            "planned work when it is a direct request from a human or blocks your "
            "own active work."
        ),
        "",
        "For each thread decide one of:",
        (
            "- **Handle**: read the thread shown under the item (open more with "
            "`get_comments`/`get_asset` if needed), reply once with "
            "`write_comment`. Check the thread for a reply of yours first — an "
            "item marked ALREADY ANSWERED needs no second reply."
        ),
        (
            "- **Dismiss**: needs no reply ever (social closings, agent chatter "
            "that asks you nothing, concluded threads). Silence is the default — "
            "most items end here."
        ),
        (
            "- **Defer**: genuinely needs action you cannot take this tick. "
            "Do nothing; it will reappear next heartbeat."
        ),
        "",
        (
            "Finish triage with ONE `read_notification(ids=[...])` call listing "
            "every id you handled or dismissed. Leave deferred ids out. Never "
            "reply to a thread without marking its ids read — otherwise you may "
            "double-reply next tick."
        ),
        "",
    ]

    if shown:
        for index, thread in enumerate(shown, 1):
            ids_repr = ", ".join(thread.notification_ids)
            age = _format_age(thread.newest_at, now=now)
            lines.append(
                f'{index}. [{thread.count} unread] {thread.latest_type} on '
                f'{thread.asset_type} "{thread.asset_name}" '
                f"(thread {_short_thread_id(thread.thread_key)}) — "
                f"latest from {thread.latest_actor}, {age} ago: "
                f'"{thread.latest_snippet}"'
            )
            lines.append(f"   ids: [{ids_repr}]")
            for context in thread.context:
                lines.extend(_render_thread_context(context, now=now))
        lines.append("")

    if overflow:
        lines.append(
            f"(+{overflow} more threads not shown; oldest render first, "
            "the rest surface next tick.)"
        )
    if expired_count:
        lines.append(
            f"(Expired {expired_count} stale notification(s) older than "
            f"{expire_after_hours}h — marked read, no action taken.)"
        )

    # Trim trailing blank line if present
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def build_notification_inbox(
    ouro: Ouro,
    cfg: NotificationInboxConfig,
    *,
    now: Optional[datetime] = None,
    own_user_id: Optional[str] = None,
) -> NotificationInbox:
    """Compose fetch → expire → group → render for a heartbeat tick.

    Any failure returns an empty inbox so a broken notifications API never
    aborts the heartbeat.
    """
    try:
        notifications = fetch_unread(ouro, cfg.max_fetch, cfg.categories)
        remaining, expired_count = expire_stale(
            ouro,
            notifications,
            cfg.expire_after_hours,
            now=now,
        )
        threads = group_threads(remaining, cfg.snippet_chars)
        if cfg.thread_context:
            attach_thread_context(
                ouro,
                threads[: cfg.max_threads],
                own_user_id=own_user_id,
                max_comments=cfg.context_comments,
                comment_chars=cfg.context_chars,
            )
        section = render_inbox(
            threads,
            expired_count,
            cfg.max_threads,
            expire_after_hours=cfg.expire_after_hours,
            now=now,
        )
        if not section:
            return NotificationInbox()
        return NotificationInbox(
            section=section,
            notification_ids=[
                nid for thread in threads for nid in thread.notification_ids
            ],
            thread_count=len(threads),
        )
    except Exception:
        logger.warning("Failed to build notification inbox", exc_info=True)
        return NotificationInbox()


def mark_replied_notifications_read(
    ouro: Ouro,
    parent_ids: Sequence[str],
    cfg: NotificationInboxConfig,
) -> list[str]:
    """Mark the unread notifications a comment on ``parent_ids`` answers as read.

    That is every notification raised by the comment being replied to, plus
    those raised by other replies when the parent is itself the comment thread
    they sit in. A top-level comment on a post or quest answers nobody in
    particular, so it clears nothing.
    """
    answered = {str(pid) for pid in parent_ids if pid}
    marked: list[str] = []
    if not answered:
        return marked
    for n in fetch_unread(ouro, cfg.max_fetch, cfg.categories):
        if not answered & {source_comment_id(n), parent_comment_id(n)}:
            continue
        try:
            ouro.notifications.read(str(n.id))
            marked.append(str(n.id))
        except Exception:
            logger.warning(
                "Failed to mark answered notification %s as read", n.id, exc_info=True
            )
    return marked


def _posted_comment(result: Any) -> Optional[dict]:
    """The created comment from a ``write_comment`` result, or None if it failed.

    MCP tool errors come back as text rather than raising, so only a JSON
    comment payload counts as posted.
    """
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(data, dict) or not data.get("id") or data.get("error"):
        return None
    return data


def wrap_write_comment_with_mark_read(
    tool: Any,
    *,
    get_ouro: Callable[[], Optional[Ouro]],
    get_config: Callable[[], NotificationInboxConfig],
) -> Any:
    """Wrap ``write_comment`` so a posted reply clears the notification it answers.

    The model is told to call ``read_notification`` after replying, but runs
    outside the heartbeat digest (chat, realtime events) never see the ids, and
    a skipped call means the next heartbeat answers the same comment again.
    Nothing here may fail the comment itself.
    """
    original_forward: Callable[..., Any] = tool.forward

    def forward_and_mark_read(*args: Any, **kwargs: Any) -> Any:
        result = original_forward(*args, **kwargs)
        try:
            posted = _posted_comment(result)
            arguments = args[0] if args and isinstance(args[0], dict) else kwargs
            # The backend may re-parent a reply onto its thread's head comment,
            # so match both the requested parent and the one it landed on.
            parent_ids = [
                str(pid)
                for pid in (arguments.get("parent_id"), (posted or {}).get("parent_id"))
                if pid
            ]
            ouro = get_ouro() if posted and parent_ids else None
            if ouro is not None:
                marked = mark_replied_notifications_read(ouro, parent_ids, get_config())
                if marked:
                    logger.info(
                        "Marked %d notification(s) read after replying on %s",
                        len(marked),
                        parent_ids[0],
                    )
        except Exception:
            logger.warning(
                "Failed to mark notifications read after write_comment", exc_info=True
            )
        return result

    tool.forward = forward_and_mark_read  # type: ignore[method-assign]
    return tool
