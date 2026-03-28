"""Format cached Ouro platform context for main agent and subagent prompts."""

from __future__ import annotations

import json
from pathlib import Path


def _description_text(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, dict):
        return str(raw.get("text", "") or "")
    return str(raw)


def _format_team_line(team: dict) -> str:
    desc = _description_text(team.get("description"))
    name = team.get("name", "?")
    tid = team.get("id", "?")
    oid = team.get("org_id", "?")
    org_name = team.get("organization_name", "?")
    role = team.get("role", "?")
    bits = [
        f"- {name}",
        f"team_id: {tid}",
        f"org_id: {oid}",
        f"org: {org_name}",
        f"role: {role}",
    ]
    acc = team.get("agent_can_create")
    if acc is not None:
        bits.append(f"agent_can_create: {acc}")
    line = ", ".join(bits)
    if desc:
        line += f" — {desc}"
    return line


def format_platform_context_for_prompt(workspace: Path) -> str:
    """Load ``data/platform_context.json`` and format for prompt injection.

    Matches the body text the main agent receives under ``## PLATFORM CONTEXT``
    (heading is added by the prompt builder).
    """
    cache_path = workspace / "data" / "platform_context.json"
    if not cache_path.exists():
        return ""

    try:
        context = json.loads(cache_path.read_text())
    except Exception:
        return ""

    parts: list[str] = []

    base_url = context.get("base_url")
    if base_url:
        parts.append(f"Platform Base URL: {base_url}")

    profile = context.get("profile")
    if profile:
        username = profile.get("username", "?")
        display = profile.get("display_name")
        name_str = f"{display} (@{username})" if display else f"@{username}"
        parts.append(
            f"You are: {name_str} (id: {profile.get('id', '?')}, "
            f"email: {profile.get('email', '?')})"
        )

    orgs = context.get("organizations", [])
    if orgs:
        parts.append("\nYour organizations:")
        for org in orgs:
            display = org.get("display_name") or org.get("name", "unknown")
            parts.append(
                f"- {display} (id: {org.get('id', '?')}, role: {org.get('role', '?')})"
            )

    teams = context.get("teams", [])
    if teams:
        parts.append("\nYour teams:")
        for team in teams:
            parts.append(_format_team_line(team))

    if not parts:
        return ""
    parts.append(
        "\nUse these IDs directly — no need to call get_organizations or get_teams "
        "unless you need to discover new teams or refresh membership info."
    )
    return "\n".join(parts)
