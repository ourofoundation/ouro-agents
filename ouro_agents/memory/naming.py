"""Pure naming logic for memory docs.

Logical doc keys, team qualifiers, and the rules that turn either into the
Ouro-facing post title. Kept separate from the I/O classes so callers can
import naming utilities without dragging in the doc store implementations.
"""

from __future__ import annotations

import re

_TEAM_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Identity prefixes are pinned to the local filesystem — they're per-machine
# state (agent identity, machine-local notes) and never persist to Ouro.
IDENTITY_PREFIXES: frozenset[str] = frozenset(
    {"SOUL", "HEARTBEAT", "NOTES", "SHARED"}
)

# Singleton prefixes name a single doc per (agent, team[, day]) tuple. The
# resolver refuses to pick a winner from ambiguous exact-name matches for
# these — duplicates indicate a real bug we want to surface.
SINGLETON_PREFIXES: frozenset[str] = frozenset(
    {"SOUL", "NOTES", "HEARTBEAT", "MEMORY", "DAILY", "USER", "SHARED"}
)


def slugify_team_key(value: str) -> str:
    """Normalize a team label for use in canonical doc names."""
    lowered = value.strip().lower()
    if not lowered:
        return ""
    return _TEAM_SLUG_RE.sub("-", lowered).strip("-")


def team_doc_key(
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Resolve the canonical team qualifier used in MEMORY/DAILY names."""
    for candidate in (team_slug, team_name, team_id):
        if not candidate:
            continue
        normalized = slugify_team_key(candidate)
        if normalized:
            return normalized
    return ""


def memory_doc_name(
    agent_name: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Build the canonical working-memory doc name."""
    qualifier = team_doc_key(team_slug=team_slug, team_name=team_name, team_id=team_id)
    if qualifier:
        return f"MEMORY:{agent_name}:{qualifier}"
    return f"MEMORY:{agent_name}"


def daily_doc_name(
    agent_name: str,
    day: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Build the canonical daily-log doc name."""
    qualifier = team_doc_key(team_slug=team_slug, team_name=team_name, team_id=team_id)
    if qualifier:
        return f"DAILY:{agent_name}:{qualifier}:{day}"
    return f"DAILY:{agent_name}:{day}"


def daily_doc_display_name(name: str) -> str:
    """Return the human-facing Ouro post title for a DAILY logical key."""
    parts = name.split(":")
    if not parts or parts[0] != "DAILY":
        return name

    if len(parts) >= 4:
        qualifier = team_doc_key(team_slug=parts[2])
        if qualifier:
            return f"#{qualifier} daily log {parts[-1]}"
    if len(parts) >= 3:
        agent = parts[1].strip()
        owner = f"{agent} " if agent else ""
        return f"{owner}daily log {parts[-1]}"
    return name


def remote_display_name(name: str) -> str:
    """Map an internal doc key to the title shown on Ouro."""
    if name.split(":", 1)[0] == "DAILY":
        return daily_doc_display_name(name)
    return name


def is_singleton_name(name: str) -> bool:
    """Return True for registry-first named memory docs."""
    return name.split(":", 1)[0] in SINGLETON_PREFIXES


def is_identity_name(name: str) -> bool:
    """Return True for docs that always live on the local filesystem."""
    return name.split(":", 1)[0] in IDENTITY_PREFIXES
