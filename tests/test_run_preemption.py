"""Tests for run-lock preemption: interactive runs cancel background holders."""

import asyncio
import threading

from ouro_agents.agent import OuroAgent
from ouro_agents.cancellation import RunCancellationToken
from ouro_agents.config import RunMode


class _FakeAgent:
    """Bare object with only the state OuroAgent.run's preemption path uses."""

    def __init__(self):
        self._run_lock = threading.RLock()
        self._active_runs_lock = threading.RLock()
        self._active_run_tokens = set()
        self._run_lock_holder = None
        self.run_kwargs = None

    def _run_blocking_locked(self, **kwargs):
        self.run_kwargs = kwargs
        return "done"

    def _run_blocking(self, **kwargs):
        self.run_kwargs = kwargs
        return "done"


def _run(agent, **kwargs):
    return asyncio.run(OuroAgent.run(agent, **kwargs))


def test_chat_run_preempts_background_holder():
    agent = _FakeAgent()
    background = RunCancellationToken()
    agent._run_lock_holder = (background, True)

    result = _run(agent, task="hi", mode=RunMode.CHAT)

    assert result == "done"
    assert background.cancelled
    assert background.reason == "preempted by interactive run"


def test_background_run_does_not_preempt():
    agent = _FakeAgent()
    background = RunCancellationToken()
    agent._run_lock_holder = (background, True)

    for mode in (RunMode.HEARTBEAT, RunMode.PLAN, RunMode.REVIEW):
        _run(agent, task="tick", mode=mode)
        assert not background.cancelled


def test_interactive_run_leaves_non_preemptible_holder_alone():
    agent = _FakeAgent()
    other = RunCancellationToken()
    agent._run_lock_holder = (other, False)

    _run(agent, task="hi", mode=RunMode.CHAT)

    assert not other.cancelled


def test_explicit_preemptible_flag_overrides_mode_default():
    agent = _FakeAgent()
    background = RunCancellationToken()
    agent._run_lock_holder = (background, True)

    # A scheduled autonomous task marked preemptible must not preempt others.
    _run(agent, task="cron", mode=RunMode.AUTONOMOUS, preemptible=True)
    assert not background.cancelled


def test_run_blocking_locked_tracks_and_clears_holder():
    agent = _FakeAgent()
    seen = {}

    def _capture(**kwargs):
        seen["holder"] = agent._run_lock_holder
        return "ok"

    agent._run_blocking = _capture
    token = RunCancellationToken()
    result = OuroAgent._run_blocking_locked(
        agent, task="x", cancellation_token=token, preemptible=True
    )

    assert result == "ok"
    assert seen["holder"] == (token, True)
    assert agent._run_lock_holder is None
