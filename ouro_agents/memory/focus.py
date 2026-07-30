"""Focused memory retrieval for planning and heartbeat decisions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from . import MemoryBackend
from .relevance import is_focus_directive, memory_signal_score

logger = logging.getLogger(__name__)

FOCUS_MEMORY_QUERIES = (
    "current work direction and priorities for this agent",
    "what the user wants this agent to focus on next",
    "work this agent should avoid stop doing or de-prioritize",
    "recent focus decisions constraints and repeated work to avoid",
)

FOCUS_KEYWORDS = {
    "avoid",
    "constraint",
    "de-prioritize",
    "deprioritize",
    "direction",
    "don't",
    "do not",
    "focus",
    "less",
    "more",
    "next",
    "prefer",
    "priority",
    "repeat",
    "spend",
    "stop",
    "work on",
}

_FOCUS_CATEGORIES = frozenset({"direction", "preference", "fact"})
_RECENCY_SLOTS = 2
_RECENCY_DAYS = 7


def looks_like_focus_memory(memory) -> bool:
    """Return whether a memory is topically relevant to focus/planning decisions.

    Keyword/score heuristic only — does not check authority. Prefer
    :func:`memory_steers_focus` at call sites that gate prompt injection.
    """
    category = getattr(memory, "category", "fact") or "fact"
    if category == "direction":
        return True

    text = (getattr(memory, "text", "") or "").lower()
    if not text:
        return False

    has_focus_language = any(keyword in text for keyword in FOCUS_KEYWORDS)
    if category == "preference":
        return has_focus_language
    if category == "episode":
        return has_focus_language and getattr(memory, "score", 0.0) >= 0.35
    return has_focus_language and getattr(memory, "score", 0.0) >= 0.55


def memory_steers_focus(memory: Any) -> bool:
    """Single gate for memories allowed to steer planning/heartbeat focus.

    Intersection of:
    - category allowlist (direction / preference / fact)
    - authority via :func:`is_focus_directive`
    - topical focus language via :func:`looks_like_focus_memory`
    """
    category = getattr(memory, "category", "") or ""
    if category and category not in _FOCUS_CATEGORIES:
        return False
    return is_focus_directive(memory) and looks_like_focus_memory(memory)


def is_directional_feedback(feedback: str) -> bool:
    """Detect human feedback that should become future focus guidance."""
    approval_only = {
        "approved",
        "good to go",
        "looks good",
        "ship it",
        "ok",
        "okay",
        "yes",
    }
    text = feedback.strip().lower()
    if not text:
        return False
    if text in approval_only:
        return False
    if len(text.split()) >= 8:
        return True
    return any(keyword in text for keyword in FOCUS_KEYWORDS)


def remember_work_direction(
    memory: MemoryBackend | None,
    agent_id: str,
    direction: str | None,
    *,
    source: str,
    run_id: str = "",
    team_id: Optional[str] = None,
    asset_id: Optional[str] = None,
    strength: float = 0.9,
    text_prefix: str = "Work direction",
) -> bool:
    """Store controller/user guidance that should steer future work selection."""
    if not memory or not agent_id or not direction:
        return False
    if not is_directional_feedback(direction):
        return False

    metadata = {
        "category": "direction",
        "basis": "stated",
        "stability": "stable",
        "strength": strength,
        "source": source,
    }
    if asset_id:
        metadata["asset_ids"] = asset_id

    try:
        memory.add(
            f"{text_prefix}: {direction}",
            agent_id=agent_id,
            run_id=run_id,
            metadata=metadata,
            team_id=team_id,
        )
        return True
    except Exception as e:
        logger.warning("Failed to store work direction memory: %s", e)
        return False


def _focus_sort_key(memory) -> tuple[int, float, str]:
    category = getattr(memory, "category", "fact") or "fact"
    priority = {
        "direction": 0,
        "preference": 1,
        "fact": 2,
        "episode": 3,
    }.get(category, 4)
    signal = memory_signal_score(memory, explicit_filter=True)
    created_at = getattr(memory, "created_at", "") or ""
    return (priority, -signal, created_at)


def _created_at_sort_key(memory) -> str:
    return str(getattr(memory, "created_at", "") or "")


def _recent_direction_slots(
    memory: MemoryBackend,
    agent_id: str,
    *,
    team_id: Optional[str],
    asset_id: Optional[str],
    slots: int = _RECENCY_SLOTS,
    days: int = _RECENCY_DAYS,
) -> list[Any]:
    """Newest direction memories from the last N days, ignoring similarity."""
    get_all = getattr(memory, "get_all", None)
    if not callable(get_all) or slots <= 0:
        return []

    since = datetime.now(timezone.utc) - timedelta(days=max(0, days))
    try:
        recent = get_all(
            agent_id=agent_id,
            limit=max(slots * 4, 8),
            team_id=team_id,
            asset_id=asset_id,
            category="direction",
            since=since,
        )
    except TypeError:
        try:
            recent = get_all(agent_id=agent_id, limit=max(slots * 4, 8))
        except Exception as e:
            logger.debug("Failed to load recent direction memories: %s", e)
            return []
    except Exception as e:
        logger.debug("Failed to load recent direction memories: %s", e)
        return []

    eligible = [item for item in (recent or []) if memory_steers_focus(item)]
    eligible.sort(key=_created_at_sort_key, reverse=True)
    return eligible[:slots]


def build_focus_memory_context(
    memory: MemoryBackend | None,
    agent_id: str,
    *,
    team_id: Optional[str] = None,
    asset_id: Optional[str] = None,
    limit: int = 8,
    heading: str = "Relevant Focus Memory",
    guidance: str = (
        "Use these memories as strong input when choosing focus and task scope."
    ),
    queries: Iterable[str] = FOCUS_MEMORY_QUERIES,
    reinforce: bool = True,
    recency_slots: int = _RECENCY_SLOTS,
) -> str:
    """Recall durable focus guidance and render it for a prompt context block.

    Reserves ``recency_slots`` of the ``limit`` for the newest direction
    memories from the last week (regardless of similarity), then fills the
    remainder from semantic focus search.
    """
    if not memory or not agent_id:
        return ""

    scope = "team" if team_id else "global"
    seen: set[str] = set()
    ordered: list[Any] = []

    for recent in _recent_direction_slots(
        memory,
        agent_id,
        team_id=team_id,
        asset_id=asset_id,
        slots=min(recency_slots, limit),
    ):
        text = (getattr(recent, "text", "") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(recent)

    results = []
    for query in queries:
        matches = []
        # One search without category fan-out; filter locally. This cuts
        # embedding calls from 4 queries × 3 categories to 4 queries.
        try:
            search_kwargs = dict(
                query=query,
                agent_id=agent_id,
                limit=8,
                team_id=team_id,
                scope=scope,
                asset_id=asset_id,
                reinforce=reinforce,
            )
            try:
                matches.extend(memory.search(**search_kwargs))
            except TypeError:
                search_kwargs.pop("reinforce", None)
                matches.extend(memory.search(**search_kwargs))
        except Exception as e:
            logger.warning("Failed to recall focus memory: %s", e)
            continue

        for match in matches:
            text = (getattr(match, "text", "") or "").strip()
            if not text or text in seen:
                continue
            if not memory_steers_focus(match):
                continue
            seen.add(text)
            results.append(match)

    results.sort(key=_focus_sort_key)
    remaining = max(0, limit - len(ordered))
    ordered.extend(results[:remaining])

    if not ordered:
        return ""

    lines = [f"### {heading}", guidance]
    for memory_item in ordered[:limit]:
        category = getattr(memory_item, "category", "fact") or "fact"
        lines.append(f"- [{category}] {memory_item.text}")
    return "\n".join(lines)
