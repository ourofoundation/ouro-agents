"""Memory consolidation: compaction, promotion, and decay.

Runs periodically (triggered by heartbeat) to keep the memory system healthy:
1. MEMORY.md compaction — rewrite when over token budget to merge/prune
2. Daily log promotion — promote yesterday's important entries to MEMORY.md
3. Memory decay — reduce importance of old unaccessed memories
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import MemoryBackend
from ..config import MemoryConfig
from ..constants import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

COMPACTION_PROMPT = """\
You are a memory curator. Given the current contents of MEMORY.md (the agent's
persistent working memory), rewrite it to be more concise and useful.

Rules:
- Remove duplicate or near-duplicate entries
- Remove stale entries that are no longer relevant (outdated facts, completed one-off tasks)
- Merge related entries into single concise statements
- Keep the same section structure: ## Facts, ## Preferences, ## Learnings
- Keep entries that represent durable knowledge, ongoing preferences, or hard-won learnings
- Target: under {max_tokens} tokens (~{max_chars} characters)
- Preserve the YAML frontmatter header exactly as-is

Output the complete rewritten MEMORY.md content, nothing else."""

PROMOTION_PROMPT = """\
You are a memory curator. Given yesterday's daily log and the current MEMORY.md,
decide which log entries (if any) should be promoted to MEMORY.md as durable knowledge.

Rules:
- Only promote facts, patterns, or learnings that will be useful in FUTURE sessions
- Do NOT promote one-off task completions ("Published X post") unless they reveal a reusable pattern
- Output a JSON array of objects: [{"section": "Facts"|"Preferences"|"Learnings", "entry": "text"}]
- If nothing is worth promoting, return an empty array: []
- Output ONLY the JSON array, no markdown fences, no explanation."""


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("---", 3)
    if end == -1:
        return text
    return text[end + 3:].lstrip("\n")


def compact_memory_md(
    workspace: Path,
    config: MemoryConfig,
    model,
) -> bool:
    """Rewrite MEMORY.md if it exceeds the token budget. Returns True if compacted."""
    memory_path = workspace / "MEMORY.md"
    if not memory_path.exists():
        return False

    content = memory_path.read_text()
    body = _strip_frontmatter(content)
    tokens = _estimate_tokens(body)

    if tokens <= config.memory_md_max_tokens:
        logger.debug("MEMORY.md is %d tokens, under %d budget", tokens, config.memory_md_max_tokens)
        return False

    logger.info("MEMORY.md is %d tokens, compacting to %d", tokens, config.memory_md_max_tokens)
    max_chars = config.memory_md_max_tokens * CHARS_PER_TOKEN

    try:
        result = model(
            [
                {
                    "role": "system",
                    "content": COMPACTION_PROMPT.format(
                        max_tokens=config.memory_md_max_tokens,
                        max_chars=max_chars,
                    ),
                },
                {"role": "user", "content": content},
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        memory_path.write_text(text)
        new_tokens = _estimate_tokens(_strip_frontmatter(text))
        logger.info("Compacted MEMORY.md: %d -> %d tokens", tokens, new_tokens)
        return True
    except Exception as e:
        logger.warning("MEMORY.md compaction failed: %s", e)
        return False


def promote_daily_entries(
    workspace: Path,
    model,
) -> int:
    """Promote worthy entries from yesterday's daily log to MEMORY.md. Returns count."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    daily_path = workspace / "memory" / "daily" / f"{yesterday}.md"
    if not daily_path.exists():
        return 0

    daily_content = daily_path.read_text().strip()
    if not daily_content or len(daily_content) < 20:
        return 0

    memory_path = workspace / "MEMORY.md"
    memory_content = memory_path.read_text() if memory_path.exists() else ""

    try:
        result = model(
            [
                {"role": "system", "content": PROMOTION_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Yesterday's daily log:\n{daily_content}\n\n"
                        f"Current MEMORY.md:\n{memory_content}"
                    ),
                },
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        entries = json.loads(text)
        if not isinstance(entries, list) or not entries:
            return 0

        content = memory_path.read_text() if memory_path.exists() else ""
        for entry in entries:
            section = entry.get("section", "Facts")
            text = entry.get("entry", "").strip()
            if not text:
                continue

            header = f"## {section}"
            bullet = f"- {text}\n"
            if header in content:
                idx = content.index(header) + len(header)
                next_newline = content.index("\n", idx) + 1
                content = content[:next_newline] + bullet + content[next_newline:]
            else:
                content = content.rstrip() + f"\n\n{header}\n{bullet}"

        memory_path.write_text(content)
        logger.info("Promoted %d entries from %s daily log to MEMORY.md", len(entries), yesterday)
        return len(entries)
    except Exception as e:
        logger.warning("Daily log promotion failed: %s", e)
        return 0


def decay_old_memories(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
) -> int:
    """Halve the importance of memories older than decay_after_days. Returns count."""
    if not config.decay_after_days:
        return 0

    try:
        all_memories = backend.get_all(agent_id=agent_id, limit=200)
    except Exception as e:
        logger.warning("Failed to load memories for decay: %s", e)
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=config.decay_after_days)
    decayed = 0

    for mem in all_memories:
        if not mem.created_at:
            continue
        try:
            created = datetime.fromisoformat(mem.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if created < cutoff and mem.importance > 0.1:
            new_importance = max(0.1, mem.importance * 0.5)
            try:
                backend.update_metadata(mem.source, {"importance": new_importance})
                decayed += 1
            except Exception:
                pass

    if decayed:
        logger.info("Decayed importance on %d old memories", decayed)
    return decayed


def run_consolidation(
    workspace: Path,
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    model,
) -> dict:
    """Run all consolidation tasks. Returns a summary dict."""
    results = {
        "compacted": False,
        "promoted": 0,
        "decayed": 0,
    }

    if not config.consolidation_enabled:
        return results

    results["compacted"] = compact_memory_md(workspace, config, model)
    results["promoted"] = promote_daily_entries(workspace, model)
    results["decayed"] = decay_old_memories(backend, agent_id, config)

    logger.info("Consolidation complete: %s", results)
    return results
