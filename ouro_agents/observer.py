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

    def on_step_persist(self, step: dict) -> None:
        """Called when a tool step is completed and should be persisted."""
        pass

    def on_reasoning_persist(self, content: str) -> None:
        """Called when a reasoning block is completed and should be persisted."""
        pass
