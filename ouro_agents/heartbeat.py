from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import asyncio
import logging

from .config import HeartbeatConfig
from .agent import OuroAgent

logger = logging.getLogger(__name__)

def start_scheduler(agent: OuroAgent, config: HeartbeatConfig):
    scheduler = AsyncIOScheduler()
    
    # Parse interval (e.g. "30m" -> 30 minutes)
    import re
    match = re.match(r'(\d+)([smhd])', config.every)
    if not match:
        logger.error(f"Invalid heartbeat interval: {config.every}")
        return
        
    val = int(match.group(1))
    unit = match.group(2)
    
    kwargs = {}
    if unit == 's':
        kwargs['seconds'] = val
    elif unit == 'm':
        kwargs['minutes'] = val
    elif unit == 'h':
        kwargs['hours'] = val
    elif unit == 'd':
        kwargs['days'] = val
        
    trigger = IntervalTrigger(**kwargs)
    
    async def run_heartbeat():
        # Check active hours
        if config.active_hours:
            # TODO: implement timezone-aware active hours check
            pass
            
        try:
            logger.info("Running heartbeat...")
            # We need to update last_heartbeat in server.py
            from .server import last_heartbeat
            import ouro_agents.server as server_module
            server_module.last_heartbeat = datetime.utcnow()
            
            await agent.heartbeat()
        except Exception as e:
            logger.error(f"Heartbeat failed: {e}")

    scheduler.add_job(run_heartbeat, trigger)
    scheduler.start()
    logger.info(f"Started heartbeat scheduler: every {config.every}")
