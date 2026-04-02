from datetime import datetime
from zoneinfo import ZoneInfo

from ouro_agents.config import HeartbeatConfig
from ouro_agents.modes.framing import HEARTBEAT_FRAMING
from ouro_agents.modes.heartbeat import (
    build_plan_execution_playbook,
    has_future_heartbeat_in_active_window,
)


def test_heartbeat_framing_prefers_bounded_progress():
    assert "bounded work session" in HEARTBEAT_FRAMING
    assert "one meaningful slice of progress" in HEARTBEAT_FRAMING


def test_plan_execution_playbook_mentions_multi_heartbeat_expectation():
    playbook = build_plan_execution_playbook("## Default Plan\n- [ ] Do the thing", 4)

    assert "one meaningful slice of progress" in playbook
    assert "do not feel pressure to use all available steps" in playbook
    assert "at least 4 heartbeats before replanning" in playbook
    assert "`create_comment`" in playbook


def test_has_future_heartbeat_in_active_window_before_last_tick():
    cfg = HeartbeatConfig(
        model="test-model",
        every="1h",
        active_hours={"start": "09:00", "end": "17:00", "timezone": "America/Chicago"},
    )
    now = datetime(2026, 4, 1, 16, 0, tzinfo=ZoneInfo("America/Chicago"))

    assert has_future_heartbeat_in_active_window(cfg, now=now) is True


def test_has_future_heartbeat_in_active_window_on_last_tick():
    cfg = HeartbeatConfig(
        model="test-model",
        every="1h",
        active_hours={"start": "09:00", "end": "17:00", "timezone": "America/Chicago"},
    )
    now = datetime(2026, 4, 1, 17, 0, tzinfo=ZoneInfo("America/Chicago"))

    assert has_future_heartbeat_in_active_window(cfg, now=now) is False
