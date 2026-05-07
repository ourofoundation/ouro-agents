"""Small helpers for local/remote reconciliation code.

The sync callers still own their domain rules.  This module only centralizes
the recurring mechanics: dict-or-model field reads, status string normalization,
and timestamp-based push/pull decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


SyncAction = Literal["push", "pull", "unchanged"]


@dataclass(frozen=True)
class SyncDecision:
    action: SyncAction
    reason: str


def read_field(obj: object, path: str, default=None):
    """Read a dotted field path from a dict or model-like object."""
    current = obj
    for part in path.split("."):
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(part, default)
        else:
            current = getattr(current, part, default)
    return current


def normalize_status(
    value: object,
    *,
    aliases: dict[str, str] | None = None,
    default: str = "",
) -> str:
    """Normalize a remote status string with caller-provided aliases."""
    status = str(value or default).strip().lower().replace("-", "_")
    if not status:
        return default
    return (aliases or {}).get(status, status)


def ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def choose_timestamp_sync_action(
    *,
    local_body: str,
    remote_body: str,
    local_ts: datetime | None,
    remote_ts: datetime | None,
) -> SyncDecision:
    """Choose push/pull/unchanged using body presence and timestamps."""
    local_body = local_body.strip()
    remote_body = remote_body.strip()
    if not local_body and not remote_body:
        return SyncDecision("unchanged", "both-empty")

    if local_ts and remote_ts:
        local_aware = ensure_utc(local_ts)
        remote_aware = ensure_utc(remote_ts)
        if local_aware > remote_aware:
            return SyncDecision("push", "local-newer")
        if remote_aware > local_aware:
            return SyncDecision("pull", "remote-newer")
        return SyncDecision("unchanged", "timestamps-equal")

    if local_body and not remote_body:
        return SyncDecision("push", "local-only")
    if remote_body and not local_body:
        return SyncDecision("pull", "remote-only")
    if local_body and not local_ts and remote_ts:
        return SyncDecision("push", "local-missing-timestamp")
    if remote_body and not remote_ts and local_ts:
        return SyncDecision("pull", "remote-missing-timestamp")
    return SyncDecision("push", "default-local-wins")
