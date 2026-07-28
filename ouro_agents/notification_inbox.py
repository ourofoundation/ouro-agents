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
from typing import Any, Optional, Sequence

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


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _notification_id(n: Any) -> str:
    return str(getattr(n, "id", None) or n.get("id") or "")


def _is_unread(n: Any) -> bool:
    """Treat as unread unless explicitly marked viewed/read."""
    viewed = n.get("viewed") if hasattr(n, "get") else getattr(n, "viewed", None)
    if viewed is True:
        return False
    read = n.get("read") if hasattr(n, "get") else getattr(n, "read", None)
    if read is True:
        return False
    return True


def _get_content(n: Any) -> dict:
    content = n.get("content") if hasattr(n, "get") else getattr(n, "content", None)
    return content if isinstance(content, dict) else {}


def _get_source_user(n: Any) -> dict:
    source = (
        n.get("source_user") if hasattr(n, "get") else getattr(n, "source_user", None)
    )
    return source if isinstance(source, dict) else {}


def _get_asset(n: Any) -> dict:
    asset = n.get("asset") if hasattr(n, "get") else getattr(n, "asset", None)
    if isinstance(asset, dict):
        return asset
    # Some payloads nest the asset under content.asset
    content_asset = _get_content(n).get("asset")
    return content_asset if isinstance(content_asset, dict) else {}


def fetch_unread(
    ouro: Any,
    max_fetch: int,
    categories: Sequence[str],
) -> list[Any]:
    """Fetch unread notifications, optionally filtered by backend categories."""
    category = ",".join(categories) if categories else None
    result = ouro.notifications.list(
        unread_only=True,
        limit=max_fetch,
        category=category,
    )
    if isinstance(result, dict):
        items = result.get("data") or []
    else:
        items = list(result or [])
    return [n for n in items if _is_unread(n)]


def expire_stale(
    ouro: Any,
    notifications: Sequence[Any],
    expire_after_hours: int,
    *,
    now: Optional[datetime] = None,
) -> tuple[list[Any], int]:
    """Mark unread notifications older than the cutoff as read.

    Returns ``(remaining, expired_count)``. Per-id failures are logged and
    skipped so one bad notification cannot abort the whole expiry pass.
    """
    if expire_after_hours <= 0:
        return list(notifications), 0

    clock = now or datetime.now(timezone.utc)
    cutoff = clock - timedelta(hours=expire_after_hours)
    remaining: list[Any] = []
    expired = 0

    for n in notifications:
        created = _as_datetime(
            n.get("created_at") if hasattr(n, "get") else getattr(n, "created_at", None)
        )
        if created is not None and created < cutoff:
            nid = _notification_id(n)
            try:
                ouro.notifications.read(nid)
                expired += 1
            except Exception:
                logger.warning(
                    "Failed to expire stale notification %s", nid, exc_info=True
                )
                remaining.append(n)
            continue
        remaining.append(n)

    return remaining, expired


def thread_key_for(n: Any) -> str:
    """Stable grouping key for a notification's conversation thread."""
    content = _get_content(n)
    parent = content.get("parent") if isinstance(content.get("parent"), dict) else {}
    content_asset = (
        content.get("asset") if isinstance(content.get("asset"), dict) else {}
    )

    for candidate in (
        parent.get("assetId"),
        parent.get("asset_id"),
        content_asset.get("assetId"),
        content_asset.get("id"),
        content_asset.get("asset_id"),
    ):
        if candidate:
            return str(candidate)

    asset_id = n.get("asset_id") if hasattr(n, "get") else getattr(n, "asset_id", None)
    if asset_id:
        return str(asset_id)

    asset = _get_asset(n)
    for candidate in (asset.get("id"), asset.get("asset_id")):
        if candidate:
            return str(candidate)

    return _notification_id(n) or "unknown"


def _format_actor(n: Any) -> str:
    source = _get_source_user(n)
    username = source.get("username") or source.get("name")
    label = f"@{username}" if username else "unknown"
    actor_type = source.get("actor_type")
    if actor_type == "agent" or source.get("is_agent") is True:
        return f"{label} (agent)"
    return label


def _snippet_for(n: Any, snippet_chars: int) -> str:
    content = _get_content(n)
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
    notifications: Sequence[Any],
    snippet_chars: int,
) -> list[InboxThread]:
    """Group notifications by thread; oldest-waiting threads first."""
    buckets: dict[str, list[Any]] = defaultdict(list)
    for n in notifications:
        buckets[thread_key_for(n)].append(n)

    threads: list[InboxThread] = []
    for key, items in buckets.items():
        items_sorted = sorted(
            items,
            key=lambda n: _as_datetime(
                n.get("created_at")
                if hasattr(n, "get")
                else getattr(n, "created_at", None)
            )
            or _MIN_DATETIME,
        )
        newest = items_sorted[-1]
        oldest = items_sorted[0]
        asset = _get_asset(newest)
        content_asset = _get_content(newest).get("asset")
        if not isinstance(content_asset, dict):
            content_asset = {}

        asset_name = (
            asset.get("name")
            or content_asset.get("name")
            or _short_thread_id(key)
        )
        asset_type = (
            asset.get("asset_type")
            or content_asset.get("asset_type")
            or "asset"
        )

        newest_at = (
            _as_datetime(
                newest.get("created_at")
                if hasattr(newest, "get")
                else getattr(newest, "created_at", None)
            )
            or _MIN_DATETIME
        )
        oldest_at = (
            _as_datetime(
                oldest.get("created_at")
                if hasattr(oldest, "get")
                else getattr(oldest, "created_at", None)
            )
            or _MIN_DATETIME
        )
        notif_type = (
            newest.get("type")
            if hasattr(newest, "get")
            else getattr(newest, "type", None)
        ) or "notification"

        threads.append(
            InboxThread(
                thread_key=key,
                asset_name=str(asset_name),
                asset_type=str(asset_type),
                notification_ids=[
                    nid for nid in (_notification_id(n) for n in items_sorted) if nid
                ],
                count=len(items_sorted),
                latest_actor=_format_actor(newest),
                latest_snippet=_snippet_for(newest, snippet_chars),
                latest_type=str(notif_type),
                oldest_at=oldest_at,
                newest_at=newest_at,
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
    ouro: Any,
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
