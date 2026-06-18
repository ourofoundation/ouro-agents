"""Focused memory retrieval for planning and heartbeat decisions."""

from __future__ import annotations

import logging
from typing import Iterable, Optional

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


def looks_like_focus_memory(memory) -> bool:
    """Return whether a memory is relevant to focus/planning decisions."""
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
) -> str:
    """Recall durable focus guidance and render it for a prompt context block."""
    if not memory or not agent_id:
        return ""

    scope = "team" if team_id else "global"
    seen: set[str] = set()
    results = []
    for query in queries:
        matches = []
        for category in ("direction", "preference", "fact"):
            try:
                matches.extend(
                    memory.search(
                        query=query,
                        agent_id=agent_id,
                        limit=6,
                        team_id=team_id,
                        scope=scope,
                        category=category,
                        asset_id=asset_id,
                    )
                )
            except TypeError:
                try:
                    matches.extend(
                        memory.search(
                            query=query,
                            agent_id=agent_id,
                            limit=6,
                            team_id=team_id,
                            scope=scope,
                        )
                    )
                except Exception as e:
                    logger.warning("Failed to recall focus memory: %s", e)
                    break
            except Exception as e:
                logger.warning("Failed to recall focus memory: %s", e)
                break

        for match in matches:
            text = (getattr(match, "text", "") or "").strip()
            if not text or text in seen:
                continue
            if not is_focus_directive(match):
                continue
            if not looks_like_focus_memory(match):
                continue
            seen.add(text)
            results.append(match)

    if not results:
        return ""

    results.sort(key=_focus_sort_key)
    lines = [f"### {heading}", guidance]
    for memory_item in results[:limit]:
        category = getattr(memory_item, "category", "fact") or "fact"
        lines.append(f"- [{category}] {memory_item.text}")
    return "\n".join(lines)
