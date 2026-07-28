"""MCP tool name → ouro-py SDK equivalents for route authoring and mining."""

from __future__ import annotations

# Keys may be bare MCP tool names or ``ouro:``-qualified forms.
MCP_TO_OURO_PY: dict[str, str] = {
    "search_assets": "ouro.assets.search",
    "get_asset": "ouro.assets.retrieve",
    "download_asset": "ouro.assets.download",
    "share_asset": "ouro.assets.share",
    "get_asset_connections": "ouro.assets.connections",
    "list_asset_actions": "ouro.assets.actions",
    "delete_asset": "ouro.assets.delete",
    "get_comments": "ouro.comments.list_by_parent",
    "write_comment": "ouro.comments.create",
    "query_dataset": "ouro.datasets.query",
    "create_dataset": "ouro.datasets.create",
    "update_dataset": "ouro.datasets.update",
    "create_post": "ouro.posts.create",
    "update_post": "ouro.posts.update",
    "create_file": "ouro.files.create",
    "update_file": "ouro.files.update",
    "create_service": "ouro.services.create",
    "update_service": "ouro.services.update",
    "create_route": "ouro.routes.create",
    "update_route": "ouro.routes.update",
    "execute_route": "ouro.routes.execute",
    "get_action": "ouro.routes.retrieve_action",
    "list_route_actions": "ouro.routes.list_actions",
    "get_action_logs": "ouro.routes.retrieve_action_logs",
    "list_conversations": "ouro.conversations.list",
    "get_conversation": "ouro.conversations.retrieve",
    "get_conversations": "ouro.conversations.list",
    "create_conversation": "ouro.conversations.create",
    "send_message": "ouro.conversations.send_message",
    "list_messages": "ouro.conversations.list_messages",
    "get_teams": "ouro.teams.list",
    "create_team": "ouro.teams.create",
    "update_team": "ouro.teams.update",
    "get_organizations": "ouro.organizations.list",
    "get_me": "ouro.users.me",
    "search_users": "ouro.users.search",
    "create_quest": "ouro.quests.create",
    "update_quest": "ouro.quests.update",
    "list_quest_items": "ouro.quests.list_items",
    "list_quest_entries": "ouro.quests.list_entries",
    "submit_quest_entry": "ouro.quests.submit_entry",
    "get_notifications": "ouro.notifications.list",
    "read_notification": "ouro.notifications.read",
}


def normalize_tool_name(name: str) -> str:
    """Strip optional ``ouro:`` / ``server:`` prefix from a tool name."""
    raw = str(name or "").strip()
    if ":" in raw:
        return raw.split(":", 1)[1]
    return raw


def sdk_equivalent(tool_name: str) -> str | None:
    """Return the ouro-py call form for an MCP tool, if known."""
    bare = normalize_tool_name(tool_name)
    return MCP_TO_OURO_PY.get(bare)


def annotate_signature(signature: list[str]) -> str:
    """Format ``search_assets (ouro.assets.search) -> get_asset (...)``."""
    parts: list[str] = []
    for name in signature:
        bare = normalize_tool_name(name)
        sdk = sdk_equivalent(bare)
        if sdk:
            parts.append(f"{bare} ({sdk})")
        else:
            parts.append(bare)
    return " -> ".join(parts)
