from __future__ import annotations

from collections.abc import Iterable

from .policy import Capability


OURO_TOOL_CAPABILITIES: dict[str, Capability] = {
    # Organization / team discovery
    "ouro:get_organizations": Capability.READ_PLATFORM,
    "ouro:get_teams": Capability.READ_PLATFORM,
    "ouro:get_team_feed": Capability.READ_PLATFORM,
    "ouro:create_team": Capability.UPDATE_ASSET,
    "ouro:update_team": Capability.UPDATE_ASSET,
    "ouro:set_team_membership": Capability.UPDATE_ASSET,
    # Assets
    "ouro:get_asset": Capability.READ_PLATFORM,
    "ouro:search_assets": Capability.READ_PLATFORM,
    "ouro:download_asset": Capability.READ_PLATFORM,
    "ouro:get_asset_connections": Capability.READ_PLATFORM,
    "ouro:get_compatible_routes": Capability.READ_PLATFORM,
    "ouro:list_asset_actions": Capability.READ_PLATFORM,
    "ouro:get_impact": Capability.READ_PLATFORM,
    "ouro:delete_asset": Capability.UPDATE_ASSET,
    "ouro:share_asset": Capability.UPDATE_ASSET,
    # Users
    "ouro:get_me": Capability.READ_PLATFORM,
    "ouro:search_users": Capability.READ_PLATFORM,
    # Posts / files / datasets
    "ouro:create_post": Capability.CREATE_ASSET,
    "ouro:update_post": Capability.UPDATE_ASSET,
    "ouro:create_file": Capability.CREATE_ASSET,
    "ouro:update_file": Capability.UPDATE_ASSET,
    "ouro:query_dataset": Capability.READ_PLATFORM,
    "ouro:create_dataset": Capability.CREATE_ASSET,
    "ouro:update_dataset": Capability.UPDATE_ASSET,
    "ouro:edit_dataset_columns": Capability.UPDATE_ASSET,
    "ouro:list_dataset_views": Capability.READ_PLATFORM,
    "ouro:write_dataset_view": Capability.CREATE_ASSET,
    "ouro:delete_dataset_view": Capability.UPDATE_ASSET,
    # Quests
    "ouro:create_quest": Capability.MANAGE_QUEST,
    "ouro:update_quest": Capability.MANAGE_QUEST,
    "ouro:list_assigned_quest_items": Capability.READ_PLATFORM,
    "ouro:list_quest_items": Capability.READ_PLATFORM,
    "ouro:create_quest_items": Capability.MANAGE_QUEST,
    "ouro:update_quest_item": Capability.MANAGE_QUEST,
    "ouro:complete_quest_item": Capability.MANAGE_QUEST,
    "ouro:delete_quest_item": Capability.MANAGE_QUEST,
    "ouro:submit_quest_entry": Capability.MANAGE_QUEST,
    "ouro:list_quest_entries": Capability.READ_PLATFORM,
    "ouro:list_quest_leaderboard": Capability.READ_PLATFORM,
    "ouro:review_quest_entry": Capability.MANAGE_QUEST,
    # Comments / conversations
    "ouro:get_comments": Capability.READ_PLATFORM,
    "ouro:write_comment": Capability.REPLY,
    "ouro:list_conversations": Capability.READ_PLATFORM,
    "ouro:get_conversation": Capability.READ_PLATFORM,
    "ouro:get_conversations": Capability.READ_PLATFORM,
    "ouro:create_conversation": Capability.SEND_MESSAGE,
    "ouro:send_message": Capability.SEND_MESSAGE,
    "ouro:list_messages": Capability.READ_PLATFORM,
    # Services / routes
    "ouro:create_service": Capability.CREATE_ASSET,
    "ouro:update_service": Capability.UPDATE_ASSET,
    "ouro:create_route": Capability.CREATE_ASSET,
    "ouro:update_route": Capability.UPDATE_ASSET,
    # Routes/actions
    "ouro:execute_route": Capability.EXECUTE_ROUTE,
    "ouro:get_action": Capability.READ_PLATFORM,
    "ouro:list_route_actions": Capability.READ_PLATFORM,
    "ouro:get_action_logs": Capability.READ_PLATFORM,
    # Money
    "ouro:get_balance": Capability.READ_PLATFORM,
    "ouro:get_transactions": Capability.READ_PLATFORM,
    "ouro:get_deposit_address": Capability.READ_PLATFORM,
    "ouro:get_usage_history": Capability.READ_PLATFORM,
    "ouro:get_pending_earnings": Capability.READ_PLATFORM,
    "ouro:add_funds": Capability.EXECUTE_ROUTE,
    "ouro:unlock_asset": Capability.EXECUTE_ROUTE,
    "ouro:send_money": Capability.EXECUTE_ROUTE,
    # Notifications
    "ouro:get_notifications": Capability.READ_PLATFORM,
    "ouro:read_notification": Capability.UPDATE_ASSET,
}

