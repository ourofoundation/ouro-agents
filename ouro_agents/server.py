import logging
import os
from contextlib import nullcontext
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from ouro.resources.conversations import Messages
from ouro_mcp.utils import content_from_markdown
from pydantic import BaseModel

from .agent import OuroAgent
from .config import OuroAgentsConfig, RunMode
from .display import OuroDisplay, get_display, set_display
from .events import EventRunContext, build_event_run_context
from .logging_config import uvicorn_log_config
from .observer import AgentObserver
from .provenance import resolve_event_focus_asset, resolve_event_provenance
from .publisher import OuroReplyPublisher
from .utils.message_persistence import (
    build_persistence_reasoning_callback,
    build_persistence_step_callback,
)
from .uuid_v7 import uuid7_str

if TYPE_CHECKING:
    from ouro.client import Ouro

app = FastAPI(title="Ouro Agents Server")
logger = logging.getLogger(__name__)

# Global state
agent_instance: Optional[OuroAgent] = None
reply_publisher: Optional[OuroReplyPublisher] = None
last_heartbeat: Optional[datetime] = None
start_time: datetime = datetime.utcnow()
session_threads: Dict[str, str] = {}
REALTIME_CHAT_EVENT_TYPES = {"new-message"}


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
    set_display(
        OuroDisplay(
            show_reasoning_in_summary=config.display.usage_table.show_reasoning
        )
    )
    agent_instance = OuroAgent(config)
    agent_instance.connect_mcp()
    ouro_client = agent_instance._get_ouro_client()
    if ouro_client:
        reply_publisher = OuroReplyPublisher(client=ouro_client)
    else:
        ouro_env = _get_ouro_client_env(config)
        reply_publisher = OuroReplyPublisher(
            api_key=ouro_env.get("OURO_API_KEY"),
            base_url=ouro_env.get("OURO_BASE_URL") or ouro_env.get("OURO_BACKEND_URL"),
        )
    logger.info("Reply publisher config: %s", reply_publisher.describe_config())
    reply_publisher.ensure_ready()
    logger.info(
        "Reply publisher ready: %s as %s",
        reply_publisher.client.base_url,
        getattr(reply_publisher.client.user, "email", "unknown"),
    )

    app.add_api_route(config.server.webhook_path, handle_event, methods=["POST"])

    await agent_instance.scheduler.start(agent_instance)


@app.on_event("shutdown")
async def shutdown_event():
    if agent_instance:
        agent_instance.scheduler.stop()
        agent_instance.close()


