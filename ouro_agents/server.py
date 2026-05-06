import asyncio
import logging
import os
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from ouro.resources.conversations import Messages
from ouro_mcp.utils import content_from_markdown
from pydantic import BaseModel

from .agent import OuroAgent
from .cancellation import RunCancelled
from .config import OuroAgentsConfig, RunMode
from .display import OuroDisplay, get_display, set_display
from .event_pool import EventPool
from .event_registry import is_chat_event
from .events import EventRunContext, build_event_run_context
from .logging_config import uvicorn_log_config
from .observer import AgentObserver
from .provenance import resolve_event_provenance
from .publisher import OuroReplyPublisher
from .utils.message_persistence import (
    build_persistence_reasoning_callback,
    build_persistence_step_callback,
)
from .uuid_v7 import uuid7_str

if TYPE_CHECKING:
    from ouro.client import Ouro

logger = logging.getLogger(__name__)

# Global state (still module-scoped for now; see P2 "server globals + session map"
# in SDK_IMPROVEMENTS.md for the planned move to request-scoped state).
agent_instance: Optional[OuroAgent] = None
reply_publisher: Optional[OuroReplyPublisher] = None
event_pool: Optional[EventPool] = None
last_heartbeat: Optional[datetime] = None
start_time: datetime = datetime.now(timezone.utc)
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize the agent + reply publisher on startup and tear down on shutdown.

    Replaces the deprecated ``@app.on_event("startup" | "shutdown")`` decorators.
    """
    global agent_instance, reply_publisher, event_pool
    config = OuroAgentsConfig.load_from_file(
        os.environ.get("CONFIG_FILE", "config.json")
    )
    set_display(
        OuroDisplay(show_reasoning_in_summary=config.display.usage_table.show_reasoning)
    )
    agent_instance = OuroAgent(config)
    agent_instance.connect_mcp()
    event_pool = EventPool(config.event_pooling, _run_event_task)
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

    try:
        yield
    finally:
        if agent_instance:
            agent_instance.cancel_active_runs("server shutdown")
        if event_pool:
            await event_pool.stop()
            event_pool = None
        if agent_instance:
            agent_instance.scheduler.stop()
            agent_instance.close()


app = FastAPI(title="Ouro Agents Server", lifespan=lifespan)


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
        if not self.reply_publisher or not is_chat_event(self.event_run.event_type):
            return
        if not self.event_run.conversation_id:
            return
        self.reply_publisher.emit_activity(
            conversation_id=self.event_run.conversation_id,
            status=status,
            active=active,
            message=message,
        )

    def _ensure_typing(self) -> None:
        """Send a single 'typing' activity event the first time text streams."""
        if self.state["has_started_typing"]:
            return
        self.state["has_started_typing"] = True
        if not self.reply_publisher or not self.event_run.conversation_id:
            return
        self.reply_publisher.emit_activity(
            conversation_id=self.event_run.conversation_id,
            status="typing",
            active=True,
        )

    def on_stream_chunk(self, chunk: str) -> None:
        if not self.reply_publisher or not is_chat_event(self.event_run.event_type):
            return
        if not self.event_run.conversation_id:
            return
        self.state["has_streamed"] = True
        self._ensure_typing()

        self.reply_publisher.emit_llm_response(
            conversation_id=self.event_run.conversation_id,
            content=chunk,
            message_id=self.stream_message_id,
        )

    def on_intermediate_chunk(self, message_id: str, chunk: str) -> None:
        """Stream a step's commentary chunk to the conversation websocket.

        Each non-final step gets its own ``message_id`` so the client can
        render commentary as a distinct message that arrives before the
        final reply, matching how chat-style coding agents narrate work.
        """
        if not self.reply_publisher or not is_chat_event(self.event_run.event_type):
            return
        if not self.event_run.conversation_id:
            return
        self._ensure_typing()
        self.reply_publisher.emit_llm_response(
            conversation_id=self.event_run.conversation_id,
            content=chunk,
            message_id=message_id,
        )

    def on_intermediate_end(self, message_id: str, full_text: str) -> None:
        """Persist a step's commentary message and signal end-of-stream.

        ``full_text`` is the concatenation of every chunk emitted under
        ``message_id`` during this step. Persisting at end-of-step (rather
        than on every chunk) keeps writes proportional to the number of
        steps instead of the number of tokens.
        """
        if not self.event_run.conversation_id or not self.reply_publisher:
            return
        text = (full_text or "").strip()
        if not text:
            return
        msg = None
        try:
            ouro = self.reply_publisher.client
            content = content_from_markdown(ouro, text)
            msg = Messages(ouro).create(
                self.event_run.conversation_id,
                id=message_id,
                type="message",
                text=content.text,
                json=content.json,
            )
        except Exception:
            logger.warning(
                "Failed to persist intermediate content message",
                exc_info=True,
            )
        if is_chat_event(self.event_run.event_type):
            self.reply_publisher.emit_llm_response_end(
                conversation_id=self.event_run.conversation_id,
                message_id=message_id,
                message=msg,
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

        if self.reply_publisher and is_chat_event(self.event_run.event_type):
            self.reply_publisher.emit_llm_response_end(
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


async def _run_event_task(event_run: EventRunContext) -> None:
    if not agent_instance:
        logger.warning("Skipping event run because the agent is not initialized")
        return

    # Self-event gate: skip events triggered by this agent to prevent reply loops.
    # The backend should already filter these, but this is a cheap safety net.
    own_id = agent_instance.own_user_id
    if own_id and event_run.actor_user_id == own_id:
        logger.info(
            "Skipping self-triggered %s event (actor=%s)",
            event_run.event_type,
            event_run.actor_user_id,
        )
        return

    # Cleanup events: deterministic, no LLM. Handle synchronously and return
    # before any of the LLM run-path setup.
    if event_run.event_type == "asset.deleted":
        from .cleanup import handle_asset_deleted_webhook

        try:
            await handle_asset_deleted_webhook(agent_instance, event_run)
        except Exception:
            logger.exception("asset.deleted cleanup failed")
        return

    await _mark_event_notifications_read(event_run)

    # Route active/pending plan feedback to the dedicated review path
    prov = event_run.provenance
    if prov and prov.is_plan_feedback:
        try:
            await agent_instance.handle_plan_feedback(event_run)
            get_display().flush_pending_run_summary()
        except RunCancelled:
            logger.info("Cancelled plan feedback event run")
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
            if reply_publisher and is_chat_event(event_run.event_type)
            else nullcontext()
        ):
            observer.on_activity("thinking", "is thinking about it...", True)

            result = await agent_instance.run(
                task=event_run.task,
                conversation_id=event_run.conversation_id,
                mode=event_run.mode,
                user_id=event_run.user_id,
                team_id=event_run.team_id,
                preload_tools=(
                    list(event_run.preload_tools) if event_run.preload_tools else None
                ),
                prefetch=event_run.prefetch if not event_run.prefetch.empty else None,
                observer=observer,
                event_type=event_run.event_type,
            )

            observer.on_activity("typing", None, False)
            get_display().flush_pending_run_summary()
    except asyncio.CancelledError:
        observer.on_activity("typing", None, False)
        if agent_instance:
            agent_instance.cancel_active_runs("event task cancelled")
        if reply_publisher and is_chat_event(event_run.event_type):
            reply_publisher.emit_llm_response_end(
                conversation_id=event_run.conversation_id,
                message_id=stream_message_id,
                message=None,
            )
        logger.info("Cancelled webhook event task: %s", event_run.event_type)
        raise
    except RunCancelled:
        observer.on_activity("typing", None, False)
        if reply_publisher and is_chat_event(event_run.event_type):
            reply_publisher.emit_llm_response_end(
                conversation_id=event_run.conversation_id,
                message_id=stream_message_id,
                message=None,
            )
        logger.info("Cancelled webhook event run: %s", event_run.event_type)
    except Exception:
        observer.on_activity("typing", None, False)
        if reply_publisher and is_chat_event(event_run.event_type):
            reply_publisher.emit_llm_response_end(
                conversation_id=event_run.conversation_id,
                message_id=stream_message_id,
                message=None,
            )
        logger.exception("Failed to process webhook event: %s", event_run.event_type)


async def _mark_event_notifications_read(event_run: EventRunContext) -> None:
    """Mark any correlated in-app notifications as read.

    Backend always emits ``notification_ids`` for notification-backed events
    since the unified ``notify()`` rollout, so there is no legacy fallback.
    """
    if not event_run.notification_ids:
        return
    try:
        ouro = agent_instance._get_ouro_client() if agent_instance else None
        if not ouro:
            return
        for notification_id in event_run.notification_ids:
            ouro.notifications.read(notification_id)
    except Exception as e:
        logger.warning("Failed to mark notification as read: %s", e)


def _is_self_event(event_run: EventRunContext) -> bool:
    if not agent_instance:
        return False
    own_id = agent_instance.own_user_id
    return bool(own_id and event_run.actor_user_id == own_id)


@app.get("/health")
async def health_check():
    scheduled_tasks = agent_instance.scheduler.list_tasks() if agent_instance else []
    return {
        "status": "ok",
        "uptime_seconds": (datetime.now(timezone.utc) - start_time).total_seconds(),
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
    except RunCancelled as e:
        raise HTTPException(status_code=499, detail=str(e) or "Run cancelled")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def handle_event(body: Dict[str, Any], background_tasks: BackgroundTasks):
    """Webhook receiver for Ouro platform events."""
    if not agent_instance:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        event_data = body.get("data", {}) or {}
        planning_cfg = agent_instance.config.planning
        agent_cfg = agent_instance.config.agent

        provenance = resolve_event_provenance(
            event_data=event_data,
            workspace=agent_cfg.workspace,
            planning_enabled=planning_cfg.enabled,
        )

        event_run = build_event_run_context(body, provenance=provenance)
    except Exception as exc:
        logger.warning("Invalid webhook payload: %s", body)
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}")

    if provenance and provenance.is_plan_feedback:
        logger.info(
            "Event provenance: plan_feedback=%s historical=%s team_id=%s",
            provenance.is_plan_feedback,
            provenance.is_historical_plan_feedback,
            provenance.team_id,
        )

    if _is_self_event(event_run):
        logger.info(
            "Skipping self-triggered %s event before pooling (actor=%s)",
            event_run.event_type,
            event_run.actor_user_id,
        )
        return {
            "status": "accepted",
            "event_type": event_run.event_type,
            "skipped": True,
        }

    if event_pool and event_pool.is_poolable(event_run):
        if event_run.notification_ids:
            background_tasks.add_task(_mark_event_notifications_read, event_run)
        await event_pool.submit(event_run)
        return {
            "status": "accepted",
            "event_type": event_run.event_type,
            "pooled": True,
        }

    background_tasks.add_task(_run_event_task, event_run)

    return {"status": "accepted", "event_type": event_run.event_type, "pooled": False}


def start_server(config_path: str = "config.json"):
    os.environ["CONFIG_FILE"] = config_path
    config = OuroAgentsConfig.load_from_file(config_path)
    reload = os.getenv("PYTHON_ENV") != "production"
    reload_excludes = (
        [
            "workspace/*",
            "../workspace/*",
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
