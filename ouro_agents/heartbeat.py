import logging
import re
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import HeartbeatConfig

logger = logging.getLogger(__name__)


def is_within_active_hours(config: HeartbeatConfig) -> bool:
    """Check if the current time falls within the configured active hours.

    Returns True if no active_hours are configured (always active).
    """
    if not config.active_hours:
        return True

    start_str = config.active_hours.get("start")
    end_str = config.active_hours.get("end")
    tz_str = config.active_hours.get("timezone")

    if not start_str or not end_str:
        return True

    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo(tz_str) if tz_str else None
    except (ImportError, KeyError):
        logger.warning("Invalid timezone %s, treating as always active", tz_str)
        return True

    now = datetime.now(tz) if tz else datetime.now().astimezone()
    start = datetime.strptime(start_str, "%H:%M").time()
    end = datetime.strptime(end_str, "%H:%M").time()

    current_time = now.time()

    if start <= end:
        return start <= current_time <= end
    # Wraps midnight (e.g. 22:00 - 06:00)
    return current_time >= start or current_time <= end


def format_active_period_status(config: HeartbeatConfig) -> str:
    """One-line summary for logging: configured window (if any) and whether now is inside it."""
    if not config.active_hours:
        return "active_period=always"

    start_str = config.active_hours.get("start")
    end_str = config.active_hours.get("end")
    tz_label = config.active_hours.get("timezone") or "local"

    if not start_str or not end_str:
        return "active_period=always (active_hours missing start/end)"

    in_window = is_within_active_hours(config)
    state = "active" if in_window else "inactive"
    return f"period={start_str}–{end_str} ({tz_label}); now={state}"


def start_scheduler(agent, config: HeartbeatConfig):
    from .agent import OuroAgent

    scheduler = AsyncIOScheduler()

    match = re.match(r"(\d+)([smhd])", config.every)
    if not match:
        logger.error("Invalid heartbeat interval: %s", config.every)
        return

    val = int(match.group(1))
    unit = match.group(2)

    kwargs = {}
    if unit == "s":
        kwargs["seconds"] = val
    elif unit == "m":
        kwargs["minutes"] = val
    elif unit == "h":
        kwargs["hours"] = val
    elif unit == "d":
        kwargs["days"] = val

    trigger = IntervalTrigger(**kwargs)

    async def run_heartbeat():
        active = is_within_active_hours(config)
        if not active:
            logger.info("Outside active hours, skipping heartbeat")
            return

        try:
            logger.info("Running heartbeat...")
            import ouro_agents.server as server_module

            server_module.last_heartbeat = datetime.utcnow()

            await agent.heartbeat()
        except Exception as e:
            logger.error("Heartbeat failed: %s", e)

    scheduler.add_job(run_heartbeat, trigger)
    scheduler.start()
    logger.info("Started heartbeat scheduler: every %s", config.every)
