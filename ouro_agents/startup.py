"""Startup summary for ``ouro-agents serve``.

Two tables rendered once when the server boots: the resolved configuration
worth double-checking, and the scheduler's upcoming jobs. Output goes through
the shared display console so it stays pipe-safe under PM2.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from rich.markup import escape
from rich.table import Table

from .config import OuroAgentsConfig
from .display import get_display
from .modes.heartbeat import is_within_active_hours
from .scheduler import SYSTEM_DREAM_ID, SYSTEM_HEARTBEAT_ID, AgentScheduler


def _truncate(text: str, limit: int = 21) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_countdown(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return "now"
    minutes = s // 60
    if minutes < 60:
        return f"in {minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours}h{mins:02d}m" if mins else f"in {hours}h"
    return f"in {hours // 24}d"


def _fmt_next_run(dt: Optional[datetime], now: datetime) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    same_day = dt.date() == now.astimezone(dt.tzinfo).date()
    wall = dt.strftime("%H:%M") if same_day else dt.strftime("%a %H:%M")
    return f"{_fmt_countdown((dt - now).total_seconds())} · {wall}"


def _active_hours_window(config: OuroAgentsConfig) -> Optional[str]:
    hours = config.heartbeat.active_hours
    if not hours or not hours.get("start") or not hours.get("end"):
        return None
    return f"{hours['start']}–{hours['end']} {hours.get('timezone') or 'local'}"


def _model_rows(config: OuroAgentsConfig) -> list[tuple[str, str]]:
    tiers = config.models
    if tiers is None:
        return [
            ("agent", config.agent.model),
            ("heartbeat", config.heartbeat.model),
            ("extraction", config.memory.extraction_model),
        ]
    rows = []
    for name in ("strong", "mid", "light"):
        spec = getattr(tiers, name)
        if spec is None:
            continue
        value = spec.id
        if spec.reasoning and spec.reasoning.effort:
            value += f" · effort {spec.reasoning.effort}"
        rows.append((name, value))
    return rows


def _mode_rows(config: OuroAgentsConfig) -> list[tuple[str, str]]:
    heartbeat = config.heartbeat
    if heartbeat.enabled:
        window = _active_hours_window(config)
        hb_value = f"every {heartbeat.every}"
        hb_value += f" · {window}" if window else " · always on"
    else:
        hb_value = "off"
    memory = config.memory
    dream = f"{memory.rhythm} · {memory.dream_time} UTC" if memory.dream_enabled else "off"
    planning = config.planning
    if planning.enabled:
        plan_value = f"every {planning.cadence}"
        plan_value += " · auto-approve" if planning.auto_approve else " · manual review"
    else:
        plan_value = "off"
    return [("heartbeat", hb_value), ("dream", dream), ("planning", plan_value)]


def _config_grid(config: OuroAgentsConfig, platform: Optional[str]) -> Table:
    rows: list[tuple[str, str]] = []
    server = config.server
    rows.append(("server", f"{server.host}:{server.port} · webhook {server.webhook_path}"))
    if server.public_base_url:
        rows.append(("public", escape(server.public_base_url)))
    config_bits = [escape(os.environ.get("CONFIG_FILE") or "config.json")]
    env_file = os.environ.get("ENV_FILE") or (
        str(config.env_file) if config.env_file else ""
    )
    if env_file:
        config_bits.append(escape(env_file))
    rows.append(("config", " · ".join(config_bits)))
    rows.append(("workspace", escape(str(config.agent.workspace))))
    if platform:
        rows.append(("platform", escape(platform)))
    keys = " · ".join(
        f"{name} {'[green]✓[/]' if os.environ.get(env) else '[red]✗[/]'}"
        for name, env in (("OPENROUTER", "OPENROUTER_API_KEY"), ("OURO", "OURO_API_KEY"))
    )
    rows.append(("keys", keys))

    rows.append(("", ""))
    rows.extend(_model_rows(config))
    rows.append(("", ""))
    rows.extend(_mode_rows(config))
    rows.append(("", ""))

    servers = ", ".join(f"{escape(s.name)} ({s.transport})" for s in config.mcp_servers)
    rows.append(("mcp", servers or "—"))
    sandbox = config.agent.sandbox
    rows.append(
        (
            "sandbox",
            "local" if sandbox.mode == "local" else f"docker · {escape(sandbox.image)}",
        )
    )
    security = config.security
    sec_bits = []
    if security.controllers:
        n = len(security.controllers)
        sec_bits.append(f"{n} controller{'s' if n != 1 else ''}")
    if security.trusted:
        sec_bits.append(f"{len(security.trusted)} trusted")
    if security.run_secret:
        sec_bits.append("run secret")
    rows.append(("security", " · ".join(sec_bits) or "—"))
    features = []
    if config.prompt_caching.enabled:
        features.append(f"prompt caching {config.prompt_caching.ttl}")
    if config.event_pooling.enabled:
        features.append("event pooling")
    if config.agent_routes.enabled:
        features.append("agent routes")
    if config.run_log.enabled:
        features.append("run log")
    rows.append(("features", " · ".join(features) or "—"))

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="ouro.muted", justify="right")
    grid.add_column()
    for key, value in rows:
        grid.add_row(key, value)
    return grid


def _dream_state(config: OuroAgentsConfig) -> str:
    from .memory.dream import read_dream_marker
    from .memory.naming import period_key

    done = read_dream_marker(config.agent.workspace) == period_key(config.memory.rhythm)
    return "[ouro.muted]done[/]" if done else "pending"


def _schedule_rows(
    config: OuroAgentsConfig, scheduler: AgentScheduler
) -> list[tuple[str, str, str, str]]:
    next_runs = scheduler.next_run_times()
    now = datetime.now(timezone.utc)
    rows: list[tuple[str, str, str, str]] = []

    heartbeat = config.heartbeat
    if heartbeat.enabled:
        # The window's timezone lives in the config grid above; keep this short.
        schedule = heartbeat.every
        hours = heartbeat.active_hours
        if hours and hours.get("start") and hours.get("end"):
            schedule += f" · {hours['start']}–{hours['end']}"
        state = "[green]active[/]" if is_within_active_hours(heartbeat) else "[ouro.muted]asleep[/]"
        rows.append(
            (
                "heartbeat",
                schedule,
                _fmt_next_run(next_runs.get(SYSTEM_HEARTBEAT_ID), now),
                state,
            )
        )

    memory = config.memory
    if memory.dream_enabled:
        rows.append(
            (
                "dream",
                f"{memory.dream_time} UTC · {memory.rhythm}",
                _fmt_next_run(next_runs.get(SYSTEM_DREAM_ID), now),
                _dream_state(config),
            )
        )

    for task in scheduler.list_tasks():
        name = escape(_truncate(task.name))
        schedule = escape(task.schedule)
        if task.timezone != "UTC":
            # "America/Argentina/Buenos_Aires" → "Buenos Aires"
            schedule += f" · {escape(task.timezone.split('/')[-1].replace('_', ' '))}"
        if not task.enabled:
            rows.append((name, schedule, "—", "[ouro.muted]disabled[/]"))
            continue
        state = {
            "success": "[green]ok[/]",
            "error": "[red]error[/]",
            "running": "running",
            "cancelled": "[ouro.muted]cancelled[/]",
        }.get(task.last_run_status or "", "scheduled")
        rows.append(
            (
                name,
                schedule,
                _fmt_next_run(next_runs.get(f"task:{task.id}"), now),
                state,
            )
        )
    return rows


def print_startup_summary(
    config: OuroAgentsConfig,
    scheduler: AgentScheduler,
    *,
    platform: Optional[str] = None,
) -> None:
    """Render the config + schedule tables shown when the server boots."""
    display = get_display()
    display.blank()
    display.rule(config.agent.name)
    display.render(_config_grid(config, platform))
    display.blank()
    display.rule("schedule")
    rows = _schedule_rows(config, scheduler)
    if rows:
        table = Table(box=None, padding=(0, 1), show_header=True, header_style="ouro.dim")
        for col in ("job", "schedule", "next run", "state"):
            table.add_column(col)
        for row in rows:
            table.add_row(*row)
        display.render(table)
    else:
        display.info("nothing scheduled")
    display.blank()
