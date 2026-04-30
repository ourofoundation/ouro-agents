from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .model import CATEGORIES, SUBJECT_TYPES, MemoryItem, coerce_id_list, content_hash, utc_now

logger = logging.getLogger(__name__)

TEAM_SCOPED_MODES = {"heartbeat", "plan", "review"}


@dataclass
class MemoryValidationError:
    text: str
    reason: str


@dataclass
class MemoryRunContext:
    agent_id: str
    user_id: str = ""
    org_id: str = ""
    conversation_id: str = ""
    run_id: str = ""
    mode: str = ""
    event_type: str = ""
    team_id: str = ""
    available_team_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.team_id:
            self.available_team_ids.add(self.team_id)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _category(value: Any) -> str:
    text = str(value or "fact").strip()
    return text if text in CATEGORIES else "fact"


def _subject_type(value: Any, category: str) -> str:
    text = str(value or "").strip()
    if text in SUBJECT_TYPES:
        return text
    if category in {"learning", "decision", "direction", "observation"}:
        return "agent"
    return "user"


def _filtered_team_ids(candidate_ids: list[str], ctx: MemoryRunContext) -> list[str]:
    if not ctx.available_team_ids:
        return []
    return [team_id for team_id in candidate_ids if team_id in ctx.available_team_ids]


def _fallback_team_ids(subject_type: str, ctx: MemoryRunContext) -> list[str]:
    if not ctx.team_id:
        return []
    mode = ctx.mode.split(":", 1)[0]
    if mode == "chat":
        return [] if subject_type == "user" else [ctx.team_id]
    if mode in TEAM_SCOPED_MODES or mode == "event" or ctx.event_type:
        return [ctx.team_id]
    return []


def validate_memory_candidate(
    candidate: dict[str, Any] | str,
    ctx: MemoryRunContext,
    *,
    source: str = "",
) -> MemoryItem:
    if isinstance(candidate, str):
        candidate = {"text": candidate}

    text = str(candidate.get("text") or "").strip()
    if not text:
        raise ValueError("missing text")

    category = _category(candidate.get("category"))
    subject_type = _subject_type(candidate.get("subject_type"), category)
    subject_id = str(
        candidate.get("subject_id")
        or candidate.get("subject_id_hint")
        or ""
    ).strip()

    team_ids = coerce_id_list(candidate.get("team_ids") or candidate.get("team_id"))
    filtered_team_ids = _filtered_team_ids(team_ids, ctx)
    if team_ids and not filtered_team_ids:
        logger.warning("Dropped unrecognized memory team IDs: %s", ",".join(team_ids))
    team_ids = filtered_team_ids or _fallback_team_ids(subject_type, ctx)

    asset_ids = coerce_id_list(candidate.get("asset_ids") or candidate.get("asset_refs"))

    if subject_type == "user":
        subject_id = subject_id or ctx.user_id
        if not subject_id:
            raise ValueError("subject_type=user requires subject_id or run user_id")
    elif subject_type == "agent":
        if subject_id == "self" or not subject_id:
            subject_id = ctx.agent_id
    elif subject_type == "team":
        if not team_ids:
            raise ValueError("subject_type=team requires a valid team_id")
        subject_id = subject_id or team_ids[0]
    elif subject_type == "asset":
        if not asset_ids:
            raise ValueError("subject_type=asset requires asset_ids")
        subject_id = subject_id or asset_ids[0]
    elif subject_type == "conversation":
        subject_id = subject_id or ctx.conversation_id
        if not subject_id:
            raise ValueError("subject_type=conversation requires conversation_id")
    elif subject_type == "general":
        subject_id = ""

    now = utc_now()
    return MemoryItem(
        text=text,
        subject_type=subject_type,  # type: ignore[arg-type]
        subject_id=subject_id,
        category=category,  # type: ignore[arg-type]
        team_ids=team_ids,
        asset_ids=asset_ids,
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        conversation_id=ctx.conversation_id,
        run_id=ctx.run_id,
        mode=ctx.mode,
        event_type=ctx.event_type,
        source=source,
        importance=max(0.0, min(1.0, _as_float(candidate.get("importance"), 0.5))),
        confidence=max(0.0, min(1.0, _as_float(candidate.get("confidence"), 0.7))),
        content_hash=content_hash(text),
        schema_version=2,
        created_at=now,
    )


def validate_memory_candidates(
    candidates: list[dict[str, Any] | str],
    ctx: MemoryRunContext,
    *,
    source: str = "",
) -> tuple[list[MemoryItem], list[MemoryValidationError]]:
    items: list[MemoryItem] = []
    errors: list[MemoryValidationError] = []
    for candidate in candidates:
        try:
            items.append(validate_memory_candidate(candidate, ctx, source=source))
        except ValueError as exc:
            text = candidate if isinstance(candidate, str) else candidate.get("text", "")
            errors.append(MemoryValidationError(text=str(text), reason=str(exc)))
    return items, errors
