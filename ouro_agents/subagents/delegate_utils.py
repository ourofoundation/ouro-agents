"""Shared helpers for shaping delegate/subagent responses."""

from __future__ import annotations

from typing import Any, Optional

_DELEGATE_RETURN_MODES = {"summary_only", "full_text", "auto"}


def normalize_return_mode(
    value: Optional[str],
    default: str = "summary_only",
) -> str:
    mode = (value or default or "summary_only").strip()
    return mode if mode in _DELEGATE_RETURN_MODES else default


def summarize_delegate_text(text: str, max_chars: int = 700) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned

    cutoff = cleaned.rfind("\n\n", 0, max_chars)
    if cutoff < max_chars // 3:
        cutoff = cleaned.rfind("\n", 0, max_chars)
    if cutoff < max_chars // 3:
        cutoff = max_chars
    return cleaned[:cutoff].rstrip() + "\n[...truncated]"


def resolve_auto_return_mode(mode: str, *, has_asset: bool) -> str:
    if mode != "auto":
        return mode
    return "summary_only" if has_asset else "full_text"


def delegate_error_payload(
    subagent: str,
    mode: str,
    error: str,
    *,
    available: Optional[list[str]] = None,
) -> dict:
    payload = {
        "status": "error",
        "subagent": subagent,
        "return_mode": mode,
        "error": error,
    }
    if available:
        payload["available"] = available
    return payload


def validate_delegate_result(
    result: Any,
    subagent: str,
    mode: str,
    *,
    available: Optional[list[str]] = None,
) -> Optional[dict]:
    if result is None:
        return delegate_error_payload(
            subagent,
            mode,
            f"Unknown subagent '{subagent}'",
            available=available,
        )
    if not result.success:
        return delegate_error_payload(
            subagent,
            mode,
            f"Subagent '{subagent}' failed: {result.error or 'unknown error'}",
        )
    if not result.text:
        return delegate_error_payload(
            subagent,
            mode,
            f"Subagent '{subagent}' returned no result.",
        )
    return None


def delegate_success_payload(
    result: Any,
    subagent: str,
    mode: str,
    summary: str,
) -> dict:
    payload: dict[str, Any] = {
        "status": "ok",
        "subagent": subagent,
        "return_mode": mode,
        "summary": result.asset_description or summary,
    }
    if result.asset_id:
        asset_type = result.asset_type or "post"
        name = result.asset_name or ""
        payload["asset_id"] = result.asset_id
        payload["asset_type"] = asset_type
        payload["name"] = name
        payload["description"] = result.asset_description or ""
        if result.asset_visibility:
            payload["visibility"] = result.asset_visibility
        # The subagent already created and published this asset on Ouro.
        # Hand the parent a drop-in reference and an explicit instruction so it
        # surfaces the existing asset instead of recreating or republishing it.
        payload["link"] = f"[{name or asset_type}]({asset_type}:{result.asset_id})"
        payload["next_step"] = (
            f"This {asset_type} already exists and is published on Ouro. "
            "Reference it by embedding or linking the asset_id above in your reply — "
            "do NOT create another asset or paste its full body. "
            "Call get_asset(asset_id) only if you need the full content."
        )
    if result.usage.total_tokens:
        payload["tokens_used"] = result.usage.total_tokens
    if mode == "full_text":
        payload["text"] = result.text
    return payload
