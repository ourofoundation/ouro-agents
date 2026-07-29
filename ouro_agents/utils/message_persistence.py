"""Persistence callbacks that store reasoning and tool-call messages in real time."""

from __future__ import annotations

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


def _truncate_tool_result(obs: str) -> str:
    if len(obs) <= _MAX_CHAT_TOOL_RESULT_CHARS:
        return obs
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

    obs = step.observations or ""
    results = attribute_observation_results(tool_calls, obs)
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
