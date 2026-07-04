"""Active context loader for entity files, task files, and recent daily logs.

Automatically detects and loads relevant workspace files based on conversation
state and the current request, so the agent doesn't have to manually read them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

from ..constants import CHARS_PER_TOKEN
from .conversation_state import ConversationState
from .naming import period_key_offset, store_rhythm

if TYPE_CHECKING:
    from . import DocStore

logger = logging.getLogger(__name__)
MAX_ENTITY_CONTEXT_TOKENS = 4000
MAX_TASK_CONTEXT_TOKENS = 2000
MAX_INDEX_FILES_PER_KIND = 30


def _slugify(name: str) -> str:
    """Convert an entity name to a likely file slug."""
    return name.lower().replace(" ", "-").replace("_", "-")


def _team_memory_dir(workspace: Path, team_id: str | None, leaf: str) -> Path:
    if team_id:
        return workspace / "teams" / team_id / "memory" / leaf
    return workspace / "memory" / leaf


def _file_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a memory file, or {} when absent/invalid."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        meta = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _file_slugs(path: Path, meta: dict) -> list[str]:
    """All slugs a file answers to: its stem plus frontmatter aliases."""
    slugs = [path.stem.lower()]
    aliases = meta.get("aliases")
    if isinstance(aliases, str):
        aliases = [aliases]
    if isinstance(aliases, list):
        slugs.extend(_slugify(str(a)) for a in aliases if str(a).strip())
    return slugs


def _find_entity_files(
    workspace: Path,
    key_entities: list[str],
    team_id: str | None = None,
) -> list[tuple[str, Path]]:
    """Match key_entities to files in memory/entities/ by stem or alias.

    Returns (entity_name, path) tuples preserving the original entity names.
    """
    entities_dir = _team_memory_dir(workspace, team_id, "entities")
    if not entities_dir.exists():
        return []

    available: dict[str, Path] = {}
    for path in sorted(entities_dir.glob("*.md")):
        for slug in _file_slugs(path, _file_frontmatter(path)):
            available.setdefault(slug, path)
    matched: list[tuple[str, Path]] = []

    for entity in key_entities:
        slug = _slugify(entity)
        if slug in available:
            matched.append((entity, available[slug]))
            continue
        for file_slug, path in available.items():
            if slug in file_slug or file_slug in slug:
                matched.append((entity, path))
                break

    return matched


def load_entity_files(
    workspace: Path,
    conversation_state: Optional[ConversationState] = None,
    max_tokens: int = 4000,
    team_id: str | None = None,
) -> str:
    """Load entity files matching conversation key_entities into a formatted string.

    Shared implementation used by the system prompt entity context, the
    context_loader subagent pipeline, and the reflector pipeline.
    """
    if not conversation_state or not conversation_state.key_entities:
        return ""

    matched = _find_entity_files(workspace, conversation_state.key_entities, team_id)
    if not matched:
        return ""

    parts: list[str] = []
    total_tokens = 0

    for entity_name, path in matched:
        remaining = max_tokens - total_tokens
        if remaining < 25:
            break
        content = _load_file_truncated(path, remaining)
        if content:
            parts.append(f"### {entity_name}\n{content}")
            total_tokens += len(content) // CHARS_PER_TOKEN

    return "\n\n".join(parts)


def _file_description(path: Path, meta: dict) -> str:
    """A one-line description: frontmatter `description`, else the first line."""
    desc = str(meta.get("description") or "").strip()
    if not desc:
        from .frontmatter import strip_frontmatter

        try:
            body = strip_frontmatter(path.read_text(errors="replace"))
        except OSError:
            return ""
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        desc = first.lstrip("# ").strip()
    return desc if len(desc) <= 120 else desc[:117].rstrip() + "..."


def build_memory_index(workspace: Path, team_id: str | None = None) -> str:
    """One line per entity/task file so the agent knows what memory files exist.

    Injected into the prompt alongside entity context: only files matching the
    conversation's key entities are auto-loaded, so this index is how the agent
    discovers the rest (readable with its file tools).
    """
    sections: list[str] = []
    for leaf, label in (("entities", "Entity files"), ("tasks", "Task files")):
        directory = _team_memory_dir(workspace, team_id, leaf)
        files = sorted(directory.glob("*.md")) if directory.exists() else []
        if not files:
            continue
        lines = [f"**{label}**"]
        for path in files[:MAX_INDEX_FILES_PER_KIND]:
            desc = _file_description(path, _file_frontmatter(path))
            rel = path.relative_to(workspace)
            lines.append(f"- `{rel}`" + (f" — {desc}" if desc else ""))
        if len(files) > MAX_INDEX_FILES_PER_KIND:
            lines.append(f"- (and {len(files) - MAX_INDEX_FILES_PER_KIND} more)")
        sections.append("\n".join(lines))
    if not sections:
        return ""
    return "### Memory files (read with file tools when relevant)\n" + "\n\n".join(
        sections
    )


def _find_active_task_files(workspace: Path, team_id: str | None = None) -> list[Path]:
    """Find task files that appear to be in-progress."""
    tasks_dir = _team_memory_dir(workspace, team_id, "tasks")
    if not tasks_dir.exists():
        return []

    active: list[Path] = []
    for p in tasks_dir.glob("*.md"):
        try:
            content = p.read_text(errors="replace")[:500].lower()
            if "in progress" in content or "in-progress" in content or "## next steps" in content:
                active.append(p)
        except Exception:
            continue
    return active


def _load_file_truncated(path: Path, max_tokens: int) -> str:
    """Load a file, truncating to a token budget."""
    try:
        content = path.read_text(errors="replace").strip()
        max_chars = max_tokens * CHARS_PER_TOKEN
        if len(content) > max_chars:
            content = content[:max_chars] + "\n[...truncated]"
        return content
    except Exception as e:
        logger.warning("Failed to load context file %s: %s", path, e)
        return ""


def _load_recent_daily_context(
    workspace: Path,
    doc_store: Optional["DocStore"] = None,
    agent_name: str = "",
) -> str:
    """Load the previous period's log if it exists (the current one is already
    in working memory). The window follows the doc store's rhythm."""
    if not doc_store:
        return ""

    rhythm = store_rhythm(doc_store)
    previous_period = period_key_offset(rhythm, -1)
    log_name = doc_store.log_name(agent_name, previous_period)
    content = doc_store.read(log_name)
    if not content:
        return ""

    label = {"weekly": "Last week", "biweekly": "Last period"}.get(rhythm, "Yesterday")
    max_chars = 300 * CHARS_PER_TOKEN
    if len(content) > max_chars:
        content = content[:max_chars] + "\n[...truncated]"
    return f"### {label} ({previous_period})\n{content}"


