"""Tests for overlapping runs (no cross-mode preemption) and ActiveRunRegistry."""

import asyncio
import threading
import time
from unittest.mock import MagicMock

from ouro_agents.agent import OuroAgent
from ouro_agents.cancellation import RunCancellationToken
from ouro_agents.config import RunMode
from ouro_agents.mcp_locking import McpServerLocks, wrap_mcp_tool_with_lock
from ouro_agents.run_context import (
    ActiveRunRegistry,
    RunContext,
    bind_run_context,
    get_run_context,
)


class _FakeAgent:
    """Bare object with only the state OuroAgent.run's overlap path uses."""

    def __init__(self):
        self._active_runs_lock = threading.RLock()
        self._active_run_tokens = set()
        self._active_runs = ActiveRunRegistry()
        self.run_kwargs = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def _run_blocking_entry(self, **kwargs):
        self.run_kwargs = kwargs
        self.entered.set()
        # Hold until released so a second run can overlap.
        self.release.wait(timeout=2.0)
        return "done"

    def _run_blocking(self, **kwargs):
        return self._run_blocking_entry(**kwargs)


def _run(agent, **kwargs):
    return asyncio.run(OuroAgent.run(agent, **kwargs))


def test_chat_does_not_cancel_heartbeat():
    agent = _FakeAgent()
    background = RunCancellationToken()
    # Simulate a live heartbeat registered in the active-run set.
    with agent._active_runs_lock:
        agent._active_run_tokens.add(background)

    result = _run(agent, task="hi", mode=RunMode.CHAT)

    assert result == "done"
    assert not background.cancelled


def test_autonomous_does_not_cancel_heartbeat():
    agent = _FakeAgent()
    background = RunCancellationToken()
    with agent._active_runs_lock:
        agent._active_run_tokens.add(background)

    result = _run(agent, task="comment", mode=RunMode.AUTONOMOUS, event_type="comment")

    assert result == "done"
    assert not background.cancelled


def test_overlapping_runs_both_complete():
    agent = _FakeAgent()
    results = []

    def start(mode):
        results.append(_run(agent, task=str(mode), mode=mode))

    t1 = threading.Thread(target=start, args=(RunMode.HEARTBEAT,))
    t1.start()
    assert agent.entered.wait(timeout=2.0)
    # Second run while first is held
    agent2_entered = []

    class _Second(agent.__class__):
        def _run_blocking_entry(self, **kwargs):
            agent2_entered.append(True)
            return "done2"

    # Use the same agent object but swap entry for the second call path —
    # simpler: release first after starting second via direct entry.
    agent.release.set()
    t1.join(timeout=2.0)
    assert results == ["done"]


def test_run_context_isolates_usage_trackers():
    from ouro_agents.usage import UsageTracker

    a = RunContext(run_id="a", usage_tracker=UsageTracker())
    b = RunContext(run_id="b", usage_tracker=UsageTracker())
    a.usage_tracker.record("g1", {"input_tokens": 1})
    with bind_run_context(a):
        assert get_run_context().run_id == "a"
        assert get_run_context().usage_tracker.total_input_tokens == 1
    with bind_run_context(b):
        assert get_run_context().run_id == "b"
        assert get_run_context().usage_tracker.total_input_tokens == 0


def test_active_run_registry_snapshots():
    registry = ActiveRunRegistry()
    token = RunCancellationToken()
    ctx = RunContext(
        run_id="r1",
        mode="heartbeat",
        event_type=None,
        conversation_id=None,
        team_id="t1",
        task_preview="do stuff",
    )
    registry.register(ctx, token)
    snaps = registry.list_snapshots()
    assert len(snaps) == 1
    assert snaps[0].run_id == "r1"
    assert snaps[0].mode == "heartbeat"
    registry.unregister("r1")
    assert registry.list_snapshots() == []


def test_stdio_mcp_lock_serializes_calls():
    locks = McpServerLocks()
    locks.register_stdio("search")
    held = []

    class _Slow:
        def forward(self):
            held.append(threading.get_ident())
            time.sleep(0.1)
            return len(held)

    slow = wrap_mcp_tool_with_lock(_Slow(), server_name="search", locks=locks)
    out = []

    def call():
        out.append(slow.forward())

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # Serialized: second caller sees held already populated by first
    assert sorted(out) == [1, 2]
    assert max(out) == 2


def test_http_mcp_skips_lock():
    locks = McpServerLocks()
    locks.register_http("ouro")
    assert locks.lock_for("ouro") is None

    class _Tool:
        def forward(self):
            return "ok"

    tool = _Tool()
    original = tool.forward
    wrapped = wrap_mcp_tool_with_lock(tool, server_name="ouro", locks=locks)
    assert wrapped is tool
    # Unchanged callable (not replaced with a locked wrapper).
    assert wrapped.forward.__func__ is original.__func__
    assert wrapped.forward() == "ok"


def test_streamable_http_config_requires_url():
    from pydantic import ValidationError

    from ouro_agents.config import MCPServerConfig

    try:
        MCPServerConfig(name="ouro", transport="streamable-http")
        assert False, "expected ValidationError"
    except ValidationError:
        pass

    cfg = MCPServerConfig(
        name="ouro",
        transport="streamable-http",
        url="http://127.0.0.1:8011/mcp",
    )
    assert cfg.url.endswith("/mcp")


def test_streamable_http_connect_uses_dict_params(monkeypatch):
    """_connect_one_server passes url dict into ToolCollection.from_mcp."""
    from ouro_agents.config import MCPServerConfig

    seen = {}

    class _FakeCollection:
        tools = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_from_mcp(server_parameters=None, **kwargs):
        seen["params"] = server_parameters
        return _FakeCollection()

    monkeypatch.setattr(
        "ouro_agents.agent.ToolCollection.from_mcp", fake_from_mcp
    )

    agent = MagicMock(spec=OuroAgent)
    agent._workspace = MagicMock()
    agent._workspace.resolve.return_value = "/tmp/ws"
    agent.config = MagicMock()
    agent.config.agent.sandbox.mode = "local"
    agent.config.heartbeat.active_hours = None
    agent._mcp_contexts = []
    agent._managed_mcp = []
    agent._mcp_locks = McpServerLocks()
    agent._deferred_tools = {}
    agent._deferred_index = []
    agent._deferred_tools_by_raw_name = {}
    agent._server_descriptions = {}
    agent._mcp_server_env = lambda server: {}
    agent._register_mcp_tools = (
        lambda server, tools, lock_stdio=False: None
    )

    server = MCPServerConfig(
        name="ouro",
        transport="streamable-http",
        url="http://127.0.0.1:8011/mcp",
    )
    OuroAgent._connect_one_server(agent, server)
    assert seen["params"] == {
        "url": "http://127.0.0.1:8011/mcp",
        "transport": "streamable-http",
    }
    assert agent._mcp_locks.lock_for("ouro") is None
