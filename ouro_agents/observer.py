from typing import Optional

class AgentObserver:
    """Interface for observing the lifecycle of an agent run."""

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        """Called when the agent changes its high-level activity status (e.g., thinking, typing)."""
        pass

    def on_stream_chunk(self, chunk: str) -> None:
        """Called when the agent streams a chunk of its final answer."""
        pass

    def on_result_ready(self, result_text: str) -> None:
        """Called when the agent has completed its final answer."""
        pass

    def on_intermediate_chunk(self, message_id: str, chunk: str) -> None:
        """Called when the agent streams a chunk of intermediate (commentary) content.

        Intermediate content is the assistant text emitted alongside non-final
        tool calls — e.g., "Looking at recent quests first." or "Found three
        candidates." It is conceptually a separate user-visible message per
        step, so each step has its own ``message_id``. Implementations should
        treat ``message_id`` as a stable identifier for the message being
        progressively streamed.
        """
        pass

    def on_intermediate_end(self, message_id: str, full_text: str) -> None:
        """Called when a step's intermediate content stream has finished.

        Fires once per step that emitted any commentary, after the step
        completes. Use this hook to persist the full message and signal the
        end of streaming for that ``message_id``.
        """
        pass

    def on_step_persist(self, step: dict) -> None:
        """Called when a tool step is completed and should be persisted."""
        pass

    def on_reasoning_persist(self, content: str) -> None:
        """Called when a reasoning block is completed and should be persisted."""
        pass
