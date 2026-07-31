"""Overlapping chat runs: newer message supersedes the in-flight reply."""

from __future__ import annotations

from ouro_agents.cancellation import RunCancellationToken
from ouro_agents import server
from ouro_agents.run_context import ActiveRunRegistry, RunContext


class _FakeAgent:
    def __init__(self):
        self._active_runs = ActiveRunRegistry()


def test_supersede_cancels_prior_active_chat_token():
    server.active_chat_tokens.clear()
    server.agent_instance = None

    prior = RunCancellationToken()
    server.active_chat_tokens["conv-a"] = prior
    newer = RunCancellationToken()

    cancelled = server._supersede_prior_chat_runs("conv-a", newer)

    assert cancelled == 1
    assert prior.cancelled
    assert "superseded" in (prior.reason or "")
    assert server.active_chat_tokens["conv-a"] is newer
    assert not newer.cancelled


def test_supersede_also_cancels_registry_tokens_for_conversation():
    server.active_chat_tokens.clear()
    agent = _FakeAgent()
    server.agent_instance = agent

    prior = RunCancellationToken()
    registry_token = RunCancellationToken()
    server.active_chat_tokens["conv-b"] = prior
    agent._active_runs.register(
        RunContext(
            run_id="r-old",
            mode="chat",
            event_type="new-message",
            conversation_id="conv-b",
            team_id=None,
            task_preview="old",
        ),
        registry_token,
    )

    newer = RunCancellationToken()
    cancelled = server._supersede_prior_chat_runs("conv-b", newer)

    assert cancelled >= 2
    assert prior.cancelled
    assert registry_token.cancelled
    assert server.active_chat_tokens["conv-b"] is newer

    server.agent_instance = None
    server.active_chat_tokens.clear()


def test_supersede_is_noop_when_no_prior():
    server.active_chat_tokens.clear()
    server.agent_instance = None
    newer = RunCancellationToken()
    cancelled = server._supersede_prior_chat_runs("conv-empty", newer)
    assert cancelled == 0
    assert server.active_chat_tokens["conv-empty"] is newer
