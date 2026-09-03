from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ouro.resources.conversations import Messages
from ouro_mcp.utils import content_from_markdown

from ..observer import AgentObserver
from ..tools.agent_base import extract_terminal_no_action
from ..utils.message_persistence import (
    build_persistence_reasoning_callback,
    build_persistence_step_callback,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TUIAgentEvent:
    kind: str
    text: str = ""
    message_id: str = ""
    active: bool = False
    payload: Any = None


EventSink = Callable[[TUIAgentEvent], None]


class TUIObserver(AgentObserver):
    """Bridge an in-process agent run to the Textual UI and optional Ouro writes."""

    def __init__(
        self,
        *,
        emit: EventSink,
        agent_client=None,
        conversation_id: str | None = None,
        stream_message_id: str | None = None,
    ) -> None:
        self.emit = emit
        self.agent_client = agent_client
        self.conversation_id = conversation_id
        self.stream_message_id = stream_message_id
        self._final_text_parts: list[str] = []
        self._intermediate_text: dict[str, list[str]] = {}
        self._turn_final_persisted = False
        self._persist_step_cb = (
            build_persistence_step_callback(agent_client, conversation_id)
            if agent_client and conversation_id
            else None
        )
        self._persist_reasoning_cb = (
            build_persistence_reasoning_callback(agent_client, conversation_id)
            if agent_client and conversation_id
            else None
        )

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        detail = f"{status}: {message}" if message else status
        self.emit(TUIAgentEvent("activity", text=detail, active=active))

    def on_stream_chunk(self, chunk: str) -> None:
        self._final_text_parts.append(chunk)
        self.emit(
            TUIAgentEvent(
                "stream",
                text=chunk,
                message_id=self.stream_message_id or "",
            )
        )

    def on_intermediate_chunk(self, message_id: str, chunk: str) -> None:
        self._intermediate_text.setdefault(message_id, []).append(chunk)
        self.emit(TUIAgentEvent("intermediate", text=chunk, message_id=message_id))

    def on_intermediate_end(
        self, message_id: str, full_text: str, turn_final: bool = False
    ) -> None:
        text = (full_text or "".join(self._intermediate_text.get(message_id, []))).strip()
        if turn_final and extract_terminal_no_action(text) is not None:
            self._turn_final_persisted = True
            self.emit(TUIAgentEvent("intermediate_end", text=text, message_id=message_id))
            return
        if text and self.agent_client and self.conversation_id:
            try:
                content = content_from_markdown(self.agent_client, text)
                Messages(self.agent_client).create(
                    self.conversation_id,
                    id=message_id,
                    type="message",
                    text=content.text,
                    json=content.json,
                    metadata={"turn_final": turn_final},
                )
                if turn_final:
                    self._turn_final_persisted = True
            except Exception:
                logger.warning("Failed to persist intermediate content", exc_info=True)
        self.emit(TUIAgentEvent("intermediate_end", text=text, message_id=message_id))

    def on_intermediate_drop(self, message_id: str) -> None:
        self._intermediate_text.pop(message_id, None)
        self._turn_final_persisted = True
        self.emit(TUIAgentEvent("intermediate_end", text="", message_id=message_id))

    def on_result_ready(self, result_text: str) -> None:
        if self._turn_final_persisted:
            text = result_text or "".join(self._final_text_parts)
            self.emit(TUIAgentEvent("result", text=text, payload=None))
            return
        text = result_text or "".join(self._final_text_parts)
        message = None
        if (
            text
            and extract_terminal_no_action(text) is None
            and self.agent_client
            and self.conversation_id
        ):
            try:
                content = content_from_markdown(self.agent_client, text)
                message = Messages(self.agent_client).create(
                    self.conversation_id,
                    id=self.stream_message_id,
                    type="message",
                    text=content.text,
                    json=content.json,
                )
            except Exception:
                logger.warning("Failed to persist final answer", exc_info=True)
        self.emit(TUIAgentEvent("result", text=text, payload=message))

    def on_step_persist(self, step: dict) -> None:
        self.emit(TUIAgentEvent("step", text=_step_summary(step), payload=step))
        if self._persist_step_cb:
            self._persist_step_cb(step)

    def on_reasoning_persist(self, content: str) -> None:
        self.emit(TUIAgentEvent("reasoning", text=content))
        if self._persist_reasoning_cb:
            self._persist_reasoning_cb(content)


def _step_summary(step: Any) -> str:
    tool_calls = getattr(step, "tool_calls", None) or []
    names: list[str] = []
    for call in tool_calls:
        if isinstance(call, dict):
            if isinstance(call.get("function"), dict):
                names.append(str(call["function"].get("name", "tool")))
            else:
                names.append(str(call.get("name", "tool")))
        elif getattr(call, "function", None) is not None:
            names.append(str(getattr(call.function, "name", "tool")))
        else:
            names.append(str(getattr(call, "name", "tool")))
    if names:
        return "called " + ", ".join(names)
    if getattr(step, "error", None):
        return f"error: {step.error}"
    return "completed step"

