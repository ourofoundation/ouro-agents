"""Multi-query retrieval pipeline for memory search.

Instead of a single vector search, this module:
1. Decomposes the task into multiple search queries (different facets)
2. Runs them in parallel against the memory backend
3. Deduplicates and re-ranks by a composite score (relevance + recency + importance)
4. Caps output to a token budget
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from . import MemoryResult, format_memories
from ..constants import CHARS_PER_TOKEN

if TYPE_CHECKING:
    from . import MemoryBackend
    from .conversation_state import ConversationState
    from ..config import MemoryConfig

logger = logging.getLogger(__name__)

QUERY_DECOMPOSITION_PROMPT = """\
You are a search query generator. Given a user request and conversation context,
generate {n} diverse search queries that would help retrieve relevant memories.

Each query should approach the request from a different angle:
- Query 1: Direct semantic match for the core request
- Query 2: Related concepts, entities, or background knowledge
- Query 3: Past decisions, preferences, or patterns relevant to this type of request

Output ONLY a JSON array of strings, no markdown fences, no explanation.
Example: ["query one", "query two", "query three"]"""


def _decompose_queries(
    task: str,
    conversation_state: Optional["ConversationState"],
    model,
    n: int = 3,
) -> list[str]:
    """Use a cheap LLM call to generate diverse search queries."""
    context_parts: list[str] = []
    if conversation_state:
        if conversation_state.current_topic:
            context_parts.append(f"Topic: {conversation_state.current_topic}")
        if conversation_state.active_goals:
            context_parts.append(f"Goals: {'; '.join(conversation_state.active_goals)}")
        if conversation_state.key_entities:
            context_parts.append(f"Entities: {', '.join(conversation_state.key_entities)}")

    context_str = "\n".join(context_parts) if context_parts else "No prior context."

    try:
        result = model(
            [
                {"role": "system", "content": QUERY_DECOMPOSITION_PROMPT.format(n=n)},
                {
                    "role": "user",
                    "content": f"Conversation context:\n{context_str}\n\nUser request:\n{task[:500]}",
                },
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        queries = json.loads(text)
        if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
            return queries[:n]
    except Exception as e:
        logger.warning("Query decomposition failed, falling back to single query: %s", e)

    return _fallback_queries(task, conversation_state)


def _fallback_queries(
    task: str, conversation_state: Optional["ConversationState"]
) -> list[str]:
    """Generate queries without LLM when decomposition fails."""
    queries = [task]
    if conversation_state:
        if conversation_state.current_topic:
            queries.append(conversation_state.current_topic)
        if conversation_state.key_entities:
            queries.append(" ".join(conversation_state.key_entities))
    return queries


def _days_ago(iso_timestamp: str) -> float:
    """Return how many days ago an ISO timestamp was. Returns 999 if unparseable."""
    if not iso_timestamp:
        return 999.0
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0.0, delta.total_seconds() / 86400)
    except Exception:
        return 999.0


def _composite_score(memory: MemoryResult) -> float:
    """Score a memory by semantic relevance, recency, and importance."""
    relevance = memory.score
    age_days = _days_ago(memory.created_at)
    recency = max(0.0, 1.0 - (age_days / 90.0))
    importance = memory.importance

    return 0.6 * relevance + 0.25 * recency + 0.15 * importance


def _deduplicate(memories: list[MemoryResult]) -> list[MemoryResult]:
    """Remove near-duplicate memories, keeping the highest-scored version."""
    seen: dict[str, MemoryResult] = {}
    for m in memories:
        key = m.text.strip().lower()[:100]
        existing = seen.get(key)
        if existing is None or m.score > existing.score:
            seen[key] = m
    return list(seen.values())


def _search_single(
    backend: "MemoryBackend",
    query: str,
    agent_id: str,
    user_id: Optional[str],
    limit: int,
) -> list[MemoryResult]:
    """Run a single search query (for use in ThreadPoolExecutor)."""
    try:
        return backend.search(query, agent_id=agent_id, user_id=user_id, limit=limit)
    except Exception as e:
        logger.warning("Memory search failed for query '%s': %s", query[:60], e)
        return []


def retrieve_memories(
    task: str,
    backend: "MemoryBackend",
    agent_id: str,
    config: "MemoryConfig",
    model=None,
    user_id: Optional[str] = None,
    conversation_state: Optional["ConversationState"] = None,
) -> str:
    """Full retrieval pipeline: decompose -> parallel search -> dedup -> re-rank -> format.

    Returns formatted memory string ready for injection into the effective task.
    Returns empty string if nothing relevant is found.
    """
    if model and config.retrieval_queries > 1:
        queries = _decompose_queries(
            task, conversation_state, model, n=config.retrieval_queries
        )
    else:
        queries = _fallback_queries(task, conversation_state)

    logger.info("Memory retrieval with %d queries: %s", len(queries), queries)

    all_results: list[MemoryResult] = []
    per_query_limit = config.search_limit

    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        futures = {
            executor.submit(
                _search_single, backend, q, agent_id, user_id, per_query_limit
            ): q
            for q in queries
        }
        for future in as_completed(futures):
            results = future.result()
            all_results.extend(results)

    if not all_results:
        return ""

    deduped = _deduplicate(all_results)

    for m in deduped:
        m.score = _composite_score(m)

    deduped.sort(key=lambda m: m.score, reverse=True)

    budget_chars = config.max_retrieval_tokens * CHARS_PER_TOKEN
    selected: list[MemoryResult] = []
    total_chars = 0
    for m in deduped:
        entry_chars = len(m.text) + 10
        if total_chars + entry_chars > budget_chars:
            break
        selected.append(m)
        total_chars += entry_chars

    logger.info(
        "Memory retrieval: %d total -> %d deduped -> %d selected (~%d tokens)",
        len(all_results),
        len(deduped),
        len(selected),
        total_chars // CHARS_PER_TOKEN,
    )

    return format_memories(selected, min_score=0.0)
