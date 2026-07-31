"""Active context loader for entity files, task files, and recent daily logs.

Automatically detects and loads relevant workspace files based on the current
request text, so the agent doesn't have to manually read them.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

from ..constants import CHARS_PER_TOKEN
from .naming import period_key_offset, store_rhythm

if TYPE_CHECKING:
    from . import DocStore

logger = logging.getLogger(__name__)
MAX_ENTITY_CONTEXT_TOKENS = 4000
MAX_TASK_CONTEXT_TOKENS = 2000
MAX_INDEX_FILES_PER_KIND = 30
_WORD_RE = re.compile(r"[a-z0-9]+")


def _slugify(name: str) -> str:
    """Convert an entity name to a likely file slug."""
    return name.lower().replace(" ", "-").replace("_", "-")


def _team_memory_dir(workspace: Path, team_id: str | None, leaf: str) -> Path:
    if team_id:
        return workspace / "teams" / team_id / "memory" / leaf
    from ..tools.workspace_paths import protected_memory

    return protected_memory(workspace) / leaf


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


def _slug_in_text(slug: str, text_lower: str) -> bool:
    """True when slug or its space-form appears in text_lower."""
    if not slug:
        return False
    if slug in text_lower:
        return True
    spaced = slug.replace("-", " ").replace("_", " ")
    return bool(spaced) and spaced in text_lower


def _haystack_run_slugs(text_lower: str, *, max_words: int = 6) -> set[str]:
    """Slugified forms of contiguous word-runs in *text_lower*."""
    words = _WORD_RE.findall(text_lower)
    runs: set[str] = set()
    for i in range(len(words)):
        parts: list[str] = []
        for j in range(i, min(i + max_words, len(words))):
            parts.append(words[j])
            runs.add("-".join(parts))
    return runs


def _find_entity_files(
    workspace: Path,
    haystack: str,
    team_id: str | None = None,
) -> list[tuple[str, Path]]:
    """Match entity files whose slug or aliases appear in *haystack*.

    Returns (display_name, path) tuples deduplicated by path.
    """
    entities_dir = _team_memory_dir(workspace, team_id, "entities")
    if not entities_dir.exists() or not haystack.strip():
        return []

    text_lower = haystack.lower()
    run_slugs = _haystack_run_slugs(text_lower)
    matched: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    for path in sorted(entities_dir.glob("*.md")):
        slugs = _file_slugs(path, _file_frontmatter(path))
        if not any(_slug_in_text(slug, text_lower) or slug in run_slugs for slug in slugs):
            continue
        if path in seen:
            continue
        seen.add(path)
        matched.append((path.stem, path))

    return matched


def load_entity_files(
    workspace: Path,
    haystack: str = "",
    max_tokens: int = 4000,
    team_id: str | None = None,
) -> str:
    """Load entity files whose names appear in *haystack* into a formatted string.

    Shared implementation used by the system prompt entity context, the
    context_loader subagent pipeline, and the reflector pipeline.
    """
    if not haystack.strip():
        return ""

    matched = _find_entity_files(workspace, haystack, team_id)
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

    Injected into the prompt alongside entity context: only files whose names
    appear in the current request text are auto-loaded, so this index is how
    the agent discovers the rest (readable with its file tools).
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
    haystack: str = "",
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

    # 1. Entity files matching haystack text (always local)
    entity_text = load_entity_files(
        workspace,
        haystack or task,
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


# ---------------------------------------------------------------------------
# Heartbeat helpers: cross-team index + path-confined reads
# ---------------------------------------------------------------------------

MAX_CROSS_TEAM_INDEX_LINES = 40
MAX_READ_CONTEXT_PATHS = 4
MAX_READ_CONTEXT_TOKENS_PER_FILE = 800


def build_cross_team_task_index(
    workspace: Path,
    *,
    team_labels: dict[str, str] | None = None,
    limit: int = MAX_CROSS_TEAM_INDEX_LINES,
) -> str:
    """One-line index of active task files across ``teams/*/memory/tasks``.

    Used by open-ended heartbeat ticks so the agent can pick a path to
    ``read_context`` instead of guessing team IDs.
    """
    teams_root = workspace / "teams"
    if not teams_root.is_dir():
        return ""

    labels = team_labels or {}
    lines: list[str] = []
    for team_dir in sorted(teams_root.iterdir()):
        if not team_dir.is_dir():
            continue
        team_id = team_dir.name
        tasks_dir = team_dir / "memory" / "tasks"
        if not tasks_dir.is_dir():
            continue
        label = labels.get(team_id, team_id[:8])
        for path in sorted(tasks_dir.glob("*.md")):
            try:
                head = path.read_text(errors="replace")[:500].lower()
            except OSError:
                continue
            if not (
                "in progress" in head
                or "in-progress" in head
                or "## next steps" in head
            ):
                continue
            meta = _file_frontmatter(path)
            desc = _file_description(path, meta)
            rel = path.relative_to(workspace)
            lines.append(
                f"- `{rel}` · team={label}" + (f" — {desc}" if desc else "")
            )
            if len(lines) >= limit:
                break
        if len(lines) >= limit:
            break

    if not lines:
        return ""
    return (
        "### Cross-team active task files (read with `read_context`)\n"
        + "\n".join(lines)
    )


def _allowed_context_roots(workspace: Path) -> list[Path]:
    """Roots the heartbeat may read: protected/memory/, teams/*/memory/."""
    from ..tools.workspace_paths import protected_memory

    roots = [protected_memory(workspace)]
    teams_root = workspace / "teams"
    if teams_root.is_dir():
        for team_dir in teams_root.iterdir():
            if team_dir.is_dir():
                roots.append(team_dir / "memory")
    return roots


def resolve_readable_context_path(
    workspace: Path, relative_path: str
) -> Path | None:
    """Return an absolute path if *relative_path* is under an allowed memory root.

    Accepts legacy ``memory/...`` paths as aliases for ``protected/memory/...``.
    """
    text = (relative_path or "").strip().lstrip("./")
    if not text or ".." in Path(text).parts:
        return None
    parts = Path(text).parts
    if parts and parts[0] == "memory":
        text = str(Path("protected") / text)
    candidate = (workspace / text).resolve()
    workspace_resolved = workspace.resolve()
    try:
        candidate.relative_to(workspace_resolved)
    except ValueError:
        return None
    for root in _allowed_context_roots(workspace):
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except ValueError:
            continue
    return None


def read_context_paths(
    workspace: Path,
    paths: list[str],
    *,
    doc_store=None,
    agent_name: str = "",
    max_paths: int = MAX_READ_CONTEXT_PATHS,
    max_tokens_per_file: int = MAX_READ_CONTEXT_TOKENS_PER_FILE,
) -> str:
    """Batch-read allowed memory/task files (and optional current team logs).

    Paths must be workspace-relative under ``protected/memory/`` (or legacy
    ``memory/``), or ``teams/<id>/memory/``. Log names like
    ``LOG:hermes:2026-07-20`` are resolved via *doc_store* when provided.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for raw in paths[:max_paths]:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)

        # Doc-store log shorthand: LOG:<agent>:<period> or just a store key.
        if doc_store is not None and (
            key.startswith("LOG:") or key.startswith("log:")
        ):
            store_key = key.split(":", 1)[1]
            content = doc_store.read(store_key) or ""
            if not content and agent_name:
                # Accept LOG:2026-07-20 → agent-qualified log name.
                content = doc_store.read(
                    doc_store.log_name(agent_name, store_key)
                ) or ""
            if content:
                truncated = content
                max_chars = max_tokens_per_file * CHARS_PER_TOKEN
                if len(truncated) > max_chars:
                    truncated = truncated[:max_chars] + "\n[...truncated]"
                parts.append(f"### {key}\n{truncated}")
            else:
                parts.append(f"### {key}\n(not found)")
            continue

        resolved = resolve_readable_context_path(workspace, key)
        if resolved is None or not resolved.is_file():
            parts.append(f"### {key}\n(not readable or outside allowed memory roots)")
            continue
        content = _load_file_truncated(resolved, max_tokens_per_file)
        parts.append(f"### {key}\n{content or '(empty)'}")

    if not parts:
        return "No context paths read."
    return "\n\n".join(parts)


def make_read_context_tool(
    workspace: Path,
    *,
    doc_store=None,
    agent_name: str = "",
):
    """Build the path-confined ``read_context`` tool for heartbeat ticks."""
    from smolagents import tool

    @tool
    def read_context(paths: list[str]) -> str:
        """Read indexed memory/task files or current team logs (batch, path-confined).

        Args:
            paths: Workspace-relative paths under protected/memory/ (or legacy
                memory/) or teams/<id>/memory/, or LOG:<name> keys for the
                current doc-store log. Max 4 paths.
        """
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            return "paths must be a list of strings"
        return read_context_paths(
            workspace,
            [str(p) for p in paths],
            doc_store=doc_store,
            agent_name=agent_name,
        )

    return read_context
