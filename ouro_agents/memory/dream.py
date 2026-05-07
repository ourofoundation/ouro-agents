"""Dream mode: memory maintenance, compaction, promotion, decay, and review.

The agent's "dream" cycle runs on a schedule (default nightly) to keep the
memory system healthy and prevent stale learnings from persisting:

1. Working memory compaction — rewrite when over token budget to merge/prune
2. Daily log promotion — promote yesterday's important entries to working memory
3. Importance decay — reduce importance of old unaccessed memories
4. Confidence decay — reduce confidence on volatile memories not recently verified
5. Dream review — LLM re-evaluation of stale volatile memories
6. Comment consolidation — merge comments into owned USER:* posts
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import MemoryBackend, MemoryResult
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
- Do NOT promote entries that duplicate or closely overlap with content already in the working memory. Check the existing memory below before deciding what to promote.
- Do NOT promote one-off task completions ("Published X post") unless they reveal a reusable pattern
- ALWAYS preserve [label](asset:<uuid>) links from log entries — these are direct references to Ouro assets
- Output a JSON array of objects: [{"section": "Facts"|"Preferences"|"Learnings", "entry": "text"}]
- If nothing is worth promoting, return an empty array: []
- Output ONLY the JSON array, no markdown fences, no explanation."""

DREAM_REVIEW_PROMPT = """\
You are reviewing an agent's stored memories for accuracy. Each memory below \
was learned at a specific point in time and may no longer be true — especially \
memories about system behavior, API responses, error states, or resource availability.

For each memory, determine whether it is likely still accurate or has become stale.

Memories to review:
{memories_block}

For each memory, output a JSON verdict:
[{{"id": "memory_id", "status": "confirmed"|"contradicted"|"uncertain", "reason": "brief explanation", "replacement": "corrected fact if contradicted, else null"}}]

Guidelines:
- "confirmed": The fact is likely still true based on general knowledge and context.
- "contradicted": The fact was likely a transient observation that is no longer true \
  (e.g., a temporary error, an outage that's been resolved, a resource that's been fixed).
- "uncertain": Not enough information to judge; leave for natural decay.
- For errors/outages/failures: assume they've been fixed unless they describe a \
  fundamental design constraint. Transient issues rarely persist for days.
- For API/endpoint behavior: if the memory describes unexpected failures, default to \
  "contradicted" after more than a few days unless it describes a known limitation.
- Be conservative with "confirmed" — only confirm facts you're confident are durable.

Output ONLY the JSON array, no markdown fences, no explanation."""


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def compact_memory_md(
    workspace: Path,
    config: MemoryConfig,
    model,
    doc_store=None,
    agent_name: str = "",
) -> bool:
    """Rewrite working memory if it exceeds the token budget. Returns True if compacted."""
    if not doc_store:
        return False
    post_name = doc_store.memory_name(agent_name)
    content = doc_store.read(post_name)

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


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


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
    daily_name = doc_store.daily_name(agent_name, yesterday)
    memory_name = doc_store.memory_name(agent_name)
    daily_content = doc_store.read(daily_name).strip()
    memory_content = doc_store.read(memory_name)

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

        if not doc_store.write(memory_name, content):
            raise RuntimeError(f"Failed to write {memory_name}")

        logger.info("Promoted %d entries from %s daily log to working memory", len(entries), yesterday)
        return len(entries)
    except Exception as e:
        logger.warning("Daily log promotion failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Importance decay (existing behavior)
# ---------------------------------------------------------------------------


def decay_old_memories(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
) -> int:
    """Apply category-specific importance decay to old memories. Returns count."""
    decay_rules = config.decay_rules or {}
    if not decay_rules and not config.decay_after_days:
        return 0
    if not team_id:
        return 0

    if all_memories is None:
        try:
            all_memories = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
        except Exception as e:
            logger.warning("Failed to load memories for decay: %s", e)
            return 0

    decayed = 0

    for mem in all_memories:
        if not mem.created_at:
            continue
        rule = decay_rules.get(mem.category)
        if rule:
            after_days = rule.get("after_days")
            factor = float(rule.get("factor", 0.5))
        else:
            after_days = config.decay_after_days
            factor = 0.5
        if after_days is None:
            continue
        try:
            created = datetime.fromisoformat(mem.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        cutoff = datetime.now(timezone.utc) - timedelta(days=int(after_days))
        if created < cutoff and mem.importance > 0.1:
            new_importance = max(0.1, mem.importance * factor)
            try:
                memory_id = getattr(mem, "id", "") or mem.source
                backend.update_metadata(memory_id, {"importance": new_importance})
                decayed += 1
            except Exception:
                pass

    if decayed:
        logger.info("Decayed importance on %d old memories", decayed)
    return decayed


# ---------------------------------------------------------------------------
# Confidence decay (volatility-based)
# ---------------------------------------------------------------------------


def decay_stale_confidence(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
) -> int:
    """Reduce confidence on volatile memories that haven't been re-verified.

    Higher volatility means faster confidence decay. A memory with volatility=0.9
    has a half-life of ~2 days; volatility=0.5 has ~7 days. Memories below the
    volatility threshold (0.2) are considered stable and never decay in confidence.
    """
    if not config.confidence_decay_enabled:
        return 0
    if not team_id:
        return 0

    if all_memories is None:
        try:
            all_memories = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
        except Exception as e:
            logger.warning("Failed to load memories for confidence decay: %s", e)
            return 0

    now = datetime.now(timezone.utc)
    decayed = 0

    for mem in all_memories:
        if mem.volatility <= 0.2:
            continue
        if mem.confidence <= 0.1:
            continue

        # Determine the reference point: last_verified or created_at
        reference = None
        if mem.last_verified:
            try:
                reference = datetime.fromisoformat(mem.last_verified)
                if reference.tzinfo is None:
                    reference = reference.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                reference = None
        if reference is None and mem.created_at:
            try:
                reference = datetime.fromisoformat(mem.created_at)
                if reference.tzinfo is None:
                    reference = reference.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

        if reference is None:
            continue

        days_since = (now - reference).total_seconds() / 86400
        # Half-life: volatility 0.9 → 2 days, 0.5 → 7 days, 0.3 → 10 days
        half_life_days = max(2, int(14 * (1 - mem.volatility)))

        if days_since <= half_life_days:
            continue

        decay_factor = 0.5 ** (days_since / half_life_days)
        new_confidence = max(0.1, mem.confidence * decay_factor)

        if abs(new_confidence - mem.confidence) < 0.01:
            continue

        try:
            memory_id = getattr(mem, "id", "") or mem.source
            backend.update_metadata(memory_id, {"confidence": new_confidence})
            decayed += 1
        except Exception:
            pass

    if decayed:
        logger.info("Decayed confidence on %d volatile memories", decayed)
    return decayed


# ---------------------------------------------------------------------------
# Dream review (LLM re-evaluation of stale memories)
# ---------------------------------------------------------------------------


def _select_review_candidates(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
) -> list[MemoryResult]:
    """Select memories that are candidates for dream review.

    Targets volatile memories whose confidence has decayed into the uncertain
    zone but that still have enough importance to matter.
    """
    if not team_id:
        return []

    if all_memories is None:
        try:
            all_memories = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
        except Exception as e:
            logger.warning("Failed to load memories for dream review: %s", e)
            return []

    candidates = [
        mem for mem in all_memories
        if mem.volatility > 0.4
        and mem.confidence < 0.5
        and mem.importance > 0.2
    ]

    # Sort by importance descending — review the most impactful stale memories first
    candidates.sort(key=lambda m: m.importance, reverse=True)
    return candidates[:config.dream_review_max_per_run]


def review_stale_memories(
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    model,
    team_id: str | None = None,
    all_memories: list[MemoryResult] | None = None,
) -> dict:
    """LLM-review memories that have decayed into the uncertain zone.

    Returns a summary: {"reviewed": int, "confirmed": int, "contradicted": int, "uncertain": int}
    """
    result = {"reviewed": 0, "confirmed": 0, "contradicted": 0, "uncertain": 0}

    if not config.dream_review_enabled:
        return result

    candidates = _select_review_candidates(
        backend, agent_id, config, team_id=team_id, all_memories=all_memories,
    )
    if not candidates:
        return result

    # Build the review block
    memory_lines = []
    for mem in candidates:
        age_days = 0
        if mem.created_at:
            try:
                created = datetime.fromisoformat(mem.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = int((datetime.now(timezone.utc) - created).total_seconds() / 86400)
            except (ValueError, TypeError):
                pass
        memory_lines.append(
            f"- ID: {mem.id}\n"
            f"  Text: {mem.text}\n"
            f"  Category: {mem.category}\n"
            f"  Stored: {age_days} days ago\n"
            f"  Volatility: {mem.volatility:.1f}\n"
            f"  Current confidence: {mem.confidence:.2f}"
        )

    memories_block = "\n".join(memory_lines)

    try:
        llm_result = model(
            [
                {
                    "role": "system",
                    "content": DREAM_REVIEW_PROMPT.format(memories_block=memories_block),
                },
                {
                    "role": "user",
                    "content": "Review the memories above and return your verdicts as JSON.",
                },
            ],
        )
        text = llm_result.content if hasattr(llm_result, "content") else str(llm_result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        verdicts = json.loads(text)
        if not isinstance(verdicts, list):
            return result

        now_iso = datetime.now(timezone.utc).isoformat()
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            memory_id = str(verdict.get("id") or "")
            status = verdict.get("status", "uncertain")
            if not memory_id:
                continue

            result["reviewed"] += 1

            if status == "confirmed":
                result["confirmed"] += 1
                backend.update_metadata(memory_id, {
                    "confidence": 0.8,
                    "last_verified": now_iso,
                })
            elif status == "contradicted":
                result["contradicted"] += 1
                replacement = verdict.get("replacement")
                if replacement and isinstance(replacement, str) and replacement.strip():
                    # TODO: In the future, write the replacement as a new memory.
                    # For now, just delete the stale one.
                    pass
                backend.delete(memory_id)
            else:
                result["uncertain"] += 1

        logger.info(
            "Dream review complete: %d reviewed, %d confirmed, %d contradicted, %d uncertain",
            result["reviewed"], result["confirmed"], result["contradicted"], result["uncertain"],
        )
    except Exception as e:
        logger.warning("Dream review failed: %s", e)

    return result


# ---------------------------------------------------------------------------
# Comment consolidation
# ---------------------------------------------------------------------------


def _consolidate_user_comments(doc_store, agent_name: str, model) -> int:
    """Merge comments from other agents into USER:* posts this agent owns."""
    if not doc_store:
        return 0

    merged = 0
    team_posts = doc_store.search("USER:")
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_dream(
    workspace: Path,
    backend: MemoryBackend,
    agent_id: str,
    config: MemoryConfig,
    model,
    doc_store=None,
    team_id: str | None = None,
) -> dict:
    """Run the full dream cycle: maintenance, decay, review. Returns a summary dict."""
    results = {
        "compacted": False,
        "promoted": 0,
        "importance_decayed": 0,
        "confidence_decayed": 0,
        "dream_review": {"reviewed": 0, "confirmed": 0, "contradicted": 0, "uncertain": 0},
        "comments_merged": 0,
    }

    if not config.dream_enabled:
        return results

    # Doc-based maintenance (works with or without team_id)
    results["compacted"] = compact_memory_md(
        workspace, config, model,
        doc_store=doc_store, agent_name=agent_id,
    )
    results["promoted"] = promote_daily_entries(
        workspace, model,
        doc_store=doc_store, agent_name=agent_id,
    )

    # Vector-memory operations: load once, share across all three passes
    all_memories: list[MemoryResult] | None = None
    if team_id:
        try:
            all_memories = backend.get_all(agent_id=agent_id, limit=300, team_id=team_id)
        except Exception as e:
            logger.warning("Failed to load memories for dream cycle: %s", e)
            all_memories = []

    results["importance_decayed"] = decay_old_memories(
        backend, agent_id, config, team_id=team_id, all_memories=all_memories,
    )
    results["confidence_decayed"] = decay_stale_confidence(
        backend, agent_id, config, team_id=team_id, all_memories=all_memories,
    )
    results["dream_review"] = review_stale_memories(
        backend, agent_id, config, model, team_id=team_id, all_memories=all_memories,
    )

    results["comments_merged"] = _consolidate_user_comments(doc_store, agent_id, model)

    logger.info("Dream cycle complete: %s", results)
    return results
