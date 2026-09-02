"""Overlapping chat runs: newer message supersedes the in-flight reply."""

from __future__ import annotations

from ouro_agents.cancellation import RunCancellationToken
from ouro_agents.config import RunMode
from ouro_agents.events import EventRunContext
from ouro_agents import server
from ouro_agents.run_context import ActiveRunRegistry, RunContext


class _FakeAgent:
    def __init__(self):
        self._active_runs = ActiveRunRegistry()


def _event(conversation_id: str = "conv-a") -> EventRunContext:
    return EventRunContext(
        event_type="new-message",
        task="hello",
        conversation_id=conversation_id,
        user_id="agent-peer",
        team_id=None,
        actor_user_id="agent-peer",
        actor_username="apollo",
        actor_is_agent=True,
        mode=RunMode.CHAT,
    )


def _reset():
    server.active_chat_tokens.clear()
    server.pending_agent_chat_events.clear()
    server.agent_instance = None


def test_supersede_cancels_prior_active_chat_token():
    _reset()

    prior = RunCancellationToken()
    server.active_chat_tokens["conv-a"] = prior
    newer = RunCancellationToken()

    cancelled = server._supersede_prior_chat_runs("conv-a", newer)

    assert cancelled == 1
    assert prior.cancelled
    assert "superseded" in (prior.reason or "")
    assert server.active_chat_tokens["conv-a"] is newer
    assert not newer.cancelled
    _reset()


def test_supersede_also_cancels_registry_tokens_for_conversation():
    _reset()
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

    _reset()


def test_supersede_is_noop_when_no_prior():
    _reset()
    newer = RunCancellationToken()
    cancelled = server._supersede_prior_chat_runs("conv-empty", newer)
    assert cancelled == 0
    assert server.active_chat_tokens["conv-empty"] is newer
    _reset()


def test_begin_chat_run_human_supersedes_in_flight():
    _reset()
    prior = RunCancellationToken()
    server.active_chat_tokens["conv-a"] = prior
    newer = RunCancellationToken()

    started = server._begin_chat_run("conv-a", newer, actor_is_agent=False)

    assert started is True
    assert prior.cancelled
    assert server.active_chat_tokens["conv-a"] is newer
    _reset()


def test_begin_chat_run_agent_queues_when_busy():
    _reset()
    prior = RunCancellationToken()
    server.active_chat_tokens["conv-a"] = prior
    newer = RunCancellationToken()
    event = _event()

    started = server._begin_chat_run(
        "conv-a", newer, actor_is_agent=True, pending_event=event
    )

    assert started is False
    assert not prior.cancelled
    assert newer not in server.active_chat_tokens.values()
    assert server.pending_agent_chat_events["conv-a"] is event
    _reset()


def test_begin_chat_run_agent_starts_when_idle():
    _reset()
    newer = RunCancellationToken()

    started = server._begin_chat_run("conv-a", newer, actor_is_agent=True)

    assert started is True
    assert server.active_chat_tokens["conv-a"] is newer
    assert "conv-a" not in server.pending_agent_chat_events
    _reset()


def test_begin_chat_run_human_drops_queued_agent_event():
    _reset()
    server.pending_agent_chat_events["conv-a"] = _event()
    newer = RunCancellationToken()

    started = server._begin_chat_run("conv-a", newer, actor_is_agent=False)

    assert started is True
    assert "conv-a" not in server.pending_agent_chat_events
    _reset()
