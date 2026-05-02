import pytest
from smolagents import tool

from ouro_agents.cancellation import RunCancellationToken, RunCancelled
from ouro_agents.tools.agent_base import SanitizedToolCallingAgent


class _DummySmolAgent:
    def __init__(self):
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True


class _FakeModel:
    model_id = "fake-model"

    def parse_tool_calls(self, message):
        return message


def test_cancellation_token_interrupts_registered_agents():
    token = RunCancellationToken()
    agent = _DummySmolAgent()

    with token.registered_agent(agent):
        token.cancel("test")

    assert agent.interrupted is True
    with pytest.raises(RunCancelled):
        token.raise_if_cancelled()


def test_sanitized_agent_skips_tool_execution_when_cancelled():
    called = False

    @tool
    def sample_tool() -> str:
        """Return a sample value."""
        nonlocal called
        called = True
        return "ok"

    token = RunCancellationToken()
    agent = SanitizedToolCallingAgent(
        tools=[sample_tool],
        model=_FakeModel(),
        cancellation_token=token,
    )

    token.cancel("test")

    with pytest.raises(RunCancelled):
        agent.execute_tool_call("sample_tool", {})
    assert called is False

