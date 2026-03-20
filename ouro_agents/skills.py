from pathlib import Path
from typing import Optional

from .config import OuroAgentsConfig


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body.

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

    body = text[end + 3:].lstrip("\n")
    return meta, body


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


def _build_skill_index(config: OuroAgentsConfig) -> dict[str, SkillEntry]:
    """Build a name -> SkillEntry mapping of all available skills."""
    index: dict[str, SkillEntry] = {}

    skills_dir = config.agent.workspace / "skills"
    if skills_dir.exists():
        for f in sorted(skills_dir.glob("*.md")):
            index[f.stem] = SkillEntry(f.stem, f.read_text())

    return index


def load_all_skills(config: OuroAgentsConfig) -> str:
    """Build prompt text: full content for `load: always` skills,
    one-line stubs for everything else."""
    index = _build_skill_index(config)
    return _render_skills(index)


def load_relevant_skills(
    config: OuroAgentsConfig,
    relevant_names: Optional[list[str]] = None,
) -> str:
    """Load skills matching the given names at full content,
    everything else as stubs."""
    index = _build_skill_index(config)

    if relevant_names:
        for name in relevant_names:
            if name in index:
                index[name].meta["load"] = "always"

    return _render_skills(index)


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
    index = _build_skill_index(config)
    lines = []
    for name, entry in index.items():
        desc = entry.description or entry.body.strip().split("\n")[0].lstrip("# ").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)
