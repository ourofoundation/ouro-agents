"""Team discovery and context — runtime team awareness.

Teams are discovered from the platform context cache (populated by
``_refresh_platform_context``).  ``TeamRegistry`` holds the snapshot;
``TeamContext`` is the lightweight pair passed into subsystems for a
single operation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamContext:
    """Attribution scope for a single agent run or reflection cycle."""

    team_id: str
    org_id: str


@dataclass
class TeamInfo:
    """Cached metadata for a team the agent belongs to."""

    id: str
    name: str
    org_id: str
    slug: str = ""


class TeamRegistry:
    """In-memory registry of teams the agent belongs to.

    Rebuilt on every ``_refresh_platform_context`` call (startup + heartbeat).
    Provides fast lookups used by doc stores, plan stores, and provenance.
    """

    def __init__(self) -> None:
        self._teams: dict[str, TeamInfo] = {}

    def refresh(self, platform_context: dict, org_id: str | None = None) -> None:
        """Rebuild the registry from a platform_context dict.

        When *org_id* is set, only teams belonging to that org are kept.
        """
        self._teams.clear()
        for team in platform_context.get("teams", []):
            tid = team.get("id")
            if not tid:
                continue
            team_org = (
                team.get("organization_id")
                or team.get("org_id")
                or (team.get("organization") or {}).get("id")
            )
            if org_id and team_org and team_org != org_id:
                continue
            self._teams[tid] = TeamInfo(
                id=tid,
                name=team.get("name", ""),
                org_id=team_org or "",
                slug=team.get("slug", team.get("name", "")),
            )

    def get_team(self, team_id: str) -> Optional[TeamInfo]:
        return self._teams.get(team_id)

    def list_teams(self) -> list[TeamInfo]:
        return list(self._teams.values())

    def team_ids(self) -> set[str]:
        return set(self._teams.keys())

    def team_name(self, team_id: str) -> str:
        """Human-readable team name, falling back to the raw id."""
        info = self._teams.get(team_id)
        return info.name if info else team_id

    def context_for(self, team_id: str, org_id: str) -> TeamContext:
        """Build a TeamContext for a known team."""
        return TeamContext(team_id=team_id, org_id=org_id)

    @classmethod
    def from_platform_context(
        cls,
        workspace: Path,
        org_id: str | None = None,
    ) -> TeamRegistry:
        """Build a registry from the cached ``platform_context.json``."""
        registry = cls()
        cache_path = workspace / "data" / "platform_context.json"
        if cache_path.exists():
            try:
                ctx = json.loads(cache_path.read_text())
                registry.refresh(ctx, org_id)
            except Exception as exc:
                logger.warning("Failed to load team registry from cache: %s", exc)
        return registry