SERVER_CAPABILITIES: dict[str, Capability] = {
    "search": Capability.EXTERNAL_SEARCH,
    "resend": Capability.SEND_MESSAGE,
}


def capability_for_tool(tool_name: str) -> Capability | None:
    if tool_name in OURO_TOOL_CAPABILITIES:
        return OURO_TOOL_CAPABILITIES[tool_name]
    server, sep, _raw = tool_name.partition(":")
    if sep and server in SERVER_CAPABILITIES:
        return SERVER_CAPABILITIES[server]
    return None


def filter_deferred_tools(
    deferred_tools: dict,
    deferred_index: list[dict],
    allowed_capabilities: Iterable[Capability],
) -> tuple[dict, list[dict]]:
    allowed = set(allowed_capabilities)
    filtered_index = [
        item
        for item in deferred_index
        if (capability := capability_for_tool(str(item["tool"]))) is not None
        and capability in allowed
    ]
    filtered_names = {item["tool"] for item in filtered_index}
    filtered_tools = {
        name: tool for name, tool in deferred_tools.items() if name in filtered_names
    }
    return filtered_tools, filtered_index


def filter_deferred_by_servers(
    deferred_tools: dict,
    deferred_index: list[dict],
    servers: Iterable[str],
) -> tuple[dict, list[dict]]:
    """Keep only deferred tools whose MCP server is in *servers*."""
    allowed = set(servers)
    filtered_index = [
        item for item in deferred_index if item.get("server") in allowed
    ]
    filtered_names = {item["tool"] for item in filtered_index}
    filtered_tools = {
        name: tool for name, tool in deferred_tools.items() if name in filtered_names
    }
    return filtered_tools, filtered_index


def filter_deferred_excluding(
    deferred_tools: dict,
    deferred_index: list[dict],
    excluded: Iterable[str],
) -> tuple[dict, list[dict]]:
    """Drop deferred tools whose qualified names appear in *excluded*."""
    blocked = set(excluded)
    filtered_index = [
        item for item in deferred_index if item.get("tool") not in blocked
    ]
    filtered_tools = {
        name: tool for name, tool in deferred_tools.items() if name not in blocked
    }
    return filtered_tools, filtered_index


def resolve_preload_tools(
    qualified_names: Iterable[str],
    *,
    primary: dict,
    index: list[dict],
    fallback: dict | None = None,
) -> tuple[list, list[str], list[str]]:
    """Resolve eagerly preloaded deferred tools.

    Returns ``(tool_objects, raw_names, found_qualified_names)``. Looks up each
    qualified name in *primary*, then *fallback*. Raw names come from *index*
    when present.
    """
    tools: list = []
    raw_names: list[str] = []
    found: list[str] = []
    fallback = fallback or {}
    for qualified_name in qualified_names:
        tool_obj = primary.get(qualified_name) or fallback.get(qualified_name)
        if not tool_obj:
            continue
        tools.append(tool_obj)
        found.append(str(qualified_name))
        item = next(
            (entry for entry in index if entry.get("tool") == qualified_name),
            None,
        )
        raw_names.append(
            item["raw_name"] if item else str(qualified_name).split(":")[-1]
        )
    return tools, raw_names, found


def unmapped_tools(tool_names: Iterable[str]) -> list[str]:
    return sorted(name for name in tool_names if capability_for_tool(name) is None)