class ServerAgentObserver(AgentObserver):
    def __init__(
        self,
        event_run: EventRunContext,
        stream_message_id: str,
        reply_publisher: Optional[OuroReplyPublisher],
    ):
        self.event_run = event_run
        self.stream_message_id = stream_message_id
        self.reply_publisher = reply_publisher
        self.state = {"has_started_typing": False, "has_streamed": False}
        self.persisted_message_ref = []

        self.persist_step_cb = (
            build_persistence_step_callback(
                reply_publisher.client, event_run.conversation_id
            )
            if event_run.conversation_id and reply_publisher
            else None
        )

        self.persist_reasoning_cb = (
            build_persistence_reasoning_callback(
                reply_publisher.client, event_run.conversation_id
            )
            if event_run.conversation_id and reply_publisher
            else None
        )

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        if (
            not self.reply_publisher
            or self.event_run.event_type not in REALTIME_CHAT_EVENT_TYPES
        ):
            return
        if not self.event_run.conversation_id or not self.event_run.user_id:
            return
        self.reply_publisher.emit_activity(
            recipient_id=self.event_run.user_id,
            conversation_id=self.event_run.conversation_id,
            status=status,
            active=active,
            message=message,
        )

    def on_stream_chunk(self, chunk: str) -> None:
        if (
            not self.reply_publisher
            or self.event_run.event_type not in REALTIME_CHAT_EVENT_TYPES
        ):
            return
        if not self.event_run.conversation_id or not self.event_run.user_id:
            return
        self.state["has_streamed"] = True

        if not self.state["has_started_typing"]:
            self.state["has_started_typing"] = True
            self.reply_publisher.emit_activity(
                recipient_id=self.event_run.user_id,
                conversation_id=self.event_run.conversation_id,
                status="typing",
                active=True,
            )

        self.reply_publisher.emit_llm_response(
            recipient_id=self.event_run.user_id,
            conversation_id=self.event_run.conversation_id,
            content=chunk,
            message_id=self.stream_message_id,
        )

    def on_result_ready(self, result_text: str) -> None:
        msg = None
        if (
            self.event_run.conversation_id
            and self.reply_publisher
            and result_text
            and result_text != "NO_ACTION"
        ):
            ouro = self.reply_publisher.client
            content = content_from_markdown(ouro, result_text)
            msg = Messages(ouro).create(
                self.event_run.conversation_id,
                id=self.stream_message_id,
                type="message",
                text=content.text,
                json=content.json,
            )
            self.persisted_message_ref.append(msg)

        if (
            self.reply_publisher
            and self.event_run.event_type in REALTIME_CHAT_EVENT_TYPES
        ):
            self.reply_publisher.emit_llm_response_end(
                recipient_id=self.event_run.user_id,
                conversation_id=self.event_run.conversation_id,
                message_id=self.stream_message_id,
                message=msg,
            )

    def on_step_persist(self, step: dict) -> None:
        if self.persist_step_cb:
            self.persist_step_cb(step)

    def on_reasoning_persist(self, content: str) -> None:
        if self.persist_reasoning_cb:
            self.persist_reasoning_cb(content)


import asyncio


async def _run_event_task(event_run: EventRunContext) -> None:
    if not agent_instance:
        logger.warning("Skipping event run because the agent is not initialized")
        return

    # Attempt to mark related notifications as read so heartbeat doesn't process them again
    try:
        ouro = agent_instance._get_ouro_client()
        if ouro and event_run.source_id:
            # Wait briefly to ensure the notification was created by the backend
            await asyncio.sleep(2)
            unreads = ouro.notifications.list(unread_only=True, limit=50)
            if isinstance(unreads, dict):
                unreads = unreads.get("data", [])

            for n in unreads:
                n_asset_id = n.get("asset_id")
                content = n.get("content") or {}
                c_asset = content.get("asset") or {}
                if (
                    n_asset_id == event_run.source_id
                    or c_asset.get("assetId") == event_run.source_id
                    or c_asset.get("id") == event_run.source_id
                ):
                    ouro.notifications.read(n.get("id"))
    except Exception as e:
        logger.warning("Failed to mark notification as read: %s", e)

    # Route active/pending plan feedback to the dedicated review path
    prov = event_run.provenance
    if prov and prov.is_plan_feedback:
        try:
            await agent_instance.handle_plan_feedback(event_run)
            get_display().flush_pending_run_summary()
        except Exception:
            logger.exception("Failed to handle plan feedback event")
        return

    if event_run.event_type == "new-conversation":
        # Conversation creation has no user message yet; we do not run the agent.
        return

    stream_message_id = uuid7_str()
    observer = ServerAgentObserver(event_run, stream_message_id, reply_publisher)

    try:
        with (
            reply_publisher.realtime_session()
            if reply_publisher and event_run.event_type in REALTIME_CHAT_EVENT_TYPES
            else nullcontext()
        ):
            observer.on_activity("thinking", "is thinking about it...", True)

            result = await agent_instance.run(
                task=event_run.task,
                conversation_id=event_run.conversation_id,
                mode=event_run.mode,
                user_id=event_run.user_id,
                preload_tools=(
                    list(event_run.preload_tools) if event_run.preload_tools else None
                ),
                prefetch=event_run.prefetch if not event_run.prefetch.empty else None,
                observer=observer,
            )

            observer.on_activity("typing", None, False)
            get_display().flush_pending_run_summary()
    except Exception:
        observer.on_activity("typing", None, False)
        if reply_publisher and event_run.event_type in REALTIME_CHAT_EVENT_TYPES:
            reply_publisher.emit_llm_response_end(
                recipient_id=event_run.user_id,
                conversation_id=event_run.conversation_id,
                message_id=stream_message_id,
                message=None,
            )
        logger.exception("Failed to process webhook event: %s", event_run.event_type)


