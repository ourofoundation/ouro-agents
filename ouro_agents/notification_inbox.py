"""Heartbeat Notification Inbox — fetch, expire, group, and render unread notifications.

Unread notifications are the triage queue. The digest groups them by thread so a
burst of comments on one asset costs one line. The agent then handle / dismiss /
defer each thread; handled and dismissed ids are marked read via the existing
``read_notification`` MCP tool (batch-capable). Deferred ids stay unread and
reappear next tick. Stale unread items older than ``expire_after_hours`` are
marked read automatically so the queue stays bounded.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

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
            )
        )

    threads.sort(key=lambda t: t.oldest_at)
    return threads


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
            "- **Handle**: open with `get_comments`/`get_asset`, reply once with "
            "`write_comment`."
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
