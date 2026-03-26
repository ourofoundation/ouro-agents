import logging
import os
from contextlib import nullcontext
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from .agent import OuroAgent
from .config import OuroAgentsConfig, RunMode
from .display import get_display
from .events import EventRunContext, build_event_run_context
from .logging_config import uvicorn_log_config
from .provenance import resolve_event_focus_asset, resolve_event_provenance
from .publisher import OuroReplyPublisher
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


def _make_activity_callback(event_run: EventRunContext):
    if (
        not reply_publisher
        or event_run.event_type not in REALTIME_CHAT_EVENT_TYPES
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
        or event_run.event_type not in REALTIME_CHAT_EVENT_TYPES
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


def _fetch_full_message_for_stream_end(
    ouro: "Ouro",
    conversation_id: str,
    stream_message_id: str,
) -> Optional[dict]:
    """Load the persisted assistant message after a chat turn for llm-response-end payloads.

    Prefer the row whose id matches ``stream_message_id`` (if the client set it on create);
    otherwise the latest message authored by the authenticated agent user.
    """
    from ouro.resources.conversations import Messages

    try:
        msgs = Messages(ouro).list(conversation_id=conversation_id)
    except Exception:
        logger.exception("Failed to list messages for llm-response-end")
        return None
    if not msgs:
        return None
    for m in msgs:
        if str(m.get("id")) == stream_message_id:
            return m
    agent_uid = getattr(ouro.user, "id", None) or getattr(ouro.user, "user_id", None)
    if agent_uid is not None:
        agent_uid = str(agent_uid)
        for m in reversed(msgs):
            if str(m.get("user_id")) == agent_uid:
                return m
    return msgs[-1]


def _resolve_comment_root_asset(comment_id: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a comment ID to its root non-comment parent asset."""
    if not agent_instance:
        return None, None

    try:
        ouro = agent_instance._get_ouro_client()
        asset = ouro.assets.retrieve(comment_id)
        seen: set[str] = set()

        while asset and getattr(asset, "asset_type", None) == "comment":
            asset_id = str(getattr(asset, "id", ""))
            if not asset_id or asset_id in seen:
                logger.warning(
                    "Circular or invalid comment chain while resolving %s", comment_id
                )
                return None, None
            seen.add(asset_id)
            parent_id = getattr(asset, "parent_id", None)
            if not parent_id:
                return None, None
            asset = ouro.assets.retrieve(str(parent_id))

        if not asset:
            return None, None
        return str(getattr(asset, "id", "")) or None, getattr(asset, "asset_type", None)
    except Exception:
        logger.exception("Failed to resolve root asset for comment %s", comment_id)
        return None, None


async def _run_event_task(event_run: EventRunContext) -> None:
    if not agent_instance:
        logger.warning("Skipping event run because the agent is not initialized")
        return

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
    activity_callback = _make_activity_callback(event_run)
    response_callback, response_state = (
        _make_response_callback(event_run, stream_message_id)
        if (
            reply_publisher
            and event_run.event_type in REALTIME_CHAT_EVENT_TYPES
            and event_run.conversation_id
            and event_run.user_id
        )
        else (None, {"has_streamed": False})
    )

    try:
        with (
            reply_publisher.realtime_session()
            if (activity_callback or response_callback) and reply_publisher
            else nullcontext()
        ):
            if activity_callback:
                activity_callback("thinking", "is thinking about it...", True)

            await agent_instance.run(
                task=event_run.task,
                conversation_id=event_run.conversation_id,
                mode=event_run.mode,
                user_id=event_run.user_id,
                status_callback=activity_callback,
                response_callback=response_callback,
                preload_tools=(
                    list(event_run.preload_tools) if event_run.preload_tools else None
                ),
                prefetch=event_run.prefetch if not event_run.prefetch.empty else None,
                reply_message_id=stream_message_id,
            )

            if response_callback and reply_publisher and response_state["has_streamed"]:
                full_message = _fetch_full_message_for_stream_end(
                    reply_publisher.client,
                    event_run.conversation_id,
                    stream_message_id,
                )
                reply_publisher.emit_llm_response_end(
                    recipient_id=event_run.user_id,
                    conversation_id=event_run.conversation_id,
                    message_id=stream_message_id,
                    message=full_message,
                )

            if activity_callback:
                activity_callback("typing", None, False)
            get_display().flush_pending_run_summary()
    except Exception:
        if activity_callback:
            activity_callback("typing", None, False)
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
            resolve_comment_parent=_resolve_comment_root_asset,
        )
        if focus_asset_id:
            event_data["focus_asset_id"] = focus_asset_id
        if focus_asset_type:
            event_data["focus_asset_type"] = focus_asset_type
        planning_cfg = agent_instance.config.planning

        provenance = resolve_event_provenance(
            source_id=focus_asset_id or source_id,
            event_data=event_data,
            workspace=agent_instance.config.agent.workspace,
            planning_team_id=planning_cfg.team_id,
            planning_org_id=planning_cfg.org_id,
            planning_enabled=planning_cfg.enabled,
            resolve_comment_parent=_resolve_comment_root_asset,
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
