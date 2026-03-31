"""Persistence callbacks that store reasoning and tool-call messages in real time."""

from __future__ import annotations

import json
import logging
from typing import Callable

from smolagents import ActionStep

logger = logging.getLogger(__name__)


def _tiptap_text_doc(text: str) -> dict:
    """Build a minimal TipTap JSON doc wrapping plain text."""
    paragraphs = text.split("\n") if text else [""]
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}] if line else [],
            }
            for line in paragraphs
        ],
    }


def build_persistence_step_callback(
    ouro_client,
    conversation_id: str,
) -> Callable[[ActionStep], None]:
    """Return a step_callback that persists each tool call as a type='tool_call' message."""
    from ouro.resources.conversations import Messages

    def _callback(step: ActionStep) -> None:
        if getattr(step, "is_final_answer", False) or step.error:
            return
        for tc in getattr(step, "tool_calls", None) or []:
            if isinstance(tc, dict):
                if "function" in tc:
                    name = tc["function"].get("name", "unknown")
                    args = tc["function"].get("arguments", {})
                else:
                    name = tc.get("name", "unknown")
                    args = tc.get("arguments", {})
            elif hasattr(tc, "function") and tc.function is not None:
                name = getattr(tc.function, "name", "unknown")
                args = getattr(tc.function, "arguments", {})
            else:
                name = getattr(tc, "name", "unknown")
                args = getattr(tc, "arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": args}
            obs = step.observations or ""
            text = f"Called {name}"
            tool_data = {
                "name": name,
                "arguments": args,
                "result": obs[:4000],
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
