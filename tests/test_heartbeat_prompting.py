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
    force_planning_heartbeat,
    has_future_heartbeat_in_active_window,
    run_heartbeat,
)
from ouro_agents.modes.planning import PlanCycle, PlanItem, PlanStore
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


def test_run_heartbeat_scopes_preflight_and_run_to_selected_team(tmp_path):
    workspace = tmp_path
    store_a = PlanStore(workspace / "teams" / "team-a" / "plans", team_id="team-a")
    store_b = PlanStore(workspace / "teams" / "team-b" / "plans", team_id="team-b")
    store_a.save(
        PlanCycle(
            status="active",
            kind="default",
            team_id="team-a",
            quest_id="quest-a",
            plan_text="Plan for team A",
            items=[PlanItem(description="Do A", status="in_progress")],
        )
    )
    store_b.save(
        PlanCycle(
            status="active",
            kind="default",
            team_id="team-b",
            quest_id="quest-b",
            plan_text="Plan for team B",
            items=[PlanItem(description="Do B", status="in_progress")],
        )
    )

    captured = {}

    class _DocStore:
        def __init__(self, team_id):
            self.team_id = team_id

        def read(self, key):
            if key == "HEARTBEAT:hermes":
                return f"Heartbeat playbook for {self.team_id}"
            return None

    class _Registry:
        def team_ids(self):
            return {"team-b", "team-a"}

    class _FakeAgent:
        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(
                    enabled=True,
                    cadence="1d",
                    min_heartbeats=1,
                    review_window="1h",
                    auto_approve=False,
                ),
                agent=SimpleNamespace(model="main-model", workspace=workspace, name="hermes"),
            )
            self.team_registry = _Registry()
            self.doc_store = _DocStore("default")

        def doc_store_for(self, team_id):
            return _DocStore(team_id)

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        def _run_subagent(self, _profile, task, **kwargs):
            captured["preflight_task"] = task
            captured["preflight_kwargs"] = kwargs
            return SimpleNamespace(
                text='{"action":"general_heartbeat","plan_id":null,"reasoning":"stay scoped"}'
            )

        def _get_ouro_client(self):
            return None

        async def run(self, task, **kwargs):
            captured["task"] = task
            captured["kwargs"] = kwargs
            return '{"action":"none"}'

    agent = _FakeAgent()

    result = asyncio.run(run_heartbeat(agent))

    assert result is None
    assert "Do A" in captured["preflight_task"]
    assert "Do B" not in captured["preflight_task"]
    assert captured["preflight_kwargs"]["team_id"] == "team-a"
    assert captured["kwargs"]["team_id"] == "team-a"


def test_run_heartbeat_does_not_fallback_to_root_playbook_for_team_runs(tmp_path):
    workspace = tmp_path
    (workspace / "HEARTBEAT.md").write_text("legacy root heartbeat")
    store_a = PlanStore(workspace / "teams" / "team-a" / "plans", team_id="team-a")
    store_a.save(
        PlanCycle(
            status="active",
            kind="default",
            team_id="team-a",
            quest_id="quest-a",
            items=[PlanItem(description="Do A", status="in_progress")],
        )
    )

    class _DocStore:
        def read(self, _key):
            return None

    class _Registry:
        def team_ids(self):
            return {"team-a"}

    class _FakeAgent:
        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(
                    enabled=True,
                    cadence="1d",
                    min_heartbeats=1,
                    review_window="1h",
                    auto_approve=False,
                ),
                agent=SimpleNamespace(model="main-model", workspace=workspace, name="hermes"),
            )
            self.team_registry = _Registry()
            self.doc_store = _DocStore()

        def doc_store_for(self, _team_id):
            return _DocStore()

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        def _get_ouro_client(self):
            return None

        def _run_subagent(self, _profile, _task, **_kwargs):
            return SimpleNamespace(
                text='{"action":"skip","plan_id":null,"reasoning":"no heartbeat run"}'
            )

        async def run(self, task, **kwargs):
            raise AssertionError("heartbeat should not fall back to the root playbook")

    result = asyncio.run(run_heartbeat(_FakeAgent()))

    assert result is None


def test_force_planning_heartbeat_uses_explicit_team_id(tmp_path):
    captured = {}

    class _Registry:
        def team_ids(self):
            return {"team-a", "team-b"}

    class _FakeAgent:
        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                ),
                agent=SimpleNamespace(model="main-model", workspace=tmp_path, name="hermes"),
            )
            self.team_registry = _Registry()

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

    agent = _FakeAgent()

    async def _fake_run_planning_heartbeat(
        _agent, hb_model, plan_store, servers, goal="", kind="default"
    ):
        captured["model_id"] = hb_model.model_id
        captured["team_id"] = plan_store.team_id
        captured["servers"] = servers
        captured["goal"] = goal
        captured["kind"] = kind
        return "planned"

    from unittest.mock import patch

    with patch(
        "ouro_agents.modes.planning.run_planning_heartbeat",
        new=_fake_run_planning_heartbeat,
    ):
        result = asyncio.run(
            force_planning_heartbeat(agent, goal="Focus on team B", team_id="team-b")
        )

    assert result == "planned"
    assert captured["model_id"] == "heartbeat-model"
    assert captured["team_id"] == "team-b"
    assert captured["servers"] == ["ouro"]
    assert captured["goal"] == "Focus on team B"
    assert captured["kind"] == "goal"


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
