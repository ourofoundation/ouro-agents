"""Local event metadata for the agent runtime.

This is the single source of truth in ``ouro_agents`` for:
  - which events are "chat" events (drive realtime presence/streaming)
  - which MCP tools to preload per event type (static lists; payload extras
    are composed in ``tool_preloads.preloads_for_event``)
  - how to compute a pool key for debouncing/coalescing

The event names themselves are imported from ``ouro.events``
(``WEBHOOK_EVENT_TYPES``), which mirrors the canonical registry in
``ouro-js``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from ouro.events import WEBHOOK_EVENT_TYPES

from .security.policy import Capability, EventSurface, capabilities_for_surface

if TYPE_CHECKING:
    from .events import EventRunContext


PoolKeyFn = Callable[["EventRunContext"], Optional[str]]


@dataclass(frozen=True)
class EventSpec:
    """Per-event-type behavior owned by the agent runtime."""

    is_chat: bool = False
    """Drives realtime activity emission and streaming."""

    tool_preloads: tuple[str, ...] = ()
    """MCP tool names eagerly loaded before the agent starts the task."""

    surface: EventSurface = EventSurface.UNKNOWN
    """Security surface used to cap capabilities before each run."""

    max_capabilities: Optional[frozenset[Capability]] = None
    """Optional explicit surface cap; defaults from ``surface`` when omitted."""

    pool_key_fn: Optional[PoolKeyFn] = None
    """Compute a pool key for debouncing. ``None`` means "do not pool"."""


# ---------------------------------------------------------------------------
# Pool key strategies
# ---------------------------------------------------------------------------


def _thread_pool_key(event: "EventRunContext") -> Optional[str]:
    if _is_top_level_asset_comment(event):
        thread_id = event.source_id or event.reply_parent_id
    else:
        thread_id = (
            event.thread_parent_id
            or event.root_asset_id
            or event.reply_parent_id
            or event.source_id
        )
    if not thread_id:
        return None
    return f"thread:{thread_id}"


def _is_top_level_asset_comment(event: "EventRunContext") -> bool:
    if event.event_type not in {"comment", "mention"}:
        return False
    if not (event.source_id or event.reply_parent_id):
        return False
    if event.root_asset_type == "comment":
        return False
    return not event.thread_parent_id or event.thread_parent_id == event.root_asset_id


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_COMMENT_TOOL_PRELOADS: tuple[str, ...] = (
    "ouro:get_asset",
    "ouro:write_comment",
    "ouro:get_comments",
)


EVENT_REGISTRY: dict[str, EventSpec] = {
    "comment": EventSpec(
        tool_preloads=_COMMENT_TOOL_PRELOADS,
        surface=EventSurface.COMMENT,
        pool_key_fn=_thread_pool_key,
    ),
    "mention": EventSpec(
        tool_preloads=_COMMENT_TOOL_PRELOADS,
        surface=EventSurface.MENTION,
        pool_key_fn=_thread_pool_key,
    ),
    "new-message": EventSpec(
        is_chat=True,
        surface=EventSurface.DIRECT_CHAT,
    ),
    "new-conversation": EventSpec(is_chat=True, surface=EventSurface.DIRECT_CHAT),
    # Cleanup events: handled synchronously by the cleanup module before the
    # LLM run path ever sees them. No tool preloads, no pooling.
    "asset.deleted": EventSpec(surface=EventSurface.CLEANUP),
}


# Fail loudly during import if the registry references an event name that the
# canonical registry doesn't know about. This catches typos before runtime.
for _name in EVENT_REGISTRY:
    if _name not in WEBHOOK_EVENT_TYPES:
        raise RuntimeError(
            f"EVENT_REGISTRY references unknown event type '{_name}'. "
            f"Allowed: {WEBHOOK_EVENT_TYPES}"
        )


def spec_for(event_type: str) -> EventSpec:
    """Get the spec for an event type, defaulting to a zero-config spec."""
    return EVENT_REGISTRY.get(event_type, EventSpec())


def is_chat_event(event_type: str) -> bool:
    return spec_for(event_type).is_chat


def tool_preloads_for(event_type: str) -> tuple[str, ...]:
    return spec_for(event_type).tool_preloads


def event_surface_for(event_type: str) -> EventSurface:
    return spec_for(event_type).surface


def max_capabilities_for(event_type: str) -> frozenset[Capability]:
    spec = spec_for(event_type)
    return spec.max_capabilities or capabilities_for_surface(spec.surface)


def surface_capability_override_for(
    event_type: str,
) -> Optional[frozenset[Capability]]:
    """Explicit per-event capability ceiling, or ``None`` to use the surface
    defaults. Unlike :func:`max_capabilities_for`, this preserves the
    distinction between "no override" and "override equal to the surface
    default" so the envelope resolver can elevate trusted actors only when no
    explicit ceiling was set."""
    return spec_for(event_type).max_capabilities
