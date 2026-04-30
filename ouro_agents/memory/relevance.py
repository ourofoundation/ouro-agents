"""Relevance policy for deciding which memories should steer attention."""

from __future__ import annotations

from typing import Any


_AMBIENT_SOURCE_HINTS = (
    "feed",
    "search",
    "asset-discovery",
    "team-feed",
    "notification",
)

_DIRECTIVE_SOURCE_HINTS = (
    "human",
    "plan-feedback",
    "review-feedback",
    "comment",
    "conversation",
    "planning",
)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text_attr(memory: Any, name: str, default: str = "") -> str:
    return str(getattr(memory, name, default) or default).strip()


def _list_attr(memory: Any, name: str) -> list[str]:
    value = getattr(memory, name, None)
    if not value and hasattr(memory, "metadata"):
        value = getattr(memory, "metadata", {}).get(name)
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


def _source_has(source: str, hints: tuple[str, ...]) -> bool:
    lowered = source.lower()
    return any(hint in lowered for hint in hints)


def memory_signal_score(
    memory: Any,
    *,
    team_id: str = "",
    explicit_filter: bool = False,
) -> float:
    """Score a memory by task usefulness, not just vector similarity.

    Semantic similarity still matters, but authority matters more: explicit
    direction, decisions, user preferences, and same-team context should outrank
    ambient platform observations unless the caller asked for that asset/team.
    """

    category = _text_attr(memory, "category", "general")
    subject_type = _text_attr(memory, "subject_type", "general")
    source = _text_attr(memory, "source", "")
    score = _as_float(getattr(memory, "score", 0.0), 0.0)
    importance = _as_float(getattr(memory, "importance", 0.5), 0.5)
    confidence = _as_float(getattr(memory, "confidence", 0.7), 0.7)
    team_ids = _list_attr(memory, "team_ids")
    asset_ids = _list_attr(memory, "asset_ids")

    category_weight = {
        "direction": 1.4,
        "decision": 1.05,
        "preference": 0.85,
        "learning": 0.45,
        "fact": 0.3,
        "general": 0.0,
        "observation": -0.35,
    }.get(category, 0.0)

    subject_weight = {
        "user": 0.45,
        "agent": 0.25,
        "conversation": 0.2,
        "team": 0.1,
        "general": 0.0,
        "asset": -0.45,
    }.get(subject_type, 0.0)

    total = 0.0
    total += min(max(score, 0.0), 1.0) * 0.9
    total += importance * 0.45
    total += confidence * 0.35
    total += category_weight + subject_weight

    if team_id and team_id in team_ids:
        total += 0.35

    if _source_has(source, _DIRECTIVE_SOURCE_HINTS):
        total += 0.35

    is_ambient = (
        category == "observation"
        or subject_type == "asset"
        or _source_has(source, _AMBIENT_SOURCE_HINTS)
        or (asset_ids and category not in {"direction", "decision"})
    )
    if is_ambient and not explicit_filter:
        total -= 0.9

    return total


def is_focus_directive(memory: Any) -> bool:
    """Return whether a memory is allowed to steer planning focus."""

    category = _text_attr(memory, "category", "general")
    subject_type = _text_attr(memory, "subject_type", "general")
    source = _text_attr(memory, "source", "")
    confidence = _as_float(getattr(memory, "confidence", 0.7), 0.7)
    importance = _as_float(getattr(memory, "importance", 0.5), 0.5)

    if subject_type == "asset" or _source_has(source, _AMBIENT_SOURCE_HINTS):
        return False

    if category == "direction":
        return True

    if category == "decision":
        if _source_has(source, _DIRECTIVE_SOURCE_HINTS):
            return True
        return confidence >= 0.65 and importance >= 0.6

    return False
