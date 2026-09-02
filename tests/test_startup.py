import json
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.console import Console

from ouro_agents.config import OuroAgentsConfig
from ouro_agents.display import THEME, OuroDisplay, Verbosity, get_display, set_display
from ouro_agents.scheduler import AgentScheduler, ScheduledTask
from ouro_agents.startup import (
    _fmt_countdown,
    _fmt_next_run,
    _model_rows,
    _mode_rows,
    _schedule_rows,
    print_startup_summary,
)


def _base_config() -> dict:
    return {
        "agent": {
            "name": "hermes",
            "model": "openai/gpt-4.1-mini",
            "workspace": "./workspace",
        },
        "modes": {
            "heartbeat": {
                "model": "openai/gpt-4.1-mini",
                "every": "1h",
                "active_hours": {
                    "start": "09:00",
                    "end": "22:00",
                    "timezone": "America/Chicago",
                },
            }
        },
        "mcp_servers": [],
        "memory": {
            "extraction_model": "openai/gpt-4.1-mini",
            "embedder": "openai/text-embedding-3-small",
        },
    }


def _load_config(data: dict) -> OuroAgentsConfig:
    tmpdir = TemporaryDirectory()
    path = Path(tmpdir.name) / "config.json"
    path.write_text(json.dumps(data))
    config = OuroAgentsConfig.load_from_file(path)
    # Keep the temp dir alive via the config object.
    config._tmpdir = tmpdir  # type: ignore[attr-defined]
    return config


class _FakeScheduler:
    def __init__(self, next_runs=None, tasks=None):
        self._next_runs = next_runs or {}
        self._tasks = tasks or []

    def next_run_times(self):
        return self._next_runs

    def list_tasks(self):
        return self._tasks


