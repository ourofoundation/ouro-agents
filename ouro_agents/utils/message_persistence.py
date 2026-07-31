"""Persistence callbacks that store reasoning and tool-call messages in real time."""

from __future__ import annotations

import json
import logging
from typing import Callable

from smolagents import ActionStep

from .tool_observations import (
    attribute_observation_results,
    tool_call_arguments,
    tool_call_id,
    tool_call_name,
)

logger = logging.getLogger(__name__)

# Match agent tool-output budget so chat UI sees what the model saw, not a
# short preview. Larger MCP payloads are already compacted upstream at 50k.
_MAX_CHAT_TOOL_RESULT_CHARS = 50_000
_CHAT_LIST_RESULT_CAP = 20


def should_persist_tool_call_payload(payload: dict) -> bool:
    # Subagent progress is persisted as its own `subagent` row; the delegate
    # tool call is just the dispatch mechanism behind that UI.
    return str(payload.get("name", "")).lower() != "delegate"


def build_persistence_step_callback(
    ouro_client,
    conversation_id: str,
) -> Callable[[ActionStep], None]:
    """Return a step_callback that persists each tool call as a type='tool_call' message."""
    from ouro.resources.conversations import Messages

    def _callback(step: ActionStep) -> None:
        for payload in extract_tool_call_payloads(step):
            if not should_persist_tool_call_payload(payload):
                continue

            text = f"Called {payload['name']}"
            tool_data = {
                "name": payload["name"],
                "arguments": payload["arguments"],
                "result": payload["result"],
            }
            try:
                Messages(ouro_client).create(
                    conversation_id,
                    type="tool_call",
                    text=text,
                    json=tool_data,
                )
            except Exception:
                logger.warning("Failed to persist tool_call message", exc_info=True)

    return _callback


def _slim_list_envelope_for_chat(obs: str) -> str | None:
    """Re-encode a list envelope so truncation keeps valid JSON + counts.

    Raw mid-string truncation breaks ``JSON.parse`` in the chat UI, which then
    falls back to static titles like "Searched assets" next to siblings that
    still parse as "Found N assets".
    """
    try:
        data = json.loads(obs)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None

    results = data["results"]
    original_count = len(results)
    capped = results[:_CHAT_LIST_RESULT_CAP]
    slim = {
        "results": capped,
        "total": data.get("total", original_count),
        "hasMore": bool(data.get("hasMore")) or original_count > len(capped),
        "shown": len(capped),
        "original_result_count": original_count,
        "truncated": original_count > len(capped),
    }
    for key in ("nextCursor", "limit"):
        if key in data:
            slim[key] = data[key]
    return json.dumps(slim, default=str)


def _slim_markdown_list_for_chat(obs: str) -> str | None:
    """Keep header + first N bullets when MCP list tools return markdown.

    Markdown list tools emit a header (``Found N …``) plus ``- `` bullets and
    optional ``##`` section headings. Cap bullets so chat persistence stays
    under the size budget without a mid-line cut.
    """
    stripped = obs.lstrip()
    if not stripped or stripped[0] in "{[":
        return None
    # Heuristic: list-shaped markdown from ouro-mcp.
    if not (
        stripped.startswith("Found ")
        or stripped.startswith("No ")
        or stripped.startswith("Comments on ")
        or stripped.startswith("Connections for ")
        or "## " in stripped[:200]
    ):
        return None

    lines = obs.splitlines()
    header_lines: list[str] = []
    body_lines: list[str] = []
    bullet_count = 0
    truncated = False

    for line in lines:
        is_bullet = line.startswith("- ")
        is_section = line.startswith("## ")
        if not body_lines and not is_bullet and not is_section:
            header_lines.append(line)
            continue
        if is_bullet:
            if bullet_count >= _CHAT_LIST_RESULT_CAP:
                truncated = True
                continue
            bullet_count += 1
            body_lines.append(line)
            continue
        # Keep continuation/body indent lines and section headers for kept bullets.
        if truncated and is_section:
            # Skip remaining sections once we've hit the cap.
            continue
        if truncated and line.startswith("  "):
            continue
        if truncated:
            continue
        body_lines.append(line)

    if not truncated and len(obs) <= _MAX_CHAT_TOOL_RESULT_CHARS:
        return None

    parts = header_lines + body_lines
    if truncated:
        parts.append(
            f"… [truncated: showing first {_CHAT_LIST_RESULT_CAP} bullets]"
        )
    slim = "\n".join(parts)
    if len(slim) > _MAX_CHAT_TOOL_RESULT_CHARS:
        # Still too big — cut at a line boundary.
        budget = _MAX_CHAT_TOOL_RESULT_CHARS - 40
        cut = slim[:budget]
        last_nl = cut.rfind("\n")
        if last_nl > budget // 2:
            cut = cut[:last_nl]
        slim = cut + "\n… [truncated]"
    return slim


def _truncate_tool_result(obs: str) -> str:
    if len(obs) <= _MAX_CHAT_TOOL_RESULT_CHARS:
        return obs

    slim = _slim_list_envelope_for_chat(obs)
    if slim is not None and len(slim) <= _MAX_CHAT_TOOL_RESULT_CHARS:
        return slim
    if slim is not None:
        # Even the capped envelope is huge — keep metadata only.
        try:
            data = json.loads(slim)
            meta = {
                "results": [],
                "total": data.get("total"),
                "hasMore": data.get("hasMore"),
                "shown": 0,
                "original_result_count": data.get("original_result_count"),
                "truncated": True,
            }
            meta_json = json.dumps(meta, default=str)
            if len(meta_json) <= _MAX_CHAT_TOOL_RESULT_CHARS:
                return meta_json
        except (json.JSONDecodeError, TypeError):
            pass

    md_slim = _slim_markdown_list_for_chat(obs)
    if md_slim is not None:
        return md_slim

    return (
        obs[:_MAX_CHAT_TOOL_RESULT_CHARS]
        + f"\n... [truncated: {len(obs):,} chars total,"
        f" showing first {_MAX_CHAT_TOOL_RESULT_CHARS:,}]"
    )


def extract_tool_call_payloads(step: ActionStep) -> list[dict]:
    if getattr(step, "is_final_answer", False) or step.error:
        return []

    tool_calls = getattr(step, "tool_calls", None) or []
    if not tool_calls:
        return []

    from .tool_observations import get_step_tool_results

    obs = step.observations or ""
    results = attribute_observation_results(
        tool_calls,
        obs,
        per_call=get_step_tool_results(step),
    )
    payloads: list[dict] = []
    for tc, result in zip(tool_calls, results):
        payloads.append(
            {
                "name": tool_call_name(tc),
                "arguments": tool_call_arguments(tc),
                "result": _truncate_tool_result(result),
                "id": tool_call_id(tc) or None,
            }
        )

    return payloads


def build_persistence_reasoning_callback(
    ouro_client,
    conversation_id: str,
) -> Callable[[str], None]:
    """Return a callback that persists reasoning text as a type='reasoning' message."""
    from ouro.resources.conversations import Messages

    def _callback(text: str) -> None:
        if not text or not text.strip():
            return
        try:
            Messages(ouro_client).create(
                conversation_id,
                type="reasoning",
                text=text,
                json={"text": text},
            )
        except Exception:
            logger.warning("Failed to persist reasoning message", exc_info=True)

    return _callback
