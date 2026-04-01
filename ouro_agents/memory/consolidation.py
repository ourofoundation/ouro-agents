"""Memory consolidation: compaction, promotion, and decay.

Runs periodically (triggered by heartbeat) to keep the memory system healthy:
1. Working memory compaction — rewrite when over token budget to merge/prune
2. Daily log promotion — promote yesterday's important entries to working memory
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
You are a memory curator. Given the current contents of the agent's persistent \
working memory, rewrite it to be more concise and useful.

Rules:
- Remove duplicate or near-duplicate entries
- Remove stale entries that are no longer relevant (outdated facts, completed one-off tasks)
- Merge related entries into single concise statements
- Keep the same section structure: ## Facts, ## Preferences, ## Learnings
- Keep entries that represent durable knowledge, ongoing preferences, or hard-won learnings
- ALWAYS preserve [label](asset:<uuid>) links — these are direct references to Ouro assets and must not be stripped or rewritten
- Target: under {max_tokens} tokens (~{max_chars} characters)
- Preserve the YAML frontmatter header exactly as-is

Output the complete rewritten working memory content, nothing else."""

PROMOTION_PROMPT = """\
You are a memory curator. Given yesterday's daily log and the agent's current \
working memory, decide which log entries (if any) should be promoted to working \
memory as durable knowledge.

Rules:
- Only promote facts, patterns, or learnings that will be useful in FUTURE sessions
- Do NOT promote one-off task completions ("Published X post") unless they reveal a reusable pattern
- ALWAYS preserve [label](asset:<uuid>) links from log entries — these are direct references to Ouro assets
- Output a JSON array of objects: [{"section": "Facts"|"Preferences"|"Learnings", "entry": "text"}]
- If nothing is worth promoting, return an empty array: []
- Output ONLY the JSON array, no markdown fences, no explanation."""


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def compact_memory_md(
    workspace: Path,
    config: MemoryConfig,
    model,
    doc_store=None,
    agent_name: str = "",
) -> bool:
    """Rewrite working memory if it exceeds the token budget. Returns True if compacted."""
    post_name = f"MEMORY:{agent_name}"
    content = doc_store.read(post_name) if doc_store else ""

    if not content:
        return False

    tokens = _estimate_tokens(content)
    if tokens <= config.memory_md_max_tokens:
        logger.debug("Working memory is %d tokens, under %d budget", tokens, config.memory_md_max_tokens)
        return False

    logger.info("Working memory is %d tokens, compacting to %d", tokens, config.memory_md_max_tokens)
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

        if not doc_store.write(post_name, text):
            raise RuntimeError(f"Failed to write {post_name}")

        new_tokens = _estimate_tokens(text)
        logger.info("Compacted working memory: %d -> %d tokens", tokens, new_tokens)
        return True
    except Exception as e:
        logger.warning("Working memory compaction failed: %s", e)
        return False


def promote_daily_entries(
    workspace: Path,
    model,
    doc_store=None,
    agent_name: str = "",
) -> int:
    """Promote worthy entries from yesterday's daily log to working memory. Returns count."""
    if not doc_store:
        return 0

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    daily_content = doc_store.read(f"DAILY:{agent_name}:{yesterday}").strip()
    memory_content = doc_store.read(f"MEMORY:{agent_name}")

    if not daily_content or len(daily_content) < 20:
        return 0

    try:
        result = model(
            [
                {"role": "system", "content": PROMOTION_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Yesterday's daily log:\n{daily_content}\n\n"
                        f"Current working memory:\n{memory_content}"
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

        content = memory_content
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

        if not doc_store.write(f"MEMORY:{agent_name}", content):
            raise RuntimeError(f"Failed to write MEMORY:{agent_name}")

        logger.info("Promoted %d entries from %s daily log to working memory", len(entries), yesterday)
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


def _consolidate_user_comments(doc_store, agent_name: str, model) -> int:
    """Merge comments from other agents into USER:* posts this agent owns."""
    if not doc_store:
        return 0

    merged = 0
    team_posts = doc_store.search(f"USER:")
    for post in team_posts:
        name = post.get("name") or ""
        if not name.startswith("USER:"):
            continue
        if not doc_store.is_owner(name):
            continue

        comments = doc_store.read_comments(name)
        if not comments:
            continue

        new_entries = []
        for c in comments:
            content = c.get("content_markdown") or c.get("content", "")
            if content.strip():
                new_entries.append(content.strip())

        if not new_entries:
            continue

        section_md = "## Recent Contributions\n" + "\n".join(f"- {e}" for e in new_entries) + "\n"
        if not doc_store.append(name, section_md):
            logger.warning("Failed to consolidate comments into %s", name)
            continue
        merged += len(new_entries)
        logger.info("Consolidated %d comments into %s", len(new_entries), name)

    return merged


def run_consolidation(
    workspace: Path,
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    model,
    doc_store=None,
) -> dict:
    """Run all consolidation tasks. Returns a summary dict."""
    results = {
        "compacted": False,
        "promoted": 0,
        "decayed": 0,
        "comments_merged": 0,
    }

    if not config.consolidation_enabled:
        return results

    results["compacted"] = compact_memory_md(
        workspace, config, model,
        doc_store=doc_store, agent_name=agent_id,
    )
    results["promoted"] = promote_daily_entries(
        workspace, model,
        doc_store=doc_store, agent_name=agent_id,
    )
    results["decayed"] = decay_old_memories(backend, agent_id, config)
    results["comments_merged"] = _consolidate_user_comments(doc_store, agent_id, model)

    logger.info("Consolidation complete: %s", results)
    return results
