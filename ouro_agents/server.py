from contextlib import nullcontext
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from .agent import OuroAgent
from .config import OuroAgentsConfig, RunMode
from .events import EventRunContext, build_event_run_context
from .publisher import OuroReplyPublisher

app = FastAPI(title="Ouro Agents Server")
logger = logging.getLogger(__name__)

# Global state
agent_instance: Optional[OuroAgent] = None
reply_publisher: Optional[OuroReplyPublisher] = None
last_heartbeat: Optional[datetime] = None
start_time: datetime = datetime.utcnow()
session_threads: Dict[str, str] = {}


class RunRequest(BaseModel):
    task: str
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    mode: Optional[str] = None
    user_id: Optional[str] = None


def _get_ouro_client_env(config: OuroAgentsConfig) -> Dict[str, str]:
    for server in config.mcp_servers:
        if server.name == "ouro" and server.env:
            return server.env
    return {}


@app.on_event("startup")
async def startup_event():
    global agent_instance, reply_publisher
    config = OuroAgentsConfig.load_from_file("config.json")
    agent_instance = OuroAgent(config)
    agent_instance.connect_mcp()
    ouro_env = _get_ouro_client_env(config)
    reply_publisher = OuroReplyPublisher(
        api_key=ouro_env.get("OURO_API_KEY"),
        base_url=ouro_env.get("OURO_BASE_URL") or ouro_env.get("OURO_BACKEND_URL"),
    )
    logger.info("Reply publisher config: %s", reply_publisher.describe_config())
    reply_publisher.ensure_ready()
    logger.info(
        "Reply publisher authenticated to %s as %s",
        reply_publisher.client.base_url,
        getattr(reply_publisher.client.user, "email", "unknown"),
    )

    if config.heartbeat.enabled:
        from .heartbeat import start_scheduler

        start_scheduler(agent_instance, config.heartbeat)


@app.on_event("shutdown")
async def shutdown_event():
    if agent_instance:
        agent_instance.close()


def _should_publish_event_result(event_run: EventRunContext, result: str) -> bool:
    if not event_run.reply_target_type or not event_run.reply_target_id:
        return False
    reply = result.strip()
    if not reply:
        return False
    return reply.upper() != "NO_ACTION"


def _make_activity_callback(event_run: EventRunContext):
    if (
        not reply_publisher
        or event_run.event_type != "new-message"
        or not event_run.conversation_id
        or not event_run.user_id
    ):
        return None

    def _callback(status: str, message: Optional[str], active: bool) -> None:
        reply_publisher.emit_activity(
            recipient_id=event_run.user_id,
            conversation_id=event_run.conversation_id,
            status=status,
            active=active,
            message=message,
        )

    return _callback


def _make_response_callback(event_run: EventRunContext, message_id: str):
    if (
        not reply_publisher
        or event_run.event_type != "new-message"
        or not event_run.conversation_id
        or not event_run.user_id
    ):
        return None

    state = {"has_started_typing": False, "has_streamed": False}

    def _callback(content: str) -> None:
        state["has_streamed"] = True
        reply_publisher.emit_llm_response(
            recipient_id=event_run.user_id,
            conversation_id=event_run.conversation_id,
            content=content,
            message_id=message_id,
        )
        if not state["has_started_typing"]:
            state["has_started_typing"] = True
            reply_publisher.emit_activity(
                recipient_id=event_run.user_id,
                conversation_id=event_run.conversation_id,
                status="typing",
                active=True,
            )

    return _callback, state


async def _run_event_task(event_run: EventRunContext) -> None:
    if not agent_instance:
        logger.warning("Skipping event run because the agent is not initialized")
        return

    stream_message_id = str(uuid4())
    activity_callback = _make_activity_callback(event_run)
    response_callback, response_state = _make_response_callback(
        event_run, stream_message_id
    ) if (
        reply_publisher
        and event_run.event_type == "new-message"
        and event_run.conversation_id
        and event_run.user_id
    ) else (None, {"has_streamed": False})

    try:
        with (
            reply_publisher.realtime_session()
            if (activity_callback or response_callback) and reply_publisher
            else nullcontext()
        ):
            if activity_callback:
                activity_callback("thinking", "is thinking about it...", True)

            result = await agent_instance.run(
                task=event_run.task,
                conversation_id=event_run.conversation_id,
                mode=event_run.mode,
                user_id=event_run.user_id,
                status_callback=activity_callback,
                response_callback=response_callback,
            )

            created = None
            if reply_publisher and _should_publish_event_result(event_run, result):
                created = reply_publisher.publish(
                    reply_target_type=event_run.reply_target_type,
                    reply_target_id=event_run.reply_target_id,
                    reply_text=result,
                    message_id=stream_message_id,
                )

            if (
                response_callback
                and reply_publisher
                and (response_state["has_streamed"] or isinstance(created, dict))
            ):
                reply_publisher.emit_llm_response_end(
                    recipient_id=event_run.user_id,
                    conversation_id=event_run.conversation_id,
                    message_id=stream_message_id,
                    message=created if isinstance(created, dict) else None,
                )

            if activity_callback:
                activity_callback("typing", None, False)
    except Exception:
        if activity_callback:
            activity_callback("typing", None, False)
        logger.exception("Failed to process webhook event: %s", event_run.event_type)


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "uptime_seconds": (datetime.utcnow() - start_time).total_seconds(),
        "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
        "agent_name": agent_instance.config.agent.name if agent_instance else None,
    }


@app.post("/run")
async def run_task(request: RunRequest):
    if not agent_instance:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        conversation_id = request.conversation_id
        if not conversation_id:
            if request.session_id and request.session_id in session_threads:
                conversation_id = session_threads[request.session_id]
            else:
                conversation_id = str(uuid4())
                if request.session_id:
                    session_threads[request.session_id] = conversation_id

        mode = RunMode(request.mode) if request.mode else RunMode.AUTONOMOUS
        result = await agent_instance.run(
            task=request.task,
            conversation_id=conversation_id,
            mode=mode,
            user_id=request.user_id,
        )
        return {
            "status": "success",
            "result": result,
            "conversation_id": conversation_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events")
async def handle_event(body: Dict[str, Any], background_tasks: BackgroundTasks):
    """Webhook receiver for Ouro platform events."""
    if not agent_instance:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        event_run = build_event_run_context(body)
    except Exception as exc:
        logger.warning("Invalid webhook payload: %s", body)
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}")

    background_tasks.add_task(_run_event_task, event_run)

    return {"status": "accepted", "event_type": event_run.event_type}


def start_server(config_path: str = "config.json"):
    config = OuroAgentsConfig.load_from_file(config_path)
    uvicorn.run(
        "ouro_agents.server:app",
        host=config.server.host,
        port=config.server.port,
        reload=True,
    )