@app.get("/health")
async def health_check():
    scheduled_tasks = agent_instance.scheduler.list_tasks() if agent_instance else []
    return {
        "status": "ok",
        "uptime_seconds": (datetime.utcnow() - start_time).total_seconds(),
        "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
        "agent_name": agent_instance.config.agent.name if agent_instance else None,
        "scheduled_tasks": len(scheduled_tasks),
    }


@app.get("/tasks")
async def list_tasks():
    """List all scheduled tasks (debug/monitoring endpoint)."""
    if not agent_instance:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    tasks = agent_instance.scheduler.list_tasks()
    return {"tasks": [t.model_dump() for t in tasks]}


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
                conversation_id = uuid7_str()
                if request.session_id:
                    session_threads[request.session_id] = conversation_id

        mode = RunMode(request.mode) if request.mode else RunMode.AUTONOMOUS
        result = await agent_instance.run(
            task=request.task,
            conversation_id=conversation_id,
            mode=mode,
            user_id=request.user_id,
        )
        get_display().flush_pending_run_summary()
        return {
            "status": "success",
            "result": result,
            "conversation_id": conversation_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def handle_event(body: Dict[str, Any], background_tasks: BackgroundTasks):
    """Webhook receiver for Ouro platform events."""
    if not agent_instance:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        # Resolve provenance before building the event context so task
        # framing can be plan-aware from the start.
        event_data = dict(body.get("data", {}) or {})
        body = dict(body)
        body["data"] = event_data
        source_id = event_data.get("source_id")
        focus_asset_id, focus_asset_type = resolve_event_focus_asset(
            source_id=source_id,
            event_data=event_data,
        )
        if focus_asset_id:
            event_data["focus_asset_id"] = focus_asset_id
        if focus_asset_type:
            event_data["focus_asset_type"] = focus_asset_type
        planning_cfg = agent_instance.config.planning
        agent_cfg = agent_instance.config.agent

        provenance = resolve_event_provenance(
            source_id=focus_asset_id or source_id,
            event_data=event_data,
            workspace=agent_cfg.workspace,
            planning_team_id=agent_cfg.team_id,
            planning_org_id=agent_cfg.org_id,
            planning_enabled=planning_cfg.enabled,
        )

        event_run = build_event_run_context(body, provenance=provenance)
    except Exception as exc:
        logger.warning("Invalid webhook payload: %s", body)
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}")

    if provenance and (provenance.is_plan_feedback or provenance.in_planning_space):
        logger.info(
            "Event provenance: plan_feedback=%s historical=%s planning_space=%s",
            provenance.is_plan_feedback,
            provenance.is_historical_plan_feedback,
            provenance.in_planning_space,
        )

    background_tasks.add_task(_run_event_task, event_run)

    return {"status": "accepted", "event_type": event_run.event_type}


def start_server(config_path: str = "config.json"):
    config = OuroAgentsConfig.load_from_file(config_path)
    reload = os.getenv("PYTHON_ENV") != "production"
    reload_excludes = (
        [
            "workspace/*",
            "__pycache__",
        ]
        if reload
        else None
    )
    uvicorn.run(
        "ouro_agents.server:app",
        host=config.server.host,
        port=config.server.port,
        reload=reload,
        reload_excludes=reload_excludes,
        log_config=uvicorn_log_config(),
    )
