"""Event provenance — resolves whether an event relates to the agent's own work.

When a webhook event arrives (e.g. a comment), this module checks the payload
against cheap local state to determine: is this about something I created?
Is it feedback on one of my quests? Which team did it come from?

Quest feedback is recognized structurally — the thread root is a quest the
agent authored — with no plan-specific local state. The quest-feedback
handler re-verifies ownership against the platform before acting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .platform_context_prompt import load_platform_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetProvenance:
    """What the agent knows about an event's source asset from the payload."""

    is_own_asset: bool = False
    team_id: Optional[str] = None
    root_asset_id: Optional[str] = None
    root_asset_type: Optional[str] = None

    @property
    def is_quest_feedback(self) -> bool:
        """Comment lands on a quest the agent authored — route to review."""
        return bool(
            self.is_own_asset
            and self.root_asset_id
            and self.root_asset_type == "quest"
        )


def _load_agent_user_id(workspace: Path) -> Optional[str]:
    ctx = load_platform_context(workspace)
    if not ctx:
        return None
    return (ctx.get("profile") or {}).get("id")


def _extract_event_team_id(event_data: Dict[str, Any]) -> Optional[str]:
    """Pull the team id from the enriched event payload."""
    team = event_data.get("team")
    if isinstance(team, dict) and "id" in team:
        return team["id"]
    return None


def _extract_root_asset(event_data: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Resolve the thread-root asset (id, type) from a webhook payload.

    The canonical payload nests the root as a structured ``root_asset`` object
    (``{"id": ..., "type": ...}``); the flat ``root_asset_id``/``root_asset_type``
    keys are the legacy form.  ``parent_asset`` is included as a last resort (it
    equals the root for top-level comments); a nested reply's parent is a
    comment id, which simply won't type-match a quest, so the fallback is safe.
    """
    root = event_data.get("root_asset")
    if isinstance(root, dict) and root.get("id"):
        return root["id"], root.get("type")
    if event_data.get("root_asset_id"):
        return event_data["root_asset_id"], event_data.get("root_asset_type")
    parent = event_data.get("parent_asset")
    if isinstance(parent, dict) and parent.get("id"):
        return parent["id"], parent.get("type")
    return event_data.get("parent_asset_id"), event_data.get("parent_asset_type")


def resolve_event_provenance(
    event_data: Dict[str, Any],
    workspace: Path,
) -> AssetProvenance:
    """Resolve provenance for an event using the payload and cached identity."""
    root_asset_id, root_asset_type = _extract_root_asset(event_data)
    team_id = _extract_event_team_id(event_data)
    if not root_asset_id:
        return AssetProvenance(team_id=team_id)

    is_own = False
    asset_author = event_data.get("source_user_id") or event_data.get("asset_user_id")
    if asset_author:
        agent_uid = _load_agent_user_id(workspace)
        if agent_uid and asset_author == agent_uid:
            is_own = True

    return AssetProvenance(
        is_own_asset=is_own,
        team_id=team_id,
        root_asset_id=str(root_asset_id),
        root_asset_type=str(root_asset_type) if root_asset_type else None,
    )
