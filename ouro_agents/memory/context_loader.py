"""Active context loader for entity files, task files, and recent daily logs.

Automatically detects and loads relevant workspace files based on conversation
state and the current request, so the agent doesn't have to manually read them.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .conversation_state import ConversationState

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
MAX_ENTITY_CONTEXT_TOKENS = 600
MAX_TASK_CONTEXT_TOKENS = 400


def _slugify(name: str) -> str:
    """Convert an entity name to a likely file slug."""
    return name.lower().replace(" ", "-").replace("_", "-")


def _find_entity_files(workspace: Path, key_entities: list[str]) -> list[Path]:
    """Match key_entities to files in memory/entities/."""
    entities_dir = workspace / "memory" / "entities"
    if not entities_dir.exists():
        return []

    available = {p.stem.lower(): p for p in entities_dir.glob("*.md")}
    matched: list[Path] = []

    for entity in key_entities:
        slug = _slugify(entity)
        if slug in available:
            matched.append(available[slug])
            continue
        for file_slug, path in available.items():
            if slug in file_slug or file_slug in slug:
                matched.append(path)
                break

    return matched


def _find_active_task_files(workspace: Path) -> list[Path]:
    """Find task files that appear to be in-progress."""
    tasks_dir = workspace / "memory" / "tasks"
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


def _load_recent_daily_context(workspace: Path) -> str:
    """Load yesterday's daily log if it exists (today's is already in working memory)."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    daily_path = workspace / "memory" / "daily" / f"{yesterday}.md"
    if not daily_path.exists():
        return ""
    content = _load_file_truncated(daily_path, 300)
    if content:
        return f"### Yesterday ({yesterday})\n{content}"
    return ""


def load_entity_context(
    workspace: Path,
    conversation_state: Optional[ConversationState] = None,
    task: str = "",
) -> str:
    """Load relevant entity files, task files, and recent daily context.

    Returns a formatted string for injection into the system prompt, or empty
    string if nothing relevant is found.
    """
    sections: list[str] = []
    total_tokens = 0

    # 1. Entity files matching conversation key_entities
    if conversation_state and conversation_state.key_entities:
        entity_files = _find_entity_files(workspace, conversation_state.key_entities)
        entity_parts: list[str] = []
        for path in entity_files:
            remaining = MAX_ENTITY_CONTEXT_TOKENS - total_tokens
            if remaining < 100:
                break
            content = _load_file_truncated(path, remaining)
            if content:
                entity_parts.append(content)
                total_tokens += len(content) // CHARS_PER_TOKEN
        if entity_parts:
            sections.append("### Entities\n" + "\n\n---\n\n".join(entity_parts))

    # 2. Active task files
    task_files = _find_active_task_files(workspace)
    if task_files:
        task_parts: list[str] = []
        for path in task_files[:2]:
            remaining = MAX_TASK_CONTEXT_TOKENS - (total_tokens - MAX_ENTITY_CONTEXT_TOKENS)
            if remaining < 100:
                break
            content = _load_file_truncated(path, min(remaining, 200))
            if content:
                task_parts.append(f"**{path.stem}**\n{content}")
                total_tokens += len(content) // CHARS_PER_TOKEN
        if task_parts:
            sections.append("### Active Tasks\n" + "\n\n".join(task_parts))

    # 3. Yesterday's daily log for continuity
    daily_context = _load_recent_daily_context(workspace)
    if daily_context:
        sections.append(daily_context)
        total_tokens += len(daily_context) // CHARS_PER_TOKEN

    if not sections:
        return ""

    result = "\n\n".join(sections)
    logger.info("Loaded entity context: ~%d tokens from %d sections", total_tokens, len(sections))
    return result
