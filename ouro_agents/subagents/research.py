"""Context loader subagent for internal context gathering.

Runs in its own context window to search memory, load entity files, and
synthesize a concise briefing for the parent agent. This keeps raw search
results and intermediate reasoning out of the parent's context.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from ..config import MemoryConfig
from ..constants import CHARS_PER_TOKEN
from ..memory import MemoryBackend, MemoryResult, format_memories
from ..memory.context_loader import load_entity_files
from ..memory.conversation_state import ConversationState
from ..memory.retrieval import (
    _composite_score,
    _decompose_queries,
    _deduplicate,
    _fallback_queries,
    _search_single,
)

logger = logging.getLogger(__name__)

_BRIEFING_PROMPT = (
    "You are a research assistant preparing a briefing for an AI agent that is about "
    "to handle a user request. You've been given raw context from multiple sources. "
    "Your job is to synthesize this into a concise, actionable briefing.\n\n"
    "Rules:\n"
    "- Lead with the most relevant information for the specific request\n"
    "- Drop anything that isn't relevant to the current request\n"
    "- Preserve specific facts, names, IDs, and decisions — don't over-summarize these\n"
    "- Merge duplicate information across sources\n"
    "- Keep the briefing under {max_tokens} tokens\n"
    "- Use a flat structure with clear sections only if there are distinct topics\n"
    "- If nothing is relevant, say 'No relevant context found.'\n\n"
    "Output ONLY the briefing text, no preamble."
)


def _load_active_tasks(workspace: Path, max_tokens: int = 2000) -> str:
    """Load in-progress task files."""
    tasks_dir = workspace / "memory" / "tasks"
    if not tasks_dir.exists():
        return ""

    parts: list[str] = []
    total_chars = 0
    max_chars = max_tokens * CHARS_PER_TOKEN

    for p in list(tasks_dir.glob("*.md"))[:4]:
        try:
            content = p.read_text(errors="replace").strip()
            header = content[:500].lower()
            if "in progress" not in header and "in-progress" not in header and "## next steps" not in header:
                continue
        except Exception:
            continue

        remaining = max_chars - total_chars
        if remaining < 100:
            break
        if len(content) > remaining:
            content = content[:remaining] + "\n[...truncated]"
        parts.append(f"**{p.stem}**\n{content}")
        total_chars += len(content)

    return "\n\n".join(parts)


def _load_working_memory(workspace: Path, max_tokens: int = 3000) -> str:
    """Load MEMORY.md and today's daily log."""
    parts: list[str] = []
    max_chars = max_tokens * CHARS_PER_TOKEN
    total_chars = 0

    memory_md = workspace / "MEMORY.md"
    if memory_md.exists():
        content = memory_md.read_text(errors="replace").strip()
        # Strip frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].lstrip("\n")
        if content:
            if len(content) > max_chars // 2:
                content = content[:max_chars // 2] + "\n[...truncated]"
            parts.append(content)
            total_chars += len(content)

    for delta_days, label in [(0, "Today"), (1, "Yesterday")]:
        day = (date.today() - timedelta(days=delta_days)).isoformat()
        daily = workspace / "memory" / "daily" / f"{day}.md"
        if daily.exists():
            content = daily.read_text(errors="replace").strip()
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    content = content[end + 3:].lstrip("\n")
            remaining = max_chars - total_chars
            if content and remaining > 100:
                if len(content) > remaining:
                    content = content[:remaining] + "\n[...truncated]"
                parts.append(f"### {label}'s Log ({day})\n{content}")
                total_chars += len(content)

    return "\n\n".join(parts)


def gather_raw_context(
    task: str,
    workspace: Path,
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    model=None,
    user_id: Optional[str] = None,
    conversation_state: Optional[ConversationState] = None,
    memory_scopes: Optional[list[str]] = None,
) -> dict[str, str]:
    """Gather raw context from all sources in parallel.

    Returns a dict of source_name -> raw_content. This runs searches and
    file reads concurrently for speed.

    If ``memory_scopes`` is provided, vector memory results are filtered to
    entries whose category/tags overlap with the given scopes.
    """
    results: dict[str, str] = {}

    def _search_memories() -> str:
        if model and config.retrieval_queries > 1:
            queries = _decompose_queries(
                task, conversation_state, model, n=config.retrieval_queries
            )
        else:
            queries = _fallback_queries(task, conversation_state)

        all_results: list[MemoryResult] = []
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            futures = {
                executor.submit(
                    _search_single, backend, q, agent_id, user_id, config.search_limit
                ): q
                for q in queries
            }
            for future in as_completed(futures):
                all_results.extend(future.result())

        if not all_results:
            return ""

        # Apply memory scope filtering if specified
        if memory_scopes:
            scope_set = set(memory_scopes)
            all_results = [
                m for m in all_results
                if not getattr(m, "categories", None)
                or scope_set & set(getattr(m, "categories", []))
            ]

        deduped = _deduplicate(all_results)
        for m in deduped:
            m.score = _composite_score(m)
        deduped.sort(key=lambda m: m.score, reverse=True)

        budget_chars = config.max_retrieval_tokens * 2 * CHARS_PER_TOKEN
        selected: list[MemoryResult] = []
        total = 0
        for m in deduped:
            entry_chars = len(m.text) + 10
            if total + entry_chars > budget_chars:
                break
            selected.append(m)
            total += entry_chars

        return format_memories(selected, min_score=0.0)

    def _load_entities() -> str:
        return load_entity_files(workspace, conversation_state)

    def _load_tasks() -> str:
        return _load_active_tasks(workspace)

    def _load_memory() -> str:
        return _load_working_memory(workspace)

    # Run all sources in parallel
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(_search_memories): "vector_memories",
            executor.submit(_load_entities): "entity_files",
            executor.submit(_load_tasks): "active_tasks",
            executor.submit(_load_memory): "working_memory",
        }
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                content = future.result()
                if content:
                    results[key] = content
            except Exception as e:
                logger.warning("Context loader: %s failed: %s", key, e)

    return results


def synthesize_briefing(
    task: str,
    raw_context: dict[str, str],
    model,
    max_tokens: int = 1500,
) -> str:
    """Use a cheap LLM call to synthesize raw context into a concise briefing.

    If the raw context is already small enough, skip the LLM call and
    return it directly.
    """
    if not raw_context:
        return ""

    # Format raw context for the synthesizer
    parts: list[str] = []
    for source, content in raw_context.items():
        label = source.replace("_", " ").title()
        parts.append(f"### Source: {label}\n{content}")

    combined = "\n\n---\n\n".join(parts)
    combined_tokens = len(combined) // CHARS_PER_TOKEN

    # If already under budget, skip the synthesis LLM call
    if combined_tokens <= max_tokens:
        logger.info(
            "Context loader: raw context (%d tokens) within budget, skipping synthesis",
            combined_tokens,
        )
        return combined

    # Synthesize with LLM
    try:
        result = model(
            [
                {
                    "role": "system",
                    "content": _BRIEFING_PROMPT.format(max_tokens=max_tokens),
                },
                {
                    "role": "user",
                    "content": (
                        f"User request:\n{task[:500]}\n\n"
                        f"Raw context:\n{combined}"
                    ),
                },
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        briefing = text.strip()
        logger.info(
            "Context loader: synthesized %d token context into ~%d token briefing",
            combined_tokens,
            len(briefing) // CHARS_PER_TOKEN,
        )
        return briefing
    except Exception as e:
        logger.warning("Context loader synthesis failed, using truncated raw: %s", e)
        max_chars = max_tokens * CHARS_PER_TOKEN
        return combined[:max_chars] + "\n[...truncated]"

