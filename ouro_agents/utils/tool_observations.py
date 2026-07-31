"""Attribute concatenated parallel tool observations to individual tool calls.

smolagents concatenates parallel tool results into one ``ActionStep.observations``
blob. ``OuroToolCallingAgent.process_tool_calls`` labels each section with
``=== Tool result: <name> (id=<id>) ===`` so callers can split them back apart.

It also stores a per-call map on the ActionStep (``ouro_tool_results``) so chat
persistence can attribute results even after a step-level spill rewrites the
combined observation blob.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

TOOL_RESULT_HEADER_RE = re.compile(
    r"^=== Tool result: (?P<name>.+?) \(id=(?P<id>[^)]*)\) ===\s*$",
    re.MULTILINE,
)

# Side-channel on ActionStep: {tool_call_id: result_body} captured at execution
# time, before any step-level observation rewrite.
STEP_TOOL_RESULTS_ATTR = "ouro_tool_results"

_COMBINED_RESULT_PLACEHOLDER = (
    "(parallel step — this call's result is on its own tool card when available; "
    "see the labeled observation or the first tool call in this step)"
)


def format_tool_result_header(name: str, call_id: str) -> str:
    return f"=== Tool result: {name} (id={call_id}) ==="


def strip_tool_result_header(observation: str) -> str:
    """Remove a leading tool-result header if present."""
    text = observation or ""
    match = TOOL_RESULT_HEADER_RE.match(text.lstrip("\n"))
    if not match:
        return text
    # Match may be against lstrip'd text; find it on the original.
    raw_match = TOOL_RESULT_HEADER_RE.search(text)
    if not raw_match:
        return text
    return text[raw_match.end() :].lstrip("\n")


def set_step_tool_results(step: Any, results: dict[str, str]) -> None:
    """Attach per-call results to an ActionStep for later attribution."""
    if step is None:
        return
    setattr(step, STEP_TOOL_RESULTS_ATTR, dict(results))


def get_step_tool_results(step: Any) -> Optional[dict[str, str]]:
    raw = getattr(step, STEP_TOOL_RESULTS_ATTR, None) if step is not None else None
    if not isinstance(raw, dict) or not raw:
        return None
    return {str(k): ("" if v is None else str(v)) for k, v in raw.items() if k}


def split_labeled_observation_sections(
    observation: Optional[str],
) -> Optional[list[tuple[str, str, str]]]:
    """Parse labeled blob into ``[(name, call_id, body), ...]``.

    Returns ``None`` when the blob is unlabeled.
    """
    if not observation or not observation.lstrip().startswith("=== Tool result:"):
        return None
    matches = list(TOOL_RESULT_HEADER_RE.finditer(observation))
    if not matches:
        return None
    sections: list[tuple[str, str, str]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(observation)
        body = observation[start:end].strip("\n")
        sections.append((match.group("name"), match.group("id") or "", body))
    return sections or None


def split_labeled_observations(
    observation: Optional[str],
) -> Optional[dict[str, str]]:
    """Parse per-call bodies from a labeled parallel observation blob.

    When ``=== Tool result: … ===`` headers are present, return
    ``{call_id: body}``. Returns ``None`` for unlabeled blobs (legacy /
    single-call / error steps) so callers can fall back.
    """
    sections = split_labeled_observation_sections(observation)
    if sections is None:
        return None
    by_id = {call_id: body for _, call_id, body in sections if call_id}
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
    *,
    per_call: Optional[dict[str, str]] = None,
) -> list[str]:
    """Return one result string per tool call.

    Preference order:
    1. ``per_call`` map (captured at tool-execution time on the ActionStep)
    2. Labeled observation headers (``=== Tool result: … ===``)
    3. Legacy fallback: full blob on the first call, placeholder on the rest
    """
    calls = list(tool_calls or [])
    obs = observation or ""
    if not calls:
        return []

    stored = {str(k): ("" if v is None else str(v)) for k, v in (per_call or {}).items() if k}
    by_id = split_labeled_observations(obs)
    results: list[str] = []
    for idx, tc in enumerate(calls):
        call_id = tool_call_id(tc)
        if call_id and call_id in stored:
            results.append(stored[call_id])
        elif by_id is not None and call_id and call_id in by_id:
            results.append(by_id[call_id])
        elif by_id is not None or stored:
            # Partial map — don't steal another call's body.
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
