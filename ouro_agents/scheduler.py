"""General-purpose task scheduler for ouro-agents.

Extends the existing APScheduler heartbeat infrastructure into a
full scheduler that the agent can use to create and manage its own
recurring tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .cancellation import RunCancelled
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, Field

from .modes.heartbeat import format_active_period_status
from .run_log import RunLogStore

logger = logging.getLogger(__name__)

MAX_TASKS = 50

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class ScheduledTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    prompt: str
    schedule: str  # cron expression ("0 9 * * *") or interval ("4h", "30m")
    timezone: str = "UTC"
    enabled: bool = True
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None  # "success" | "error" | "running"
    last_error: Optional[str] = None
    run_count: int = 0
    learnings: list[str] = Field(default_factory=list)
    team_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TaskStore:
    """Read/write scheduled tasks as a JSON file in the workspace."""

    def __init__(self, path: Path):
        self._path = path

    def load(self) -> list[ScheduledTask]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
            return [ScheduledTask(**item) for item in data]
        except Exception:
            logger.exception("Failed to load scheduled tasks from %s", self._path)
            return []

    def save(self, tasks: list[ScheduledTask]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [t.model_dump() for t in tasks]
        # Atomic write: tmp file then rename
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            os.unlink(tmp)
            raise

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        for t in self.load():
            if t.id == task_id:
                return t
        return None

    def add(self, task: ScheduledTask) -> None:
        from .memory_lock import memory_write_lock

        with memory_write_lock():
            tasks = self.load()
            tasks.append(task)
            self.save(tasks)

    def update(self, task_id: str, **kwargs: Any) -> Optional[ScheduledTask]:
        from .memory_lock import memory_write_lock

        with memory_write_lock():
            tasks = self.load()
            for i, t in enumerate(tasks):
                if t.id == task_id:
                    updated = t.model_copy(
                        update={
                            **kwargs,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    tasks[i] = updated
                    self.save(tasks)
                    return updated
            return None

    def delete(self, task_id: str) -> bool:
        from .memory_lock import memory_write_lock

        with memory_write_lock():
            tasks = self.load()
            filtered = [t for t in tasks if t.id != task_id]
            if len(filtered) == len(tasks):
                return False
            self.save(filtered)
            return True


# ---------------------------------------------------------------------------
# Trigger helpers
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^(\d+)([smhdw])$")


def _is_cron_expression(schedule: str) -> bool:
    """Return True if the string looks like a 5-field cron expression."""
    parts = schedule.strip().split()
    return len(parts) == 5


def parse_trigger(schedule: str, tz: str = "UTC"):
    """Parse a schedule string into an APScheduler trigger.

    Supports:
    - Cron expressions: "0 9 * * *" (5 fields)
    - Interval shorthand: "30s", "5m", "2h", "1d", "1w"
    """
    schedule = schedule.strip()

    if _is_cron_expression(schedule):
        return CronTrigger.from_crontab(schedule, timezone=tz)

    match = _INTERVAL_RE.match(schedule)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        kwargs = {
            "s": {"seconds": val},
            "m": {"minutes": val},
            "h": {"hours": val},
            "d": {"days": val},
            "w": {"weeks": val},
        }[unit]
        return IntervalTrigger(**kwargs)

    raise ValueError(
        f"Invalid schedule '{schedule}'. Use a cron expression (e.g. '0 9 * * *') "
        f"or an interval (e.g. '30m', '4h', '1d', '1w')."
    )


def _parse_cadence_time(value: Optional[str]) -> tuple[int, int]:
    """Parse an HH:MM cadence anchor, defaulting an omitted value to 03:00."""
    value = value or "03:00"
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid cadence time {value!r}; expected HH:MM") from None
    return parsed.hour, parsed.minute


def cadence_trigger(
    every: Optional[str],
    *,
    at: Optional[str] = None,
    timezone_name: Optional[str] = None,
):
    """Build an anchored interval trigger, or a daily trigger when omitted."""
    tz_name = timezone_name or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Invalid cadence timezone {tz_name!r}") from None

    hour, minute = _parse_cadence_time(at)
    if every is None:
        return CronTrigger(hour=hour, minute=minute, timezone=tz)

    match = _INTERVAL_RE.fullmatch(every.strip())
    if not match or int(match.group(1)) < 1:
        raise ValueError(
            f"Invalid cadence {every!r}; expected Ns, Nm, Nh, Nd, or Nw"
        )

    value = int(match.group(1))
    unit = match.group(2)
    # A one-day cadence means "every local night at this wall-clock time",
    # not a fixed 24-hour interval that drifts across daylight-saving changes.
    if unit == "d" and value == 1:
        return CronTrigger(hour=hour, minute=minute, timezone=tz)
    interval = {
        "s": {"seconds": value},
        "m": {"minutes": value},
        "h": {"hours": value},
        "d": {"days": value},
        "w": {"weeks": value},
    }[unit]
    # A fixed historical anchor keeps interval phase stable across restarts
    # without baking the current deployment year into scheduling behavior.
    anchor = datetime(1970, 1, 1, hour, minute, tzinfo=tz)
    return IntervalTrigger(**interval, start_date=anchor, timezone=tz)


def heartbeat_trigger(
    every: str,
    *,
    at: Optional[str] = None,
    timezone_name: Optional[str] = None,
):
    """Build a heartbeat trigger from either cron or anchored interval syntax."""
    if _is_cron_expression(every):
        return parse_trigger(every, timezone_name or "UTC")
    return cadence_trigger(every, at=at, timezone_name=timezone_name)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

SYSTEM_HEARTBEAT_ID = "system:heartbeat"
SYSTEM_DREAM_ID = "system:dream"
SYSTEM_PROTECTED_IDS = frozenset({SYSTEM_HEARTBEAT_ID, SYSTEM_DREAM_ID})


def count_dream_activity_runs(
    store: RunLogStore,
    *,
    since: Optional[str] = None,
) -> int:
    """Count meaningful top-level runs eligible for the agentic dream gate."""
    from .classify import is_trivial_message

    rows = store.query_runs(since=since, limit=10_000)
    count = 0
    for row in rows:
        mode = row.get("mode") or ""
        if mode in {"dream", "plan"} or row.get("parent_run_id"):
            continue
        if mode == "chat" and is_trivial_message(row.get("task")):
            continue
        if mode == "heartbeat" and row.get("preflight_intent") == "pass":
            continue
        if not any(
            (
                row.get("num_steps"),
                row.get("num_tool_calls"),
                row.get("total_tokens"),
                (row.get("task") or "").strip(),
                (row.get("result") or "").strip(),
            )
        ):
            continue
        count += 1
    return count


def has_sufficient_dream_activity(
    workspace: Path,
    store: RunLogStore,
    *,
    min_new_runs: int,
) -> tuple[bool, int]:
    """Check activity since the last completed dream recorded in status."""
    from .memory.dream import read_dream_status

    status = read_dream_status(workspace) or {}
    since = status.get("last_dream_at") or status.get("completed_at")
    count = count_dream_activity_runs(store, since=since)
    return count >= min_new_runs, count


class AgentScheduler:
    """Manages recurring scheduled tasks for the agent."""

    def __init__(self, store_path: Path):
        self.store = TaskStore(store_path)
        self._scheduler = AsyncIOScheduler()
        self._agent = None  # set in start()

    async def start(self, agent) -> None:
        """Load persisted tasks, register them with APScheduler, and start."""
        self._agent = agent

        # Register user-created tasks
        for task in self.store.load():
            if task.enabled:
                self._register_job(task)

        # Register system tasks
        config = agent.config
        if config.heartbeat.enabled:
            self._register_heartbeat(config.heartbeat)
        if config.dream.enabled:
            self._register_dream(config.dream)
        self._scheduler.start()
        task_count = len(self.store.load())
        logger.debug(
            "Scheduler started: %d user task(s), heartbeat=%s, dream=%s",
            task_count,
            "enabled" if config.heartbeat.enabled else "disabled",
            (
                (
                    f"every {config.dream.every}"
                    if config.dream.every
                    else f"daily@{config.dream.at}"
                )
                if config.dream.enabled
                else "disabled"
            ),
        )

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def next_run_times(self) -> dict[str, datetime]:
        """Job id → next fire time for registered jobs (empty before start)."""
        if not self._scheduler.running:
            return {}
        return {job.id: job.next_run_time for job in self._scheduler.get_jobs()}

    # -- CRUD ----------------------------------------------------------------

    def add_task(self, task: ScheduledTask) -> ScheduledTask:
        current = self.store.load()
        if len(current) >= MAX_TASKS:
            raise ValueError(f"Maximum of {MAX_TASKS} scheduled tasks reached.")
        # Validate the schedule before persisting
        parse_trigger(task.schedule, task.timezone)
        self.store.add(task)
        if task.enabled:
            self._register_job(task)
        return task

    def update_task(self, task_id: str, **kwargs: Any) -> Optional[ScheduledTask]:
        existing_task = self.store.get(task_id)
        if not existing_task:
            return None
        # If schedule changes, validate the new one first
        if "schedule" in kwargs:
            tz = kwargs.get("timezone") or existing_task.timezone
            parse_trigger(kwargs["schedule"], tz)

        updated = self.store.update(task_id, **kwargs)
        if updated:
            # Re-register (or remove) the APScheduler job
            job_id = f"task:{task_id}"
            existing = self._scheduler.get_job(job_id)
            if existing:
                self._scheduler.remove_job(job_id)
            if updated.enabled:
                self._register_job(updated)
        return updated

    def remove_task(self, task_id: str) -> bool:
        job_id = f"task:{task_id}"
        existing = self._scheduler.get_job(job_id)
        if existing:
            self._scheduler.remove_job(job_id)
        return self.store.delete(task_id)

    def list_tasks(self) -> list[ScheduledTask]:
        return self.store.load()

    # -- Internal ------------------------------------------------------------

    def _register_job(self, task: ScheduledTask) -> None:
        try:
            trigger = parse_trigger(task.schedule, task.timezone)
        except ValueError:
            logger.warning(
                "Skipping task '%s' with invalid schedule: %s", task.name, task.schedule
            )
            return

        self._scheduler.add_job(
            self._execute_task,
            trigger=trigger,
            id=f"task:{task.id}",
            args=[task.id],
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
        )
        logger.debug("Registered scheduled task: %s (%s)", task.name, task.schedule)

    def _register_heartbeat(self, heartbeat_config) -> None:
        active_hours = heartbeat_config.active_hours or {}
        try:
            trigger = heartbeat_trigger(
                heartbeat_config.every,
                at=active_hours.get("start") or "00:00",
                timezone_name=active_hours.get("timezone"),
            )
        except ValueError:
            logger.error("Invalid heartbeat schedule: %s", heartbeat_config.every)
            return

        job = self._scheduler.add_job(
            self._execute_heartbeat,
            trigger=trigger,
            id=SYSTEM_HEARTBEAT_ID,
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
            next_run_time=trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        )
        
        next_run = job.next_run_time if hasattr(job, "next_run_time") else None
        next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else "unknown"

        logger.debug(
            "Registered heartbeat: every %s; %s; next_run=%s",
            heartbeat_config.every,
            format_active_period_status(heartbeat_config),
            next_run_str,
        )

    async def _execute_heartbeat(self) -> None:
        if not self._agent:
            return
        try:
            from .modes.heartbeat import is_within_active_hours
            if not is_within_active_hours(self._agent.config.heartbeat):
                logger.info("Outside active hours, skipping heartbeat")
                # Don't log next_run_time here, since the next trigger will also be skipped
                # until we actually enter active hours.
                return

            logger.info("Running heartbeat...")
            import ouro_agents.server as server_module
            from .display import get_display

            server_module.last_heartbeat = datetime.now(timezone.utc)
            await self._agent.heartbeat()
            get_display().flush_pending_run_summary()
            
            job = self._scheduler.get_job(SYSTEM_HEARTBEAT_ID)
            if job and hasattr(job, "next_run_time") and job.next_run_time:
                logger.info("Next heartbeat scheduled for: %s", job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        except RunCancelled:
            logger.info("Heartbeat cancelled")
        except Exception:
            logger.exception("Heartbeat failed")

    def _register_dream(self, dream_config) -> None:
        heartbeat_hours = (
            getattr(self._agent.config.heartbeat, "active_hours", None) or {}
            if self._agent is not None
            else {}
        )
        timezone_name = dream_config.timezone or heartbeat_hours.get("timezone")
        try:
            trigger = cadence_trigger(
                dream_config.every,
                at=dream_config.at,
                timezone_name=timezone_name,
            )
        except ValueError as exc:
            logger.error("Invalid dream cadence: %s", exc)
            return
        self._scheduler.add_job(
            self._execute_dream,
            trigger=trigger,
            id=SYSTEM_DREAM_ID,
            max_instances=1,
            misfire_grace_time=600,
            replace_existing=True,
        )
        logger.debug(
            "Registered dream cycle: cadence=%s, at=%s, timezone=%s",
            dream_config.every or "daily",
            dream_config.at,
            timezone_name or "UTC",
        )


    async def _execute_dream(self) -> None:
        if not self._agent:
            return
        try:
            agent = self._agent
            workspace = agent.config.agent.workspace
            dream_config = agent.config.dream

            ready, run_count = has_sufficient_dream_activity(
                workspace,
                agent._run_log,
                min_new_runs=dream_config.min_new_runs,
            )
            if not ready:
                logger.info(
                    "Dream: skipping cycle (%d/%d new runs)",
                    run_count,
                    dream_config.min_new_runs,
                )
                return

            logger.info("Running dream cycle after %d new runs...", run_count)
            dream_kwargs: dict[str, Any] = {"mode": "scheduled"}
            if dream_config.dry_run:
                dream_kwargs["dry_run"] = True
            # dream() owns an asyncio.run(agent.run(...)) call, so keep it off
            # APScheduler's live event-loop thread.
            await asyncio.to_thread(agent.dream, **dream_kwargs)
            logger.info("Dream cycle complete")
        except Exception:
            logger.exception("Dream cycle failed")


    async def _execute_task(self, task_id: str) -> None:
        if not self._agent:
            return

        task = self.store.get(task_id)
        if not task or not task.enabled:
            return

        self.store.update(task_id, last_run_status="running")
        conversation_id = f"scheduled-{task.id}"

        try:
            from .task_learnings import format_learnings_for_prompt

            effective_prompt = task.prompt + format_learnings_for_prompt(task.learnings)

            logger.info(
                "Running scheduled task '%s' (run #%d)...",
                task.name,
                task.run_count + 1,
            )
            from .config import RunMode
            from .display import get_display

            result = await self._agent.run(
                task=effective_prompt,
                conversation_id=conversation_id,
                mode=RunMode.AUTONOMOUS,
                skip_memory=False,
                team_id=task.team_id,
                preemptible=True,
            )
            get_display().flush_pending_run_summary()
            self.store.update(
                task_id,
                last_run_at=datetime.now(timezone.utc).isoformat(),
                last_run_status="success",
                last_error=None,
                run_count=task.run_count + 1,
            )
            logger.info(
                "Scheduled task '%s' completed: %s", task.name, str(result)[:200]
            )

            # Post-run refinement: learn from this execution
            self._run_refinement(task)

        except RunCancelled as e:
            self.store.update(
                task_id,
                last_run_at=datetime.now(timezone.utc).isoformat(),
                last_run_status="cancelled",
                last_error=str(e) or "cancelled",
                run_count=task.run_count + 1,
            )
            logger.info("Scheduled task '%s' cancelled", task.name)
        except Exception as e:
            self.store.update(
                task_id,
                last_run_at=datetime.now(timezone.utc).isoformat(),
                last_run_status="error",
                last_error=str(e),
                run_count=task.run_count + 1,
            )
            logger.exception("Scheduled task '%s' failed", task.name)

            # Still refine on failure — errors are the most valuable learnings
            self._run_refinement(task)

    def _run_refinement(self, task: ScheduledTask) -> None:
        """Run a cheap LLM call to extract learnings from the last execution."""
        if not self._agent:
            return

        try:
            from .task_learnings import apply_learnings, refine

            conversations_dir = self._agent.config.agent.workspace / "conversations"
            conversation_id = f"scheduled-{task.id}"

            # Use the cheap model (same one used for classification/reflection)
            model = self._agent._build_model(
                self._agent._utility_model_id(),
                role="utility",
            )

            result = refine(
                original_prompt=task.prompt,
                existing_learnings=task.learnings,
                conversations_dir=conversations_dir,
                conversation_id=conversation_id,
                model=model,
            )

            if result.new_learnings or result.drop_learnings:
                updated_learnings = apply_learnings(task.learnings, result)
                self.store.update(task.id, learnings=updated_learnings)
                logger.info(
                    "Refined task '%s': +%d/-%d learnings (total: %d). %s",
                    task.name,
                    len(result.new_learnings),
                    len(result.drop_learnings),
                    len(updated_learnings),
                    result.summary,
                )
            elif result.summary:
                logger.info(
                    "Refinement for '%s': %s (no changes)", task.name, result.summary
                )
        except Exception:
            logger.exception("Refinement failed for task '%s'", task.name)
