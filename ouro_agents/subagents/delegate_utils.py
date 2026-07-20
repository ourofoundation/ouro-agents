"""Shared helpers for shaping delegate/subagent responses."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

_DELEGATE_RETURN_MODES = {"summary_only", "full_text", "auto"}

logger = logging.getLogger(__name__)


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


def default_delegate_error_payload(
    spec: dict,
    exc: BaseException,
) -> dict:
    """Standard error shape when a single delegate task raises."""
    return {
        "status": "error",
        "subagent": spec.get("subagent", "?"),
        "return_mode": normalize_return_mode(spec.get("return_mode", "")),
        "error": str(exc),
    }


def dispatch_delegate_tasks(
    tasks: list[dict],
    run_one: Callable[[dict], dict],
    *,
    parallel: bool = False,
    max_workers: int = 4,
    make_error: Callable[[dict, BaseException], dict] | None = None,
) -> list[Any]:
    """Run delegate task specs sequentially or in parallel.

    *run_one* should return a payload dict for one spec. Exceptions are caught
    and converted via *make_error* (defaults to :func:`default_delegate_error_payload`).
    """
    if not tasks:
        return []

    error_fn = make_error or default_delegate_error_payload

    if len(tasks) == 1 or not parallel:
        outputs: list[Any] = []
        for spec in tasks:
            try:
                outputs.append(run_one(spec))
            except Exception as exc:
                logger.exception(
                    "Delegate '%s' failed", spec.get("subagent", "?")
                )
                outputs.append(error_fn(spec, exc))
        return outputs

    outputs = [None] * len(tasks)
    workers = max(1, min(max_workers, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(run_one, spec): i for i, spec in enumerate(tasks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                outputs[idx] = future.result()
            except Exception as exc:
                outputs[idx] = error_fn(tasks[idx], exc)
    return outputs


def dumps_delegate_result(tasks: list[dict], outputs: list[Any]) -> str:
    """JSON-encode a single delegate result or a multi-task list."""
    if len(tasks) == 1:
        return json.dumps(outputs[0] if outputs else {"status": "error", "error": "No tasks provided."})
    return json.dumps(outputs)
