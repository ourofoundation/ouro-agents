"""Skill loading and resolution for the Ouro agent framework.

Skills are reusable knowledge/instruction fragments stored as markdown files.
Built-in skills ship with the package (this directory). Workspace skills
live in ``workspace/skills/`` and override built-in skills of the same name.

Two consumption patterns share the same underlying index:

1. **Main agent** — ``load_all_skills(config)`` builds a prompt section with
   full content for ``load: always`` skills and one-line stubs for the rest.

2. **Subagents** — ``resolve_skills(names, workspace)`` returns the body text
   for an explicit list of skill names referenced by a SubAgentProfile.
"""

import copy
import logging
from pathlib import Path
from typing import Optional

from ..config import OuroAgentsConfig

logger = logging.getLogger(__name__)

_BUILTIN_DIR = Path(__file__).parent

_index_cache: dict[str, "dict[str, SkillEntry]"] = {}


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split optional YAML frontmatter from markdown body.

    Returns (metadata_dict, body_text). If no frontmatter is found,
    metadata is empty and body is the full text.
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("---", 3)
    if end == -1:
        return {}, text

    raw = text[3:end].strip()
    meta: dict = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()

    body = text[end + 3 :].lstrip("\n")
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
        return self.meta.get("description", "")

    @property
    def load(self) -> str:
        return self.meta.get("load", "stub")


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
# Main-agent API (renders full index into prompt text)
# ---------------------------------------------------------------------------


def load_all_skills(config: OuroAgentsConfig) -> str:
    """Build prompt text: full content for ``load: always`` skills,
    one-line stubs for everything else."""
    index = _build_index(config.agent.workspace)
    return _render_skills(index)


def load_relevant_skills(
    config: OuroAgentsConfig,
    relevant_names: Optional[list[str]] = None,
) -> str:
    """Load skills matching the given names at full content,
    everything else as stubs."""
    index = _build_index(config.agent.workspace)
    working = copy.deepcopy(index)

    if relevant_names:
        for name in relevant_names:
            if name in working:
                working[name].meta["load"] = "always"

    return _render_skills(working)


def _render_skills(index: dict[str, SkillEntry]) -> str:
    """Render skills into prompt text respecting load behavior."""
    always_parts: list[str] = []
    stub_lines: list[str] = []

    for name, entry in index.items():
        if entry.load == "always":
            always_parts.append(entry.body)
        else:
            desc = entry.description or name
            stub_lines.append(f"- **{name}**: {desc}")

    sections: list[str] = []
    if always_parts:
        sections.append("\n\n---\n\n".join(always_parts))
    if stub_lines:
        header = "Available skills (read the full file with filesystem tools if needed):"
        sections.append(f"{header}\n" + "\n".join(stub_lines))

    return "\n\n---\n\n".join(sections)


def get_skill_directory(config: OuroAgentsConfig) -> str:
    """One-line-per-skill directory for system prompts."""
    index = _build_index(config.agent.workspace)
    lines = []
    for name, entry in index.items():
        desc = entry.description or entry.body.strip().split("\n")[0].lstrip("# ").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


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
