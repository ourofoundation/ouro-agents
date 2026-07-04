from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

MemorySubjectType = Literal[
    "user",
    "agent",
    "team",
    "asset",
    "conversation",
    "org",
    "general",
]
MemoryCategory = Literal[
    "direction",
    "fact",
    "preference",
    "episode",
]
MemoryBasis = Literal["stated", "inferred", "observed"]
MemoryStability = Literal["stable", "evolving"]

SUBJECT_TYPES = {
    "user",
    "agent",
    "team",
    "asset",
    "conversation",
    "org",
    "general",
}
CATEGORIES = {
    "direction",
    "fact",
    "preference",
    "episode",
}
BASIS_VALUES = {"stated", "inferred", "observed"}
STABILITY_VALUES = {"stable", "evolving"}
LEGACY_CATEGORY_MAP = {
    "decision": "direction",
    "learning": "fact",
    "observation": "episode",
    "general": "fact",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(text: str) -> str:
    return hashlib.sha1(text.lower().strip().encode("utf-8")).hexdigest()


EMPTY_CONTENT_HASH = content_hash("")


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def coerce_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]

    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part).strip().strip(",")
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def encode_id_index(ids: list[str]) -> str:
    cleaned = coerce_id_list(ids)
    return f",{','.join(cleaned)}," if cleaned else ""


class MemoryItem(BaseModel):
    text: str
    subject_type: MemorySubjectType = "general"
    subject_id: str = ""
    category: MemoryCategory = "fact"
    basis: MemoryBasis = "inferred"
    stability: MemoryStability = "stable"
    team_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    user_id: str = ""
    org_id: str = ""
    conversation_id: str = ""
    run_id: str = ""
    source: str = ""
    strength: float = 0.5
    verification_hint: str = ""
    # IDs of existing memories this item contradicts or replaces. An
    # instruction to the storage pipeline, not a persisted property.
    supersedes: list[str] = Field(default_factory=list)
    content_hash: str = ""
    schema_version: int = 3
    created_at: datetime = Field(default_factory=utc_now)
    last_accessed: datetime | None = None
    last_verified: datetime | None = None


def _safe_category(value: Any, default: str = "fact") -> str:
    text = str(value or default).strip()
    text = LEGACY_CATEGORY_MAP.get(text, text)
    return text if text in CATEGORIES else default


def _safe_basis(value: Any, default: str = "inferred") -> str:
    text = str(value or default).strip()
    return text if text in BASIS_VALUES else default


def _safe_stability(value: Any, default: str = "stable") -> str:
    text = str(value or default).strip()
    return text if text in STABILITY_VALUES else default


def _safe_subject_type(value: Any, default: str = "general") -> str:
    text = str(value or default).strip()
    return text if text in SUBJECT_TYPES else default


def to_metadata(item: MemoryItem) -> dict[str, str | float | int]:
    team_ids = coerce_id_list(item.team_ids)
    asset_ids = coerce_id_list(item.asset_ids)
    metadata: dict[str, str | float | int] = {
        "subject_type": item.subject_type,
        "subject_id": item.subject_id,
        "category": item.category,
        "team_ids": ",".join(team_ids),
        "team_ids_idx": encode_id_index(team_ids),
        "asset_ids": ",".join(asset_ids),
        "asset_ids_idx": encode_id_index(asset_ids),
        "user_id": item.user_id,
        "org_id": item.org_id,
        "conversation_id": item.conversation_id,
        "run_id": item.run_id,
        "source": item.source,
        "basis": item.basis,
        "stability": item.stability,
        "strength": float(item.strength),
        "verification_hint": item.verification_hint if item.stability == "evolving" else "",
        "content_hash": item.content_hash or content_hash(item.text),
        "schema_version": 3,
        "created_at": item.created_at.isoformat(),
    }
    if item.last_accessed:
        metadata["last_accessed"] = item.last_accessed.isoformat()
    if item.last_verified:
        metadata["last_verified"] = item.last_verified.isoformat()

    return metadata


def memory_item_from_raw(text: str, raw_metadata: dict[str, Any] | None = None) -> MemoryItem:
    meta = dict(raw_metadata or {})
    team_ids = coerce_id_list(meta.get("team_ids") or meta.get("team_id"))
    asset_ids = coerce_id_list(meta.get("asset_ids") or meta.get("asset_refs"))
    category = _safe_category(meta.get("category"), "fact")
    user_id = str(meta.get("user_id") or "")
    conversation_id = str(meta.get("conversation_id") or "")

    if meta.get("subject_type"):
        subject_type = _safe_subject_type(meta.get("subject_type"))
    elif category == "preference" and user_id:
        subject_type = "user"
    elif asset_ids:
        subject_type = "asset"
    else:
        subject_type = "agent"

    subject_id = str(meta.get("subject_id") or "")
    if not subject_id:
        if subject_type == "user":
            subject_id = user_id
        elif subject_type == "team" and team_ids:
            subject_id = team_ids[0]
        elif subject_type == "asset" and asset_ids:
            subject_id = asset_ids[0]
        elif subject_type == "conversation":
            subject_id = conversation_id

    created_at = parse_timestamp(meta.get("created_at")) or utc_now()
    last_accessed = parse_timestamp(meta.get("last_accessed"))
    last_verified = parse_timestamp(meta.get("last_verified"))

    try:
        schema_version = int(meta.get("schema_version") or 1)
    except (TypeError, ValueError):
        schema_version = 1

    basis = _safe_basis(meta.get("basis"))
    if "basis" not in meta and "confidence" in meta:
        try:
            basis = "stated" if float(meta.get("confidence") or 0.0) >= 0.9 else "inferred"
        except (TypeError, ValueError):
            basis = "inferred"

    stability = _safe_stability(meta.get("stability"))
    if "stability" not in meta and "volatility" in meta:
        try:
            stability = "evolving" if float(meta.get("volatility") or 0.0) > 0.0 else "stable"
        except (TypeError, ValueError):
            stability = "stable"

    try:
        strength = float(meta.get("strength", meta.get("importance", 0.5)) or 0.5)
    except (TypeError, ValueError):
        strength = 0.5
    strength = max(0.0, min(1.0, strength))

    resolved_content_hash = str(meta.get("content_hash") or content_hash(text))
    if text.strip() and resolved_content_hash == EMPTY_CONTENT_HASH:
        resolved_content_hash = content_hash(text)

    return MemoryItem(
        text=text,
        subject_type=subject_type,  # type: ignore[arg-type]
        subject_id=subject_id,
        category=category,  # type: ignore[arg-type]
        basis=basis,  # type: ignore[arg-type]
        stability=stability,  # type: ignore[arg-type]
        team_ids=team_ids,
        asset_ids=asset_ids,
        user_id=user_id,
        org_id=str(meta.get("org_id") or ""),
        conversation_id=conversation_id,
        run_id=str(meta.get("run_id") or ""),
        source=str(meta.get("source") or ""),
        strength=strength,
        verification_hint=str(meta.get("verification_hint") or "") if stability == "evolving" else "",
        content_hash=resolved_content_hash,
        schema_version=schema_version,
        created_at=created_at,
        last_accessed=last_accessed,
        last_verified=last_verified,
    )
