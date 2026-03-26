"""General-purpose task scheduler for ouro-agents.

Extends the existing APScheduler heartbeat infrastructure into a
full scheduler that the agent can use to create and manage its own
recurring tasks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, Field

from .modes.heartbeat import format_active_period_status

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
        tasks = self.load()
        tasks.append(task)
        self.save(tasks)

    def update(self, task_id: str, **kwargs: Any) -> Optional[ScheduledTask]:
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
        tasks = self.load()
        filtered = [t for t in tasks if t.id != task_id]
        if len(filtered) == len(tasks):
            return False
        self.save(filtered)
        return True


# ---------------------------------------------------------------------------
# Trigger helpers
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^(\d+)([smhd])$")


def _is_cron_expression(schedule: str) -> bool:
    """Return True if the string looks like a 5-field cron expression."""
    parts = schedule.strip().split()
    return len(parts) == 5


def parse_trigger(schedule: str, tz: str = "UTC"):
    """Parse a schedule string into an APScheduler trigger.

    Supports:
    - Cron expressions: "0 9 * * *" (5 fields)
    - Interval shorthand: "30s", "5m", "2h", "1d"
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
        }[unit]
        return IntervalTrigger(**kwargs)

    raise ValueError(
        f"Invalid schedule '{schedule}'. Use a cron expression (e.g. '0 9 * * *') "
        f"or an interval (e.g. '30m', '4h', '1d')."
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

SYSTEM_HEARTBEAT_ID = "system:heartbeat"
SYSTEM_CONSOLIDATION_ID = "system:consolidation"
SYSTEM_PROTECTED_IDS = frozenset({SYSTEM_HEARTBEAT_ID, SYSTEM_CONSOLIDATION_ID})


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
        if config.memory.consolidation_enabled:
            self._register_consolidation(config.memory)

        self._scheduler.start()
        task_count = len(self.store.load())
        logger.info(
            "Scheduler started: %d user task(s), heartbeat=%s, consolidation=%s",
            task_count,
            "enabled" if config.heartbeat.enabled else "disabled",
            (
                config.memory.consolidation_schedule
                if config.memory.consolidation_enabled
                else "disabled"
            ),
        )

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

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
        # If schedule changes, validate the new one first
        if "schedule" in kwargs:
            tz = (
                kwargs.get("timezone")
                or (
                    self.store.get(task_id) or ScheduledTask(name="", prompt="")
                ).timezone
            )
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
        logger.info("Registered scheduled task: %s (%s)", task.name, task.schedule)

    def _register_heartbeat(self, heartbeat_config) -> None:
        from datetime import datetime

        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        from .config import HeartbeatConfig

        match = _INTERVAL_RE.match(heartbeat_config.every)
        if not match:
            logger.error("Invalid heartbeat interval: %s", heartbeat_config.every)
            return

        val = int(match.group(1))
        unit = match.group(2)

        start_hour = 0
        start_minute = 0
        if heartbeat_config.active_hours and "start" in heartbeat_config.active_hours:
            try:
                start_time = datetime.strptime(
                    heartbeat_config.active_hours["start"], "%H:%M"
                ).time()
                start_hour = start_time.hour
                start_minute = start_time.minute
            except Exception:
                pass

        if unit == "d":
            trigger = CronTrigger(day=f"*/{val}", hour=start_hour, minute=start_minute)
        else:
            kwargs = {
                "s": {"seconds": val},
                "m": {"minutes": val},
                "h": {"hours": val},
            }[unit]

            tz = None
            if (
                heartbeat_config.active_hours
                and "timezone" in heartbeat_config.active_hours
            ):
                try:
                    import zoneinfo

                    tz = zoneinfo.ZoneInfo(heartbeat_config.active_hours["timezone"])
                except Exception:
                    pass

            # Anchor date in the past to align intervals to the start time
            anchor = datetime(2026, 1, 1, start_hour, start_minute, tzinfo=tz)
            trigger = IntervalTrigger(**kwargs, start_date=anchor)

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
        
        logger.info(
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
                job = self._scheduler.get_job(SYSTEM_HEARTBEAT_ID)
                if job and hasattr(job, "next_run_time") and job.next_run_time:
                    logger.info("Next heartbeat scheduled for: %s", job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z"))
                return

            logger.info("Running heartbeat...")
            import ouro_agents.server as server_module
            from .display import get_display

            server_module.last_heartbeat = datetime.utcnow()
            await self._agent.heartbeat()
            get_display().flush_pending_run_summary()
            
            job = self._scheduler.get_job(SYSTEM_HEARTBEAT_ID)
            if job and hasattr(job, "next_run_time") and job.next_run_time:
                logger.info("Next heartbeat scheduled for: %s", job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        except Exception:
            logger.exception("Heartbeat failed")

    def _register_consolidation(self, memory_config) -> None:
        try:
            trigger = parse_trigger(memory_config.consolidation_schedule)
        except ValueError:
            logger.error(
                "Invalid consolidation schedule: %s",
                memory_config.consolidation_schedule,
            )
            return

        self._scheduler.add_job(
            self._execute_consolidation,
            trigger=trigger,
            id=SYSTEM_CONSOLIDATION_ID,
            max_instances=1,
            misfire_grace_time=600,
            replace_existing=True,
        )
        logger.info(
            "Registered consolidation: %s",
            memory_config.consolidation_schedule,
        )

    async def _execute_consolidation(self) -> None:
        if not self._agent:
            return
        try:
            logger.info("Running memory consolidation...")
            from .memory.consolidation import run_consolidation

            agent = self._agent
            hb_model = agent._build_model(
                agent.config.heartbeat.model or agent.config.agent.model,
                heartbeat=True,
            )
            results = run_consolidation(
                workspace=agent.config.agent.workspace,
                backend=agent.memory,
                agent_id=agent.config.agent.name,
                config=agent.config.memory,
                model=hb_model,
                doc_store=agent.doc_store,
            )
            logger.info("Memory consolidation complete: %s", results)
        except Exception:
            logger.exception("Memory consolidation failed")

    async def _execute_task(self, task_id: str) -> None:
        if not self._agent:
            return

        task = self.store.get(task_id)
        if not task or not task.enabled:
            return

        self.store.update(task_id, last_run_status="running")
        conversation_id = f"scheduled-{task.id}"

        # Inject learnings from previous runs into the prompt
        from .refinement import format_learnings_for_prompt

        effective_prompt = task.prompt + format_learnings_for_prompt(task.learnings)

        try:
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
                skip_memory=True,
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
            from .refinement import apply_learnings, refine

            conversations_dir = self._agent.config.agent.workspace / "conversations"
            conversation_id = f"scheduled-{task.id}"

            # Use the cheap model (same one used for classification/reflection)
            model = self._agent._build_model(
                self._agent.config.heartbeat.model or self._agent.config.agent.model,
                heartbeat=True,
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
