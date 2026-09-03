import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressEvent:
    """Typed progress signal for local/remote run observers."""

    phase: str
    message: str = ""
    state: str = "active"
    detail: dict = field(default_factory=dict)


class AgentObserver:
    """Interface for observing the lifecycle of an agent run."""

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        """Called when the agent changes its high-level activity status (e.g., thinking, typing)."""
        pass

    def on_stream_chunk(self, chunk: str) -> None:
        """Deprecated: final text is streamed via ``on_intermediate_chunk``."""
        pass

    def on_result_ready(self, result_text: str) -> None:
        """Called when the agent loop has finished (after the last content step)."""
        pass

    def on_intermediate_chunk(self, message_id: str, chunk: str) -> None:
        """Called when the agent streams a chunk of assistant content.

        Every step — including the last — is a distinct user-visible message
        with its own ``message_id``. Implementations should treat that id as
        stable for the message being progressively streamed.
        """
        pass

    def on_intermediate_end(
        self, message_id: str, full_text: str, turn_final: bool = False
    ) -> None:
        """Called when a step's content stream has finished.

        Fires once per step that emitted any content, after the step
        completes. ``turn_final`` is True for the last step (the user-facing
        reply). Persist the full message and signal end of streaming for
        ``message_id``.
        """
        pass

    def on_intermediate_drop(self, message_id: str) -> None:
        """Close a content stream without persisting it.

        Used when a terminal control tool such as ``no_action`` was accompanied
        by accidental narration that must not become a chat message.
        """
        pass

    def on_step_persist(self, step: dict) -> None:
        """Called when a tool step is completed and should be persisted."""
        pass

    def on_reasoning_persist(self, content: str) -> None:
        """Called when a reasoning block is completed and should be persisted."""
        pass

    def on_reasoning_stream(self, content: str) -> None:
        """Called when a reasoning chunk streams and should be shown in realtime."""
        pass

    def on_progress(self, event: ProgressEvent) -> None:
        """Called when a run enters or completes a typed progress phase."""
        pass


class CompositeAgentObserver(AgentObserver):
    """Fan out agent lifecycle events to multiple observers.

    This keeps transport-specific concerns (websocket publishing, terminal
    progress, persistence) outside the agent while still presenting the single
    observer interface the run loop expects.
    """

    def __init__(self, *observers: Optional[AgentObserver]) -> None:
        self._observers = tuple(observer for observer in observers if observer)

    def _call(self, method_name: str, *args) -> None:
        for observer in self._observers:
            try:
                getattr(observer, method_name)(*args)
            except Exception:
                logger.warning(
                    "%s.%s failed",
                    observer.__class__.__name__,
                    method_name,
                    exc_info=True,
                )

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        self._call("on_activity", status, message, active)

    def on_stream_chunk(self, chunk: str) -> None:
        self._call("on_stream_chunk", chunk)

    def on_result_ready(self, result_text: str) -> None:
        self._call("on_result_ready", result_text)

    def on_intermediate_chunk(self, message_id: str, chunk: str) -> None:
        self._call("on_intermediate_chunk", message_id, chunk)

    def on_intermediate_end(
        self, message_id: str, full_text: str, turn_final: bool = False
    ) -> None:
        self._call("on_intermediate_end", message_id, full_text, turn_final)

    def on_intermediate_drop(self, message_id: str) -> None:
        self._call("on_intermediate_drop", message_id)

    def on_step_persist(self, step: dict) -> None:
        self._call("on_step_persist", step)

    def on_reasoning_persist(self, content: str) -> None:
        self._call("on_reasoning_persist", content)

    def on_reasoning_stream(self, content: str) -> None:
        self._call("on_reasoning_stream", content)

    def on_progress(self, event: ProgressEvent) -> None:
        self._call("on_progress", event)


def emit_progress(
    observer: Optional[AgentObserver],
    phase: str,
    message: str = "",
    *,
    state: str = "active",
    detail: Optional[dict] = None,
) -> None:
    """Safely emit a typed progress event to an optional observer."""
    if observer is None:
        return
    try:
        observer.on_progress(
            ProgressEvent(
                phase=phase,
                message=message,
                state=state,
                detail=detail or {},
            )
        )
    except Exception:
        logger.warning("observer.on_progress failed", exc_info=True)
