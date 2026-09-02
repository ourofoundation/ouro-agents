import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ouro_agents.memory.dream import write_dream_status
from ouro_agents.run_log import RunLogStore, RunRecord
from ouro_agents.scheduler import (
    AgentScheduler,
    cadence_trigger,
    count_dream_activity_runs,
    has_sufficient_dream_activity,
)


@pytest.mark.parametrize(
    ("cadence", "expected"),
    [
        ("2s", timedelta(seconds=2)),
        ("3m", timedelta(minutes=3)),
        ("4h", timedelta(hours=4)),
        ("5d", timedelta(days=5)),
        ("2w", timedelta(weeks=2)),
    ],
)
def test_cadence_trigger_parses_all_interval_units(cadence, expected):
    trigger = cadence_trigger(
        cadence,
        at="06:15",
        timezone_name="America/New_York",
    )

    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval == expected
    assert trigger.start_date.hour == 6
    assert trigger.start_date.minute == 15
    assert str(trigger.start_date.tzinfo) == "America/New_York"


def test_cadence_trigger_defaults_to_daily_at_configured_time():
    trigger = cadence_trigger(
        None,
        at="03:45",
        timezone_name="America/Los_Angeles",
    )

    assert isinstance(trigger, CronTrigger)
    next_fire = trigger.get_next_fire_time(
        None,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert next_fire.hour == 3
    assert next_fire.minute == 45
    assert str(next_fire.tzinfo) == "America/Los_Angeles"


def test_one_day_cadence_uses_local_cron_without_dst_drift():
    trigger = cadence_trigger(
        "1d",
        at="03:00",
        timezone_name="America/Chicago",
    )

    assert isinstance(trigger, CronTrigger)
    assert str(trigger.timezone) == "America/Chicago"


@pytest.mark.parametrize("cadence", ["", "hourly", "0m", "1y"])
def test_cadence_trigger_rejects_invalid_intervals(cadence):
    with pytest.raises(ValueError):
        cadence_trigger(cadence)


def test_activity_gate_counts_only_new_non_trivial_top_level_runs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    store = RunLogStore(tmp_path / "runs.db")
    write_dream_status(workspace, "2026-09-01", {})
    after_last_dream = datetime.now(timezone.utc) + timedelta(seconds=1)
    started_at = after_last_dream.isoformat()

    store.write(
        RunRecord(
            run_id="chat",
            mode="chat",
            task="Help a user",
            started_at=started_at,
        )
    )
    store.write(
        RunRecord(
            run_id="heartbeat",
            mode="heartbeat",
            num_steps=1,
            started_at=started_at,
        )
    )
    store.write(
        RunRecord(
            run_id="dream",
            mode="dream",
            task="dream",
            started_at=started_at,
        )
    )
    store.write(
        RunRecord(
            run_id="plan",
            mode="plan",
            task="plan",
            started_at=started_at,
        )
    )
    store.write(
        RunRecord(
            run_id="child",
            mode="subagent:research",
            parent_run_id="chat",
            task="research",
            started_at=started_at,
        )
    )
    store.write(
        RunRecord(
            run_id="empty",
            mode="chat",
            started_at=started_at,
        )
    )
    store.write(
        RunRecord(
            run_id="old",
            mode="chat",
            task="old activity",
            started_at="2020-01-01T00:00:00+00:00",
        )
    )

    assert count_dream_activity_runs(store) == 3
    assert has_sufficient_dream_activity(
        workspace,
        store,
        min_new_runs=2,
    ) == (True, 2)
    assert has_sufficient_dream_activity(
        workspace,
        store,
        min_new_runs=3,
    ) == (False, 2)
    store.close()


def test_dream_runs_once(tmp_path: Path):
    workspace = tmp_path / "workspace"
    store = RunLogStore(tmp_path / "runs.db")
    for index in range(3):
        store.write(
            RunRecord(
                run_id=f"run-{index}",
                mode="chat",
                task=f"task {index}",
            )
        )

    dream = Mock(return_value={"run_id": "dream-run"})
    agent = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(workspace=workspace),
            dream=SimpleNamespace(
                min_new_runs=3,
                dry_run=False,
            ),
        ),
        _run_log=store,
        dream=dream,
    )

    async def execute():
        scheduler = AgentScheduler(tmp_path / "tasks.json")
        scheduler._agent = agent
        await scheduler._execute_dream()

    asyncio.run(execute())

    dream.assert_called_once_with(mode="scheduled")
    store.close()
