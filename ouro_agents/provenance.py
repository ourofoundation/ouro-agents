"""Event provenance — resolves whether an event relates to the agent's own work.

When a webhook event arrives (e.g. a comment), this module checks local state
to determine: is this about something I created? Does it match a known plan
cycle? Which team did it come from?
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanCycleRef:
    """Reference to a plan cycle matched by source_id."""

    cycle_id: str
    status: str  # "planning" | "pending_review" | "active" | "completed"
    plan_text: str = ""
    quest_id: Optional[str] = None


@dataclass(frozen=True)
class AssetProvenance:
    """What the agent knows about an event's source asset from local state."""

    is_own_asset: bool = False
    team_id: Optional[str] = None
    plan_cycle: Optional[PlanCycleRef] = None

    @property
    def is_plan_feedback(self) -> bool:
        return (
            self.plan_cycle is not None
            and self.plan_cycle.status in ("pending_review", "active")
        )

    @property
    def is_historical_plan_feedback(self) -> bool:
        return (
            self.plan_cycle is not None
            and self.plan_cycle.status == "completed"
        )


def _load_agent_user_id(workspace: Path) -> Optional[str]:
    cache_path = workspace / "data" / "platform_context.json"
    if not cache_path.exists():
        return None
    try:
        ctx = json.loads(cache_path.read_text())
        return (ctx.get("profile") or {}).get("id")
    except Exception:
        return None


def _extract_event_team_id(event_data: Dict[str, Any]) -> Optional[str]:
    """Pull the team id from the enriched event payload."""
    team = event_data.get("team")
    if isinstance(team, dict) and "id" in team:
        return team["id"]
    return None


def _extract_root_asset_id(event_data: Dict[str, Any]) -> Optional[str]:
    """Resolve the thread-root asset id from a webhook payload.

    The canonical payload nests the root as a structured ``root_asset`` object
    (``{"id": ..., "type": ...}``); the flat ``root_asset_id`` key is only the
    legacy form.  Mirror the SDK / ``CommentContext`` fallback chain so plan
    feedback on quests is recognized regardless of payload shape.  ``parent_asset``
    is included as a last resort (it equals the root for top-level comments); a
    nested reply's parent is a comment id, which simply won't match any plan
    quest, so the fallback is safe.
    """
    root = event_data.get("root_asset")
    if isinstance(root, dict) and root.get("id"):
        return root["id"]
    if event_data.get("root_asset_id"):
        return event_data["root_asset_id"]
    parent = event_data.get("parent_asset")
    if isinstance(parent, dict) and parent.get("id"):
        return parent["id"]
    return event_data.get("parent_asset_id")


def resolve_event_provenance(
    event_data: Dict[str, Any],
    workspace: Path,
    planning_enabled: bool = False,
) -> AssetProvenance:
    """Resolve provenance for an event using local state.

    Reads ``root_asset_id`` and ``team`` directly from the enriched event
    payload.  Searches team-namespaced plan stores for cycle matches.
    """
    root_asset_id = _extract_root_asset_id(event_data)
    if not root_asset_id:
        team_id = _extract_event_team_id(event_data)
        return AssetProvenance(team_id=team_id)

    is_own = False
    plan_cycle: Optional[PlanCycleRef] = None
    team_id = _extract_event_team_id(event_data)

    asset_author = event_data.get("source_user_id") or event_data.get("asset_user_id")
    if asset_author:
        agent_uid = _load_agent_user_id(workspace)
        if agent_uid and asset_author == agent_uid:
            is_own = True

    if planning_enabled:
        from .modes.planning import PlanStore

        def _search_stores() -> Optional[PlanCycleRef]:
            """Search team plan stores for a matching quest."""
            teams_dir = workspace / "teams"
            search_targets: list[tuple[Path, Optional[str]]] = []
            if team_id and (teams_dir / team_id / "plans").exists():
                search_targets.append((teams_dir / team_id / "plans", team_id))
            elif teams_dir.exists():
                for child in teams_dir.iterdir():
                    plans_path = child / "plans"
                    if plans_path.exists():
                        search_targets.append((plans_path, child.name))

            for plans_path, tid in search_targets:
                store = PlanStore(plans_path, team_id=tid)
                for active in store.load_all_active():
                    if active.quest_id == root_asset_id:
                        return PlanCycleRef(
                            cycle_id=active.id,
                            status=active.status,
                            plan_text=active.plan_text,
                            quest_id=active.quest_id,
                        )
                for hist in store.load_history():
                    if hist.quest_id == root_asset_id:
                        return PlanCycleRef(
                            cycle_id=hist.id,
                            status=hist.status,
                            plan_text=hist.plan_text,
                            quest_id=hist.quest_id,
                        )
            return None

        plan_cycle = _search_stores()
        if plan_cycle:
            is_own = True

    return AssetProvenance(
        is_own_asset=is_own,
        team_id=team_id,
        plan_cycle=plan_cycle,
    )
