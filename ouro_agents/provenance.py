"""Event provenance — resolves whether an event relates to the agent's own work.

When a webhook event arrives (e.g. a comment), this module checks local state
to determine: is this about something I created? Is it in my planning space?
Is it on a specific plan post? It primarily uses local state, with optional
comment-parent resolution supplied by the caller for threaded comment events.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

AssetResolver = Callable[[str], tuple[Optional[str], Optional[str]]]


@dataclass(frozen=True)
class PlanCycleRef:
    """Reference to a plan cycle matched by source_id."""

    cycle_id: str
    status: str  # "planning" | "pending_review" | "active" | "completed"
    plan_text: str = ""
    post_id: Optional[str] = None


@dataclass(frozen=True)
class AssetProvenance:
    """What the agent knows about an event's source asset from local state."""

    is_own_asset: bool = False
    in_planning_space: bool = False
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


def resolve_event_focus_asset(
    source_id: Optional[str],
    event_data: Dict[str, Any],
    resolve_comment_parent: Optional[AssetResolver] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Return the root asset an event is effectively about.

    For direct comments on an asset, this is the webhook ``target_id``.
    For replies in a comment thread, this optionally resolves the target comment
    up to its root non-comment parent so plan-post replies can still map back to
    the original plan post.
    """
    focus_id = event_data.get("focus_asset_id")
    focus_type = event_data.get("focus_asset_type")
    if focus_id:
        return focus_id, focus_type

    target_id = event_data.get("target_id")
    target_type = event_data.get("target_asset_type")
    if target_id:
        if target_type == "comment" and resolve_comment_parent:
            resolved_id, resolved_type = resolve_comment_parent(target_id)
            if resolved_id:
                return resolved_id, resolved_type
        return target_id, target_type

    return source_id, event_data.get("source_asset_type")


def resolve_event_provenance(
    source_id: Optional[str],
    event_data: Dict[str, Any],
    workspace: Path,
    planning_team_id: Optional[str] = None,
    planning_org_id: Optional[str] = None,
    planning_enabled: bool = False,
    resolve_comment_parent: Optional[AssetResolver] = None,
) -> AssetProvenance:
    """Resolve provenance for an event using local state and optional comment resolution."""
    focus_asset_id, _focus_asset_type = resolve_event_focus_asset(
        source_id,
        event_data,
        resolve_comment_parent=resolve_comment_parent,
    )

    if not focus_asset_id:
        return AssetProvenance()

    is_own = False
    in_planning_space = False
    plan_cycle: Optional[PlanCycleRef] = None

    # Identity match: does the event say who authored the source asset?
    asset_author = event_data.get("source_user_id") or event_data.get("asset_user_id")
    if asset_author:
        agent_uid = _load_agent_user_id(workspace)
        if agent_uid and asset_author == agent_uid:
            is_own = True

    # Team/org match: is the event in the agent's planning space?
    if planning_enabled:
        event_team = event_data.get("team_id")
        event_org = event_data.get("org_id") or event_data.get("organization_id")
        if planning_team_id and event_team == planning_team_id:
            in_planning_space = True
        elif planning_org_id and event_org == planning_org_id and not planning_team_id:
            in_planning_space = True

    # Plan store match: is the effective event asset a known plan post?
    if planning_enabled:
        from .modes.planning import PlanStore

        store = PlanStore(workspace / "plans")

        for active in store.load_all_active():
            if active.post_id == focus_asset_id:
                is_own = True
                in_planning_space = True
                plan_cycle = PlanCycleRef(
                    cycle_id=active.id,
                    status=active.status,
                    plan_text=active.plan_text,
                    post_id=active.post_id,
                )
                break

        if not plan_cycle:
            for hist in store.load_history():
                if hist.post_id == focus_asset_id:
                    is_own = True
                    in_planning_space = True
                    plan_cycle = PlanCycleRef(
                        cycle_id=hist.id,
                        status=hist.status,
                        plan_text=hist.plan_text,
                        post_id=hist.post_id,
                    )
                    break

    return AssetProvenance(
        is_own_asset=is_own,
        in_planning_space=in_planning_space,
        plan_cycle=plan_cycle,
    )
