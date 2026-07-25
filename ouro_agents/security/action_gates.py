"""Small, explicit action categories used by the Ask Controller rollout."""

from __future__ import annotations

from typing import Optional


_MONEY = {"send_money", "add_funds", "unlock_asset"}
_DESTRUCTIVE = {
    "delete_asset",
    "delete_dataset_view",
    "delete_quest_item",
    "delete_scheduled_task",
}
_SCHEDULING = {
    "create_scheduled_task",
    "update_scheduled_task",
}
_EXTERNALLY_VISIBLE = {
    "send_email",
    "send_message",
    "write_comment",
    "create_post",
    "create_quest",
}


def observed_action_category(tool_name: str) -> Optional[str]:
    """Return the MVP observation category for a side-effecting tool."""
    raw_name = (tool_name or "").split(":")[-1]
    if raw_name in _MONEY:
        return "money"
    if raw_name in _DESTRUCTIVE:
        return "destructive"
    if raw_name in _SCHEDULING:
        return "scheduling"
    if raw_name in _EXTERNALLY_VISIBLE:
        return "externally_visible"
    return None

