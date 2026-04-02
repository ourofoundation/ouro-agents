import asyncio
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

from ouro_agents.config import HeartbeatConfig
from ouro_agents.display import OuroDisplay
from ouro_agents.modes.framing import HEARTBEAT_FRAMING
from ouro_agents.modes.heartbeat import (
    build_plan_execution_playbook,
    has_future_heartbeat_in_active_window,
    run_heartbeat,
)
from ouro_agents.subagents.context import SubAgentUsage
from ouro_agents.usage import RunUsage


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


def test_run_heartbeat_preserves_existing_usage_for_main_run(tmp_path):
    captured = {}

    class _DocStore:
        def read(self, key):
            if key == "HEARTBEAT:hermes":
                return "Review the world and act."
            return None

    class _FakeAgent:
        def __init__(self, workspace: Path):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(model="main-model", workspace=workspace, name="hermes"),
            )
            self.doc_store = _DocStore()

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        async def run(self, task, **kwargs):
            captured["task"] = task
            captured["kwargs"] = kwargs
            return '{"action":"none"}'

    agent = _FakeAgent(tmp_path)

    result = asyncio.run(run_heartbeat(agent))

    assert result is None
    assert captured["task"] == "Review the world and act."
    assert captured["kwargs"]["preserve_existing_usage"] is True
    assert captured["kwargs"]["model_override"].model_id == "heartbeat-model"


def test_usage_rows_include_model_ids_for_run_and_subagent_rows():
    display = OuroDisplay()
    total = RunUsage(
        model_id="main-model",
        steps=3,
        input_tokens=120,
        output_tokens=30,
        cost_usd=0.12,
    ).finalize()
    preflight = SubAgentUsage(
        model_id="preflight-model",
        steps=1,
        input_tokens=40,
        output_tokens=10,
        llm_calls=1,
        wall_time_ms=250,
        cost_usd=0.02,
    )

    rows = display._usage_rows(total, duration_s=1.5, subagent_ledger=[("preflight", preflight)])

    assert rows[0][1] == "main-model"
    assert rows[1][0] == "sub:preflight"
    assert rows[1][1] == "preflight-model"
    assert rows[-1][0] == "total"
    assert rows[-1][1] == "main-model"


def test_usage_rows_hide_reasoning_by_default():
    display = OuroDisplay()
    total = RunUsage(model_id="main-model", input_tokens=10, output_tokens=5).finalize()

    rows = display._usage_rows(total, duration_s=1.5)

    assert len(rows[0]) == 9
    assert "Reasoning" not in display._usage_table_headers()


def test_usage_rows_can_show_reasoning_when_enabled():
    display = OuroDisplay(show_reasoning_in_summary=True)
    total = RunUsage(
        model_id="main-model",
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=3,
    ).finalize()

    rows = display._usage_rows(total, duration_s=1.5)

    assert len(rows[0]) == 10
    assert "Reasoning" in display._usage_table_headers()
    assert rows[0][7] == "3"
