"""Skill loading and resolution for the Ouro agent framework.

Skills are reusable knowledge/instruction fragments stored as markdown files
with optional YAML frontmatter. Built-in skills ship with the package (this
directory). Workspace skills live in ``workspace/skills/`` and override
built-in skills of the same name.

Two consumption patterns share the same underlying index:

1. **Main agent** — ``load_startup_skills(config)`` inlines the body of every
   skill tagged ``load: always`` into the system prompt.
   ``get_skill_directory(config)`` produces a one-line-per-skill listing so
   the agent knows what else is available to read on demand.

2. **Subagents** — ``resolve_skills(names, workspace)`` returns the body text
   for an explicit list of skill names referenced by a SubAgentProfile.
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

from ..config import OuroAgentsConfig

logger = logging.getLogger(__name__)

_BUILTIN_DIR = Path(__file__).parent

_index_cache: dict[str, "dict[str, SkillEntry]"] = {}


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split optional YAML frontmatter from markdown body.

    Returns ``(metadata_dict, body_text)``. If no frontmatter is found or the
    block fails to parse as YAML, metadata is empty and body is the full text.
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    raw = text[3:end].strip()
    try:
        parsed = yaml.safe_load(raw) if raw else {}
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse skill frontmatter as YAML: %s", exc)
        return {}, text

    meta = parsed if isinstance(parsed, dict) else {}
    body = text[end + 4 :].lstrip("\n")
    return meta, body


# ---------------------------------------------------------------------------
# SkillEntry
# ---------------------------------------------------------------------------


class SkillEntry:
    """A single skill file with parsed frontmatter."""

    __slots__ = ("name", "meta", "body", "full_text")

    def __init__(self, name: str, full_text: str):
        self.name = name
        self.full_text = full_text
        self.meta, self.body = _parse_frontmatter(full_text)

    @property
    def description(self) -> str:
        return str(self.meta.get("description", "") or "")

    @property
    def load(self) -> str:
        return str(self.meta.get("load", "stub") or "stub")


# ---------------------------------------------------------------------------
# Core index (shared by main agent and subagents)
# ---------------------------------------------------------------------------


def _build_index(workspace: Optional[Path] = None) -> dict[str, SkillEntry]:
    """Build a name → SkillEntry mapping, merging built-in + workspace.

    Workspace skills override built-in skills of the same name.
    Cached per workspace path.
    """
    cache_key = str(workspace) if workspace else "__builtins_only__"
    if cache_key in _index_cache:
        return _index_cache[cache_key]

    index: dict[str, SkillEntry] = {}

    for f in sorted(_BUILTIN_DIR.glob("*.md")):
        index[f.stem] = SkillEntry(f.stem, f.read_text())

    if workspace:
        ws_dir = workspace / "skills"
        if ws_dir.exists():
            for f in sorted(ws_dir.glob("*.md")):
                index[f.stem] = SkillEntry(f.stem, f.read_text())

    _index_cache[cache_key] = index
    return index


# ---------------------------------------------------------------------------
# Main-agent API
# ---------------------------------------------------------------------------


def invalidate_skill_cache(workspace: Optional[Path] = None) -> None:
    """Drop the cached skill index after workspace skill files change."""
    _index_cache.pop(str(workspace) if workspace else "__builtins_only__", None)


def load_startup_skills(config: OuroAgentsConfig) -> str:
    """Build prompt text for skills tagged ``load: always``.

    Everything else stays stub-only; the agent can pull a specific skill on
    demand via :func:`resolve_skill` / the ``load_skill`` tool.
    """
    index = _build_index(config.agent.workspace)
    always_parts = [entry.body for entry in index.values() if entry.load == "always"]
    return "\n\n---\n\n".join(always_parts)


def get_skill_directory(
    config: OuroAgentsConfig, *, include_always: bool = False
) -> str:
    """One-line-per-skill directory for system prompts."""
    index = _build_index(config.agent.workspace)
    lines = []
    for name, entry in index.items():
        if entry.load == "always" and not include_always:
            continue
        desc = entry.description or entry.body.strip().split("\n")[0].lstrip("# ").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def list_skill_names(
    workspace: Optional[Path] = None, *, include_always: bool = True
) -> list[str]:
    """Return available skill names for a workspace."""
    index = _build_index(workspace)
    return sorted(
        name
        for name, entry in index.items()
        if include_always or entry.load != "always"
    )


# ---------------------------------------------------------------------------
# Subagent API (explicit names → body text)
# ---------------------------------------------------------------------------


def resolve_skill(name: str, workspace: Optional[Path] = None) -> Optional[str]:
    """Resolve a single skill name to its body text, or None if not found."""
    index = _build_index(workspace)
    entry = index.get(name)
    if entry:
        return entry.body.strip()
    logger.warning("Skill '%s' not found in workspace or builtins", name)
    return None


def resolve_skills(
    names: list[str], workspace: Optional[Path] = None
) -> list[str]:
    """Resolve a list of skill names to their body text.

    Skips any skills that can't be found. Returns content in input order.
    """
    index = _build_index(workspace)
    sections: list[str] = []
    for name in names:
        entry = index.get(name)
        if entry:
            sections.append(entry.body.strip())
        else:
            logger.warning("Skill '%s' not found in workspace or builtins", name)
    return sections


def list_builtin_skills() -> list[str]:
    """Return names of all available built-in skills."""
    return sorted(p.stem for p in _BUILTIN_DIR.glob("*.md"))
