"""Skill loading and resolution for the Ouro agent framework.

Skills are reusable knowledge/instruction fragments stored as markdown files
with optional YAML frontmatter. Built-in skills ship with the package (this
directory). Workspace skills live in ``workspace/skills/`` and override
built-in skills of the same name.

An agent-authored skill may declare ``extends: <parent>`` to attach as an
addendum. Valid addenda inherit the parent's load behavior and are rendered
after the parent body (parent wins on conflict). See ``docs/skills.md``.

Two consumption patterns share the same underlying index:

1. **Main agent** — ``load_startup_skills(config)`` inlines the body of every
   skill tagged ``load: always`` into the system prompt (plus any attached
   addenda). ``get_skill_directory(config)`` produces a one-line-per-skill
   listing so the agent knows what else is available to read on demand.

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

# Soft cap on total addendum body characters per parent (across all children).
_ADDENDUM_CHAR_CAP = 8000
_ADDENDUM_TRUNCATION_MARKER = "[addendum truncated — compact this file]"


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

    @property
    def extends(self) -> str:
        """Parent skill name when this entry is an addendum, else ``""``."""
        return str(self.meta.get("extends", "") or "").strip()


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
# Addenda (extends:)
# ---------------------------------------------------------------------------


def _attach_children(
    index: dict[str, SkillEntry],
) -> tuple[dict[str, list[SkillEntry]], set[str]]:
    """Resolve valid ``extends:`` children from an index.

    Returns ``(children_by_parent, valid_child_names)``. Invalid ``extends``
    declarations are logged and left as standalone skills.
    """
    children_by_parent: dict[str, list[SkillEntry]] = {}
    valid_child_names: set[str] = set()

    for name, entry in sorted(index.items()):
        parent_name = entry.extends
        if not parent_name:
            continue

        expected_stem = f"{parent_name}-addendum"
        if name != expected_stem:
            logger.warning(
                "Skill '%s' extends '%s' but is not named '%s'; "
                "honoring the extends link anyway",
                name,
                parent_name,
                expected_stem,
            )

        parent = index.get(parent_name)
        if parent is None:
            logger.warning(
                "Skill '%s' extends unknown parent '%s'; treating as standalone",
                name,
                parent_name,
            )
            continue

        if parent.extends:
            logger.warning(
                "Skill '%s' extends '%s' which itself has extends='%s' "
                "(no chaining); treating as standalone",
                name,
                parent_name,
                parent.extends,
            )
            continue

        children_by_parent.setdefault(parent_name, []).append(entry)
        valid_child_names.add(name)

    for parent_name, children in children_by_parent.items():
        children.sort(key=lambda e: e.name)
        if len(children) > 1:
            logger.warning(
                "Multiple addenda extend '%s' (%s); appending in name order",
                parent_name,
                ", ".join(c.name for c in children),
            )

    return children_by_parent, valid_child_names


def _render_with_addenda(
    parent: SkillEntry, children: list[SkillEntry]
) -> str:
    """Render parent body followed by labeled, capped addendum sections."""
    parent_body = parent.body.strip()
    if not children:
        return parent_body

    parts = [parent_body]
    remaining = _ADDENDUM_CHAR_CAP

    for child in children:
        body = child.body.strip()
        header = (
            f'## Agent addendum: {child.name}\n'
            f'(Extends the "{parent.name}" skill. Where this conflicts with '
            f"the skill above, the skill above wins.)\n\n"
        )
        if remaining <= 0:
            parts.append(header + _ADDENDUM_TRUNCATION_MARKER)
            continue

        if len(body) > remaining:
            body = body[:remaining] + "\n" + _ADDENDUM_TRUNCATION_MARKER
            remaining = 0
        else:
            remaining -= len(body)

        parts.append(header + body)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main-agent API
# ---------------------------------------------------------------------------


def invalidate_skill_cache(workspace: Optional[Path] = None) -> None:
    """Drop the cached skill index after workspace skill files change."""
    _index_cache.pop(str(workspace) if workspace else "__builtins_only__", None)


def load_startup_skills(config: OuroAgentsConfig) -> str:
    """Build prompt text for skills tagged ``load: always``.

    Valid addenda ride with their parent (a child's own ``load:`` is ignored).
    Everything else stays stub-only; the agent can pull a specific skill on
    demand via :func:`resolve_skill` / the ``load_skill`` tool.
    """
    index = _build_index(config.agent.workspace)
    children_by_parent, valid_child_names = _attach_children(index)

    always_parts: list[str] = []
    for name, entry in index.items():
        if name in valid_child_names:
            continue
        if entry.load != "always":
            continue
        always_parts.append(
            _render_with_addenda(entry, children_by_parent.get(name, []))
        )
    return "\n\n---\n\n".join(always_parts)


def get_skill_directory(
    config: OuroAgentsConfig, *, include_always: bool = False
) -> str:
    """One-line-per-skill directory for system prompts.

    Valid addenda are excluded — loading the parent brings the addendum.
    """
    index = _build_index(config.agent.workspace)
    _, valid_child_names = _attach_children(index)
    lines = []
    for name, entry in index.items():
        if name in valid_child_names:
            continue
        if entry.load == "always" and not include_always:
            continue
        desc = entry.description or entry.body.strip().split("\n")[0].lstrip("# ").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def list_skill_names(
    workspace: Optional[Path] = None, *, include_always: bool = True
) -> list[str]:
    """Return available skill names for a workspace.

    Valid addenda are excluded (they are not independently loadable targets).
    """
    index = _build_index(workspace)
    _, valid_child_names = _attach_children(index)
    return sorted(
        name
        for name, entry in index.items()
        if name not in valid_child_names
        and (include_always or entry.load != "always")
    )


# ---------------------------------------------------------------------------
# Subagent API (explicit names → body text)
# ---------------------------------------------------------------------------


def resolve_skill(name: str, workspace: Optional[Path] = None) -> Optional[str]:
    """Resolve a single skill name to its body text, or None if not found.

    Parents with addenda return parent + addenda. Valid children return their
    own body as-is (still directly resolvable for debugging).
    """
    index = _build_index(workspace)
    entry = index.get(name)
    if entry is None:
        logger.warning("Skill '%s' not found in workspace or builtins", name)
        return None

    children_by_parent, valid_child_names = _attach_children(index)
    if name in valid_child_names:
        return entry.body.strip()
    return _render_with_addenda(entry, children_by_parent.get(name, []))


def resolve_skills(
    names: list[str], workspace: Optional[Path] = None
) -> list[str]:
    """Resolve a list of skill names to their body text.

    Skips any skills that can't be found. Returns content in input order.
    Parents include attached addenda (same as :func:`resolve_skill`).
    """
    sections: list[str] = []
    for name in names:
        content = resolve_skill(name, workspace=workspace)
        if content is not None:
            sections.append(content)
    return sections


def list_builtin_skills() -> list[str]:
    """Return names of all available built-in skills."""
    return sorted(p.stem for p in _BUILTIN_DIR.glob("*.md"))