class TestFmtHelpers(unittest.TestCase):
    def test_countdown_buckets(self):
        self.assertEqual(_fmt_countdown(0), "now")
        self.assertEqual(_fmt_countdown(45), "now")
        self.assertEqual(_fmt_countdown(60 * 23), "in 23m")
        self.assertEqual(_fmt_countdown(3600 * 5), "in 5h")
        self.assertEqual(_fmt_countdown(3600 * 11 + 60 * 44), "in 11h44m")
        self.assertEqual(_fmt_countdown(3600 * 26), "in 1d")

    def test_next_run_none(self):
        self.assertEqual(_fmt_next_run(None, datetime.now(timezone.utc)), "—")

    def test_next_run_same_day_and_later(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        same_day = now + timedelta(hours=2)
        self.assertEqual(
            _fmt_next_run(same_day, now), f"in 2h · {same_day.strftime('%H:%M')}"
        )
        later = now + timedelta(days=2)
        rendered = _fmt_next_run(later, now)
        self.assertTrue(rendered.startswith("in 2d · "))
        self.assertIn(later.strftime("%a"), rendered)


class TestConfigRows(unittest.TestCase):
    def test_model_rows_without_tiers(self):
        config = _load_config(_base_config())
        rows = dict(_model_rows(config))
        self.assertEqual(rows["agent"], "openai/gpt-4.1-mini")

    def test_model_rows_with_tiers_and_effort(self):
        data = _base_config()
        data["models"] = {
            "strong": {"id": "acme/strong", "reasoning": {"effort": "high"}},
            "light": {"id": "acme/light"},
        }
        config = _load_config(data)
        rows = dict(_model_rows(config))
        self.assertEqual(rows["strong"], "acme/strong · effort high")
        self.assertEqual(rows["light"], "acme/light")
        self.assertNotIn("mid", rows)

    def test_mode_rows(self):
        config = _load_config(_base_config())
        rows = dict(_mode_rows(config))
        self.assertEqual(rows["heartbeat"], "every 1h · 09:00–22:00 America/Chicago")
        self.assertEqual(rows["dream"], "daily · 03:00 America/Chicago")
        self.assertEqual(rows["planning"], "off")

    def test_mode_rows_disabled(self):
        data = _base_config()
        data["modes"]["heartbeat"]["enabled"] = False
        data["dream"] = {"enabled": False}
        config = _load_config(data)
        rows = dict(_mode_rows(config))
        self.assertEqual(rows["heartbeat"], "off")
        self.assertEqual(rows["dream"], "off")


class TestScheduleRows(unittest.TestCase):
    def test_system_and_user_rows(self):
        config = _load_config(_base_config())
        now = datetime.now(timezone.utc)
        tasks = [
            ScheduledTask(
                id="abc",
                name="alignn-sprint",
                prompt="x",
                schedule="0 9 * * *",
                timezone="America/Chicago",
                last_run_status="success",
            ),
            ScheduledTask(
                id="def",
                name="joke generator",
                prompt="x",
                schedule="1m",
                enabled=False,
            ),
        ]
        scheduler = _FakeScheduler(
            next_runs={
                "system:heartbeat": now + timedelta(minutes=30),
                "system:dream": now + timedelta(hours=10),
                "task:abc": now + timedelta(days=1, hours=1),
            },
            tasks=tasks,
        )
        rows = _schedule_rows(config, scheduler)
        self.assertEqual([r[0] for r in rows], ["heartbeat", "dream", "alignn-sprint", "joke generator"])

        heartbeat = rows[0]
        self.assertEqual(heartbeat[1], "1h · 09:00–22:00")
        self.assertIn(heartbeat[2].split(" · ")[0], {"in 29m", "in 30m"})

        dream = rows[1]
        self.assertEqual(dream[1], "03:00 America/Chicago · daily")

        task_row = rows[2]
        self.assertEqual(task_row[1], "0 9 * * * · Chicago")
        self.assertEqual(task_row[2].split(" · ")[0], "in 1d")
        self.assertEqual(task_row[3], "[green]ok[/]")

        disabled_row = rows[3]
        self.assertEqual(disabled_row[2], "—")
        self.assertEqual(disabled_row[3], "[ouro.muted]disabled[/]")

    def test_error_state_shown_for_enabled_task(self):
        config = _load_config(_base_config())
        tasks = [
            ScheduledTask(
                id="abc",
                name="failing",
                prompt="x",
                schedule="1h",
                last_run_status="error",
            )
        ]
        rows = _schedule_rows(config, _FakeScheduler(tasks=tasks))
        failing = next(r for r in rows if r[0] == "failing")
        self.assertEqual(failing[3], "[red]error[/]")


class TestNextRunTimes(unittest.TestCase):
    def test_empty_before_start(self):
        with TemporaryDirectory() as tmpdir:
            scheduler = AgentScheduler(Path(tmpdir) / "tasks.json")
            self.assertEqual(scheduler.next_run_times(), {})


class TestPrintSummary(unittest.TestCase):
    def _render(self, config, scheduler, platform=None) -> str:
        buffer = StringIO()
        display = OuroDisplay(verbosity=Verbosity.NORMAL)
        display.console = Console(
            file=buffer, force_terminal=False, width=120, theme=THEME
        )
        previous = get_display()
        set_display(display)
        try:
            print_startup_summary(config, scheduler, platform=platform)
        finally:
            set_display(previous)
        return buffer.getvalue()

    def test_renders_config_and_schedule(self):
        config = _load_config(_base_config())
        scheduler = _FakeScheduler(
            next_runs={
                "system:heartbeat": datetime.now(timezone.utc)
                + timedelta(hours=1, minutes=30)
            }
        )
        output = self._render(
            config, scheduler, platform="https://api.example.com as a@b.c"
        )
        self.assertIn("hermes", output)
        self.assertIn("server", output)
        self.assertIn("openai/gpt-4.1-mini", output)
        self.assertIn("https://api.example.com as a@b.c", output)
        self.assertIn("schedule", output)
        self.assertIn("heartbeat", output)
        self.assertIn("dream", output)
        self.assertIn("in 1h", output)

    def test_smoke_nothing_scheduled(self):
        data = _base_config()
        data["modes"]["heartbeat"]["enabled"] = False
        data["dream"] = {"enabled": False}
        config = _load_config(data)
        output = self._render(config, _FakeScheduler())
        self.assertIn("nothing scheduled", output)


if __name__ == "__main__":
    unittest.main()
