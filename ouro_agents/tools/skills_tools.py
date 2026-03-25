"""Skill-loading tool factory for on-demand prompt guidance."""

from __future__ import annotations

from pathlib import Path

from smolagents import tool

from ..skills import list_skill_names, resolve_skill


def make_load_skill_tool(workspace: Path):
    """Create a load_skill tool backed by the workspace skill registry."""

    available_names = list_skill_names(workspace, include_always=False)

    @tool
    def load_skill(skill_names: list) -> str:
        """Load one or more skills into the current run context.

        Use this when you need detailed guidance from a skill in the available
        skills directory. The returned skill text becomes part of the current
        run's context for subsequent steps.

        Args:
            skill_names: List of skill names to load.

        Example single: ["python"]
        Example multi: ["python", "filesystem", "web-search"]
        """
        if not skill_names:
            return "No skill names provided."

        sections: list[str] = []
        missing: list[str] = []
        seen: set[str] = set()

        for raw_name in skill_names:
            name = str(raw_name).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            content = resolve_skill(name, workspace=workspace)
            if content is None:
                missing.append(name)
                continue
            sections.append(f"## Skill: {name}\n{content}")

        if missing:
            available = ", ".join(available_names[:12]) or "(none)"
            sections.append(
                "## Missing Skills\n"
                f"Unknown skill names: {', '.join(missing)}\n"
                f"Available skills: {available}"
            )

        if not sections:
            return "No skills were loaded."

        return "\n\n---\n\n".join(sections)

    return load_skill
