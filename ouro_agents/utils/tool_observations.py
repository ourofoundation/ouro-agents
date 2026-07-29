"""Attribute concatenated parallel tool observations to individual tool calls.

smolagents concatenates parallel tool results into one ``ActionStep.observations``
blob. ``OuroToolCallingAgent.process_tool_calls`` labels each section with
``=== Tool result: <name> (id=<id>) ===`` so callers can split them back apart.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

TOOL_RESULT_HEADER_RE = re.compile(
    r"^=== Tool result: (?P<name>.+?) \(id=(?P<id>[^)]*)\) ===\s*$",
    re.MULTILINE,
)

_COMBINED_RESULT_PLACEHOLDER = "(result included with the first tool call above)"


def split_labeled_observations(
    observation: Optional[str],
) -> Optional[dict[str, str]]:
    """Parse per-call bodies from a labeled parallel observation blob.

    When ``=== Tool result: … ===`` headers are present, return
    ``{call_id: body}``. Returns ``None`` for unlabeled blobs (legacy /
    single-call / error steps) so callers can fall back.
    """
    if not observation or not observation.lstrip().startswith("=== Tool result:"):
        return None
    matches = list(TOOL_RESULT_HEADER_RE.finditer(observation))
    if not matches:
        return None
    by_id: dict[str, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(observation)
        body = observation[start:end].strip("\n")
        call_id = match.group("id")
        if call_id:
            by_id[call_id] = body
    return by_id or None


def tool_call_id(tc: Any) -> str:
    """Best-effort id from a smolagents ToolCall, ChatMessageToolCall, or dict."""
    if isinstance(tc, dict):
        if "id" in tc and tc["id"]:
            return str(tc["id"])
        fn = tc.get("function")
        if isinstance(fn, dict) and fn.get("id"):
            return str(fn["id"])
        return ""
    return str(getattr(tc, "id", "") or "")


def tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        if "function" in tc and isinstance(tc["function"], dict):
            return tc["function"].get("name", "unknown") or "unknown"
        return tc.get("name", "unknown") or "unknown"
    if hasattr(tc, "function") and tc.function is not None:
        return getattr(tc.function, "name", "unknown") or "unknown"
    return getattr(tc, "name", "unknown") or "unknown"


def tool_call_arguments(tc: Any) -> Any:
    if isinstance(tc, dict):
        if "function" in tc and isinstance(tc["function"], dict):
            args = tc["function"].get("arguments", {})
        else:
            args = tc.get("arguments", tc.get("args", {}))
    elif hasattr(tc, "function") and tc.function is not None:
        args = getattr(tc.function, "arguments", {})
    else:
        args = getattr(tc, "arguments", getattr(tc, "args", {}))

    if isinstance(args, str):
        try:
            return json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return {"raw": args}
    return args if args is not None else {}


def attribute_observation_results(
    tool_calls: list[Any] | tuple[Any, ...] | None,
    observation: Optional[str],
) -> list[str]:
    """Return one result string per tool call, split from a labeled blob when possible.

    Matching is by ``tool_call_id`` (completion order is not call order under
    parallel execution). Unlabeled single-call steps get the full observation.
    Unlabeled multi-call steps keep the legacy fallback: full blob on the first
    call, placeholder on the rest.
    """
    calls = list(tool_calls or [])
    obs = observation or ""
    if not calls:
        return []

    by_id = split_labeled_observations(obs)
    results: list[str] = []
    for idx, tc in enumerate(calls):
        call_id = tool_call_id(tc)
        if by_id is not None and call_id and call_id in by_id:
            results.append(by_id[call_id])
        elif by_id is not None:
            # Labeled blob but this call's id is missing — do not steal another
            # call's body by falling back to the full concatenated observation.
            results.append("(no result attributed to this tool call)")
        elif len(calls) == 1:
            results.append(obs)
        elif idx == 0:
            results.append(obs)
        elif obs:
            results.append(_COMBINED_RESULT_PLACEHOLDER)
        else:
            results.append("")
    return results
