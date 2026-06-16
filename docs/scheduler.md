# Scheduler

`AgentScheduler` (`ouro_agents/scheduler.py`) is a thin wrapper around
[APScheduler](https://github.com/agronholm/apscheduler) that owns two
**system tasks** plus any number of **user-defined tasks** the agent
creates at runtime.

It runs only when the FastAPI server is up (`ouro-agents serve`).

## What runs by default

| Task | When | Source |
|------|------|--------|
| `system:heartbeat` | Every `heartbeat.every`, anchored to the start of the active window. | `HeartbeatConfig`. |
| `system:dream` | Ticks daily at `memory.dream_time` (default `03:00`), but only runs once per `memory.rhythm` period (daily/weekly/biweekly). | `MemoryConfig`. |

Refinement no longer has its own job: the change-set queue is drained as
the first phase of `system:dream` (see [refinement.md](refinement.md)).

System task ids are protected: `SYSTEM_PROTECTED_IDS` blocks the agent
from accidentally deleting them with `remove_task`.

## ScheduledTask shape

```python
class ScheduledTask(BaseModel):
    id: str           # uuid
    name: str
    prompt: str       # task to dispatch via OuroAgent.run
    schedule: str     # cron ("0 9 * * *") or interval ("30s", "5m", "2h", "1d")
    timezone: str = "UTC"
    enabled: bool = True
    team_id: str | None = None  # team-scope the run

    last_run_at: str | None
    last_run_status: "success" | "error" | "running" | None
    last_error: str | None
    run_count: int
    learnings: list[str]      # short bullets the agent left behind
    created_at, updated_at
```

## CLI / programmatic management

The agent itself can manage tasks through the `scheduler_tools` (added to
its tool set when not in restricted-server mode). Programmatic callers
work directly:

```python
from ouro_agents.scheduler import AgentScheduler, ScheduledTask

scheduler.add_task(ScheduledTask(
    name="morning-digest",
    prompt="Summarize overnight activity in team X.",
    schedule="0 9 * * *",
    timezone="America/Chicago",
    team_id="...",
))

scheduler.update_task(task_id, enabled=False)
scheduler.remove_task(task_id)
scheduler.list_tasks()
```

`MAX_TASKS` (50) bounds the number of user-defined tasks. Schedule
strings are validated up front via `parse_trigger`.

## Schedule formats

`parse_trigger` accepts:

- **Cron expressions** with five fields: `0 9 * * *`.
- **Interval shorthand**: `30s`, `5m`, `2h`, `1d`.

Anything else raises `ValueError` with a hint.

## Heartbeat anchoring

When `heartbeat.active_hours.start` is set, the heartbeat trigger anchors
its schedule to the configured start minute so daily ticks don't drift
across days. Without active hours it uses a plain `IntervalTrigger`.

## HTTP introspection

`GET /tasks` returns the list of tasks (system + user) for monitoring
dashboards. `GET /health` reports the count.

## Failure handling

Each task execution sets `last_run_status="running"`, runs
`OuroAgent.run(...)` in autonomous mode (with the task's `team_id` when
set), and updates `last_run_status` to `success` or `error` plus
`last_error` afterwards. APScheduler's `misfire_grace_time=300` means a
missed tick within five minutes still fires once.
