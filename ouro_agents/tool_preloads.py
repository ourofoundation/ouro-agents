"""MCP tool preload catalog and context-aware resolution.

Preloads are the tools we attach before the model starts, because we
already know it will need them. They come from three layers, merged
first-seen (context extras first, then mode defaults):

1. **Context extras** — this module. Event payload, quest inbox, planning.
2. **Mode profile defaults** — ``ModeProfile.preload_tools``.
3. **Capability envelope** — subtracts anything the role/surface cannot use.
   Roles never add tools; they only cap them.

Static per-event-type lists still live on ``EventSpec.tool_preloads``.
Payload-dependent extras (quest comments, attached assets) are resolved
here so adding a case is one named set plus one branch, not another
if-statement in the event or mode builder.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .event_registry import tool_preloads_for
from .security.policy import Capability
from .security.tool_capabilities import capability_for_tool

GET_ASSET = "ouro:get_asset"

# ---------------------------------------------------------------------------
# Named sets — the catalog
# ---------------------------------------------------------------------------

AUTONOMOUS_ACTION: tuple[str, ...] = (
    "ouro:search_assets",
    GET_ASSET,
    "ouro:execute_route",
    "ouro:get_action",
)

HEARTBEAT_DEFAULT: tuple[str, ...] = (
    "ouro:search_assets",
    GET_ASSET,
    "ouro:write_comment",
    "ouro:create_post",
)

HEARTBEAT_INBOX: tuple[str, ...] = (
    GET_ASSET,
    "ouro:list_quest_items",
    "ouro:update_quest_item",
    "ouro:create_quest_items",
    "ouro:delete_quest_item",
    "ouro:complete_quest_item",
    "ouro:submit_quest_entry",
    "ouro:write_comment",
    "ouro:update_quest",
)

HEARTBEAT_NOTIFICATIONS: tuple[str, ...] = (
    GET_ASSET,
    "ouro:get_comments",
    "ouro:write_comment",
    "ouro:read_notification",
)

PLANNING: tuple[str, ...] = (
    "ouro:search_assets",
    GET_ASSET,
    "ouro:get_comments",
    "ouro:create_quest",
    "ouro:create_quest_items",
    "ouro:update_quest",
    "ouro:list_quest_items",
    "ouro:get_impact",
)

QUEST_COMMENT: tuple[str, ...] = (
    GET_ASSET,
    "ouro:get_comments",
    "ouro:write_comment",
    "ouro:update_quest",
    "ouro:list_quest_items",
    "ouro:create_quest_items",
    "ouro:update_quest_item",
    "ouro:delete_quest_item",
    "ouro:complete_quest_item",
)

# ---------------------------------------------------------------------------
# Attached assets in the triggering payload
# ---------------------------------------------------------------------------

_ASSET_COMPONENT_RE = re.compile(
    r"```assetComponent\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def _ids_from_markdown(text: str) -> list[str]:
    ids: list[str] = []
    for match in _ASSET_COMPONENT_RE.finditer(text):
        try:
            payload = json.loads(match.group("body"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            asset_id = payload.get("id")
            if isinstance(asset_id, str) and asset_id.strip():
                ids.append(asset_id.strip())
    return ids


def _ids_from_json(node: Any) -> list[str]:
    ids: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "assetComponent":
            attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
            asset_id = attrs.get("id") or node.get("id")
            if isinstance(asset_id, str) and asset_id.strip():
                ids.append(asset_id.strip())
        for value in node.values():
            ids.extend(_ids_from_json(value))
    elif isinstance(node, list):
        for item in node:
            ids.extend(_ids_from_json(item))
    return ids


def attached_asset_ids(data: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Asset ids embedded in the triggering message or comment.

    Accepts an explicit ``attached_assets`` list from the producer, TipTap
    ``json`` with ``assetComponent`` nodes, and `` ```assetComponent ``
    fences in ``text``.
    """
    if not data:
        return ()
    ids: list[str] = []

    raw = data.get("attached_assets")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                ids.append(item.strip())
            elif isinstance(item, Mapping):
                asset_id = item.get("id")
                if isinstance(asset_id, str) and asset_id.strip():
                    ids.append(asset_id.strip())

    ids.extend(_ids_from_json(data.get("json")))

    text = data.get("text")
    if isinstance(text, str):
        ids.extend(_ids_from_markdown(text))

    return tuple(dict.fromkeys(ids))


def attached_asset_task_hint(ids: Iterable[str]) -> str:
    listed = tuple(ids)
    if not listed:
        return ""
    refs = ", ".join(f"`{asset_id}`" for asset_id in listed)
    return (
        f"This message includes attached asset(s): {refs}. "
        "Use `get_asset` to load their content."
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_QUEST_EVENT_TYPES = frozenset({"comment", "mention"})


def preloads_for_event(
    event_type: str,
    *,
    root_asset_type: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """MCP tools to attach before the run, based on the triggering event.

    Starts from the static ``EventSpec`` list, then applies payload rules.
    Role/surface never add tools here — they subtract later via the
    capability envelope.
    """
    if event_type in _QUEST_EVENT_TYPES and root_asset_type == "quest":
        names = list(QUEST_COMMENT)
    else:
        names = list(tool_preloads_for(event_type))

    if attached_asset_ids(data):
        names.append(GET_ASSET)

    return tuple(dict.fromkeys(names))


def merge_preloads(*groups: Iterable[str] | None) -> list[str]:
    """Stable first-seen dedup across preload sources."""
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for name in group or ():
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def filter_preloads(
    names: Iterable[str],
    allowed_capabilities: frozenset[Capability] | None,
) -> list[str]:
    """Drop preloads the current envelope cannot use.

    Unmapped names are dropped when an envelope is active — same as
    deferred-tool filtering — so a missing capability map cannot sneak
    a tool past the cap.
    """
    if allowed_capabilities is None:
        return list(names)
    filtered: list[str] = []
    for tool_name in names:
        capability = capability_for_tool(tool_name)
        if capability is not None and capability in allowed_capabilities:
            filtered.append(tool_name)
    return filtered