def load_entity_context(
    workspace: Path,
    conversation_state: Optional[ConversationState] = None,
    task: str = "",
    doc_store: Optional["DocStore"] = None,
    agent_name: str = "",
    team_id: str | None = None,
) -> str:
    """Load relevant entity files, task files, and recent daily context.

    Returns a formatted string for injection into the system prompt, or empty
    string if nothing relevant is found.
    """
    sections: list[str] = []
    total_tokens = 0

    # 1. Entity files matching conversation key_entities (always local)
    entity_text = load_entity_files(
        workspace,
        conversation_state,
        max_tokens=MAX_ENTITY_CONTEXT_TOKENS,
        team_id=team_id,
    )
    if entity_text:
        sections.append(entity_text)
        total_tokens += len(entity_text) // CHARS_PER_TOKEN

    # 2. Active task files (always local)
    task_budget_used = 0
    task_files = _find_active_task_files(workspace, team_id=team_id)
    if task_files:
        task_parts: list[str] = []
        for path in task_files[:2]:
            remaining = MAX_TASK_CONTEXT_TOKENS - task_budget_used
            if remaining < 100:
                break
            content = _load_file_truncated(path, min(remaining, 200))
            if content:
                task_parts.append(f"**{path.stem}**\n{content}")
                tokens = len(content) // CHARS_PER_TOKEN
                task_budget_used += tokens
                total_tokens += tokens
        if task_parts:
            sections.append("### Active Tasks\n" + "\n\n".join(task_parts))

    # 3. Yesterday's daily log for continuity (doc_store if available)
    daily_context = _load_recent_daily_context(
        workspace, doc_store=doc_store, agent_name=agent_name,
    )
    if daily_context:
        sections.append(daily_context)
        total_tokens += len(daily_context) // CHARS_PER_TOKEN

    # 4. Index of all memory files so unmatched ones remain discoverable
    index = build_memory_index(workspace, team_id=team_id)
    if index:
        sections.append(index)
        total_tokens += len(index) // CHARS_PER_TOKEN

    if not sections:
        return ""

    result = "\n\n".join(sections)
    logger.info("Loaded entity context: ~%d tokens from %d sections", total_tokens, len(sections))
    return result
