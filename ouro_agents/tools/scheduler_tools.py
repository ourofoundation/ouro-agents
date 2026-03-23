"""Agent-facing tools for managing scheduled tasks."""

from __future__ import annotations

import json
from typing import Optional

from smolagents import tool

from ..scheduler import AgentScheduler, ScheduledTask, SYSTEM_HEARTBEAT_ID


def make_scheduler_tools(scheduler: AgentScheduler) -> list:
    """Create the CRUD tools the agent uses to manage its own schedule."""

    @tool
    def create_scheduled_task(
        name: str,
        prompt: str,
        schedule: str,
        timezone: str = "UTC",
    ) -> str:
        """Create a new recurring scheduled task that will run automatically.

        Args:
            name: Human-readable name (e.g. "Daily AI Safety Research")
            prompt: The full instruction to execute each time the task runs
            schedule: Cron expression (e.g. "0 9 * * *" for daily at 9am) or interval (e.g. "4h", "30m", "1d")
            timezone: IANA timezone for cron schedules (default UTC). Use e.g. "America/New_York", "Europe/London"
        """
        try:
            task = ScheduledTask(
                name=name,
                prompt=prompt,
                schedule=schedule,
                timezone=timezone,
            )
            created = scheduler.add_task(task)
            return json.dumps({
                "status": "created",
                "task_id": created.id,
                "name": created.name,
                "schedule": created.schedule,
                "timezone": created.timezone,
                "next_hint": "The task is now active and will run on its schedule.",
            })
        except ValueError as e:
            return json.dumps({"error": str(e)})

    @tool
    def list_scheduled_tasks() -> str:
        """List all scheduled tasks with their current status.

        Args:
        """
        tasks = scheduler.list_tasks()
        if not tasks:
            return json.dumps({"tasks": [], "message": "No scheduled tasks."})
        return json.dumps({
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "schedule": t.schedule,
                    "timezone": t.timezone,
                    "enabled": t.enabled,
                    "last_run_at": t.last_run_at,
                    "last_run_status": t.last_run_status,
                    "run_count": t.run_count,
                    "learnings_count": len(t.learnings),
                    "learnings": t.learnings,
                    "prompt": t.prompt[:100] + ("..." if len(t.prompt) > 100 else ""),
                }
                for t in tasks
            ]
        })

    @tool
    def update_scheduled_task(
        task_id: str,
        name: Optional[str] = None,
        prompt: Optional[str] = None,
        schedule: Optional[str] = None,
        enabled: Optional[bool] = None,
        timezone: Optional[str] = None,
    ) -> str:
        """Update an existing scheduled task.

        Args:
            task_id: The ID of the task to update
            name: New name (optional)
            prompt: New instruction prompt (optional)
            schedule: New schedule - cron expression or interval (optional)
            enabled: Set to false to pause, true to resume (optional)
            timezone: New timezone (optional)
        """
        kwargs = {}
        if name is not None:
            kwargs["name"] = name
        if prompt is not None:
            kwargs["prompt"] = prompt
        if schedule is not None:
            kwargs["schedule"] = schedule
        if enabled is not None:
            kwargs["enabled"] = enabled
        if timezone is not None:
            kwargs["timezone"] = timezone

        if not kwargs:
            return json.dumps({"error": "No fields to update."})

        try:
            updated = scheduler.update_task(task_id, **kwargs)
            if not updated:
                return json.dumps({"error": f"Task '{task_id}' not found."})
            return json.dumps({
                "status": "updated",
                "task_id": updated.id,
                "name": updated.name,
                "schedule": updated.schedule,
                "enabled": updated.enabled,
            })
        except ValueError as e:
            return json.dumps({"error": str(e)})

    @tool
    def delete_scheduled_task(task_id: str) -> str:
        """Delete a scheduled task permanently.

        Args:
            task_id: The ID of the task to delete
        """
        if task_id == SYSTEM_HEARTBEAT_ID:
            return json.dumps({"error": "Cannot delete the system heartbeat task."})

        removed = scheduler.remove_task(task_id)
        if not removed:
            return json.dumps({"error": f"Task '{task_id}' not found."})
        return json.dumps({"status": "deleted", "task_id": task_id})

    return [create_scheduled_task, list_scheduled_tasks, update_scheduled_task, delete_scheduled_task]
