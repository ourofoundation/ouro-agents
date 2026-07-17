import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from ouro.resources.conversations import Messages
from ouro_mcp.utils import content_from_markdown
from pydantic import BaseModel

from .agent import OuroAgent
from .cancellation import RunCancellationToken, RunCancelled
from .cli_progress import TerminalRunProgress
from .config import OuroAgentsConfig, RunMode
from .display import OuroDisplay, get_display, set_display
from .event_pool import EventPool
from .event_registry import is_chat_event
from .events import EventRunContext, build_event_run_context
from .logging_config import uvicorn_log_config
from .observer import AgentObserver, CompositeAgentObserver, ProgressEvent
from .provenance import resolve_event_provenance
from .publisher import OuroReplyPublisher
from .rate_limit import RATE_LIMIT_FAIL_MESSAGE, RATE_LIMIT_NOTE, is_rate_limit_error
from .security.policy import (
    ActorRole,
    EventSurface,
    actor_role_for,
    resolve_envelope,
)
from .utils.conversation import INTERRUPTED_REPLY_PREFIX
from .utils.message_persistence import (
    extract_tool_call_payloads,
    should_persist_tool_call_payload,
)
from .uuid_v7 import uuid7_str

if TYPE_CHECKING:
    from ouro.client import Ouro

logger = logging.getLogger(__name__)

_EMPTY_FINAL_ANSWER_MARKERS = frozenset(
    {
        "NO_ACTION",
        "MODEL_EMPTY_RESPONSE: model returned no content and no tool calls.",
    }
)
# Commentary shorter than this is unlikely to be the real user-facing answer.
_MIN_PROMOTABLE_INTERMEDIATE_CHARS = 40


def _is_trivial_final_result(result_text: str) -> bool:
    text = (result_text or "").strip()
    return not text or text in _EMPTY_FINAL_ANSWER_MARKERS


# Global state (still module-scoped for now; see P2 "server globals + session map"
# in SDK_IMPROVEMENTS.md for the planned move to request-scoped state).
agent_instance: Optional[OuroAgent] = None
reply_publisher: Optional[OuroReplyPublisher] = None
event_pool: Optional[EventPool] = None
last_heartbeat: Optional[datetime] = None
start_time: datetime = datetime.now(timezone.utc)
session_threads: Dict[str, str] = {}
# Active chat run tokens by conversation id, so an `interrupt` event can
# cancel just the run for that conversation.
active_chat_tokens: Dict[str, RunCancellationToken] = {}


class RunRequest(BaseModel):
    task: str
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    mode: Optional[str] = None
    user_id: Optional[str] = None
    run_secret: Optional[str] = None


# Legacy mode aliases accepted on the HTTP API. ``chat-reply``/``reply`` were
# merged into the single ``chat`` mode.
_REQUEST_MODE_ALIASES = {"chat-reply": "chat", "reply": "chat", "run": "autonomous"}


def _resolve_request_mode(mode: Optional[str]) -> RunMode:
    if not mode:
        return RunMode.AUTONOMOUS
    normalized = _REQUEST_MODE_ALIASES.get(mode.strip().lower(), mode)
    return RunMode(normalized)


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


def _event_envelope(event_run: EventRunContext):
    assert agent_instance is not None
    surface = (
        EventSurface.PLAN_REVIEW
        if event_run.provenance and event_run.provenance.is_quest_feedback
        else event_run.surface
    )
    role = actor_role_for(
        actor_user_id=event_run.actor_user_id,
        actor_is_agent=event_run.actor_is_agent,
        controller_user_ids=agent_instance.config.security.resolved_controller_ids,
        trusted_user_ids=agent_instance.config.security.resolved_trusted_ids,
    )
    envelope = resolve_envelope(
        role,
        surface,
        surface_capabilities=event_run.surface_capabilities
        if surface == event_run.surface
        else None,
    )
    logger.info(
        "Capability envelope for %s: role=%s surface=%s capabilities=%s",
        event_run.event_type,
        envelope.role.value,
        envelope.surface.value,
        ",".join(sorted(cap.value for cap in envelope.allowed_capabilities)),
    )
    return envelope


def _run_request_authenticated(request: Request, payload: RunRequest) -> bool:
    assert agent_instance is not None
    configured = agent_instance.config.security.run_secret
    supplied = payload.run_secret or request.headers.get("x-ouro-run-secret")
    if configured:
        return bool(supplied and supplied == configured)
    client_host = request.client.host if request.client else ""
    return client_host in {"127.0.0.1", "::1", "localhost"}


def _run_request_envelope(request: Request, payload: RunRequest):
    assert agent_instance is not None
    authenticated = _run_request_authenticated(request, payload)
    if authenticated and not payload.user_id:
        role = ActorRole.CONTROLLER
    elif authenticated:
        role = actor_role_for(
            actor_user_id=payload.user_id,
            actor_is_agent=False,
            controller_user_ids=agent_instance.config.security.resolved_controller_ids,
            trusted_user_ids=agent_instance.config.security.resolved_trusted_ids,
        )
    else:
        role = ActorRole.PUBLIC
    envelope = resolve_envelope(role, EventSurface.API_RUN)
    logger.info(
        "Capability envelope for /run: authenticated=%s role=%s capabilities=%s",
        authenticated,
        envelope.role.value,
        ",".join(sorted(cap.value for cap in envelope.allowed_capabilities)),
    )
    return envelope


class ServerAgentObserver(AgentObserver):
    def __init__(
        self,
        event_run: EventRunContext,
        stream_message_id: str,
        turn_id: str,
        reply_publisher: Optional[OuroReplyPublisher],
    ):
        self.event_run = event_run
        self.stream_message_id = stream_message_id
        self.turn_id = turn_id
        self._next_seq = 0
        self._message_seq: dict[str, int] = {}
        self.reply_publisher = reply_publisher
        self.active_activity_status: Optional[str] = None
        self.persisted_message_ref = []
        self._streamed_final_text = ""

        # Id of a final-step commentary stream flagged for drop. Held until
        # on_result_ready instead of being discarded immediately. When the model
        # streams its final answer as plain content (no final_answer tool
        # stream), that "commentary" IS the answer; promoting it (persisting the
        # answer under this same id) lets the client replace the streamed message
        # in place. Discarding it and re-persisting under stream_message_id would
        # make the client tear down the streamed message and mount a new one
        # under a different id, which flashes.
        self._deferred_drop_id: Optional[str] = None

        # Per-step flag: was this step's reasoning streamed live? Reset at each
        # step boundary (see on_step_persist) so every step decides afresh.
        self._has_streamed_reasoning = False
        self._reasoning_message_id: Optional[str] = None
        self._reasoning_started_at: Optional[float] = None

        # NOTE: reasoning/tool persistence is handled inline so the persisted
        # row shares the same id as its realtime stream event (enabling robust
        # id-based dedup on the client).

        # Active subagent runs keyed by run_id (see on_progress).
        self._subagent_runs: dict[str, dict[str, object]] = {}

        # Intermediate commentary rows persisted with ``turn_final: False``.
        # If the model ends on an empty ``final_answer`` after dumping its reply
        # into commentary, we promote the last substantial row in-place.
        self._intermediate_messages: list[dict[str, Any]] = []

    def _next_turn_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def _seq_for_message(self, message_id: str) -> int:
        if message_id not in self._message_seq:
            self._message_seq[message_id] = self._next_turn_seq()
        return self._message_seq[message_id]

    def on_activity(self, status: str, message: Optional[str], active: bool) -> None:
        if not self.reply_publisher or not is_chat_event(self.event_run.event_type):
            return
        if not self.event_run.conversation_id:
            return
        if active:
            self.active_activity_status = status
        elif self.active_activity_status == status:
            self.active_activity_status = None
        self.reply_publisher.emit_activity(
            conversation_id=self.event_run.conversation_id,
            status=status,
            active=active,
            message=message,
        )

    def _clear_activity(self) -> None:
        if self.active_activity_status:
            self.on_activity(self.active_activity_status, None, False)

    def on_stream_chunk(self, chunk: str) -> None:
        if not self.reply_publisher or not is_chat_event(self.event_run.event_type):
            return
        if not self.event_run.conversation_id:
            return
        self._clear_activity()
        self._streamed_final_text += chunk

        self.reply_publisher.emit_llm_response(
            conversation_id=self.event_run.conversation_id,
            content=chunk,
            message_id=self.stream_message_id,
            turn_id=self.turn_id,
            seq=self._seq_for_message(self.stream_message_id),
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
        self._clear_activity()
        self.reply_publisher.emit_llm_response(
            conversation_id=self.event_run.conversation_id,
            content=chunk,
            message_id=message_id,
            turn_id=self.turn_id,
            seq=self._seq_for_message(message_id),
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
                turn_id=self.turn_id,
                seq=self._seq_for_message(message_id),
                type="message",
                text=content.text,
                json=content.json,
                metadata={"turn_final": False},
            )
            self._intermediate_messages.append(
                {"id": message_id, "text": text, "msg": msg}
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

    def _emit_discard(self, message_id: str) -> None:
        if not self.reply_publisher or not self.event_run.conversation_id:
            return
        self.reply_publisher.emit_llm_response_end(
            conversation_id=self.event_run.conversation_id,
            message_id=message_id,
            message={
                "id": message_id,
                "user_id": str(self.reply_publisher.client.user.id),
                "discarded": True,
            },
        )

    def on_intermediate_drop(self, message_id: str) -> None:
        if (
            not self.reply_publisher
            or not self.event_run.conversation_id
            or not is_chat_event(self.event_run.event_type)
        ):
            return
        # Defer the discard until on_result_ready, which decides whether to
        # promote this streamed message to the final answer (persist in place)
        # or genuinely discard it (when a separate final answer was streamed).
        self._deferred_drop_id = message_id

    def discard_pending_intermediate(self) -> None:
        """Flush a deferred drop without promotion (used on error/cancel)."""
        if self._deferred_drop_id is not None:
            self._emit_discard(self._deferred_drop_id)
            self._deferred_drop_id = None

    def _last_substantial_intermediate(self) -> Optional[dict[str, Any]]:
        for entry in reversed(self._intermediate_messages):
            text = str(entry.get("text", "")).strip()
            # Rate-limit status notes are not answers — never promote them.
            if text == RATE_LIMIT_NOTE:
                continue
            if len(text) >= _MIN_PROMOTABLE_INTERMEDIATE_CHARS:
                return entry
        return None

    def _promote_intermediate_to_final(self, entry: dict[str, Any]) -> Optional[dict]:
        if not self.event_run.conversation_id or not self.reply_publisher:
            return None
        message_id = str(entry["id"])
        existing = entry.get("msg")
        if not isinstance(existing, dict):
            return None
        try:
            ouro = self.reply_publisher.client
            updated_metadata = {**(existing.get("metadata") or {}), "turn_final": True}
            updated = Messages(ouro).update(
                self.event_run.conversation_id,
                message_id,
                metadata=updated_metadata,
            )
            promoted = updated if isinstance(updated, dict) else {
                **existing,
                "metadata": updated_metadata,
            }
            logger.info(
                "Promoted intermediate message %s to turn_final after trivial final_answer",
                message_id,
            )
            return promoted
        except Exception:
            logger.warning(
                "Failed to promote intermediate message %s to turn_final",
                message_id,
                exc_info=True,
            )
            return None

    def on_result_ready(self, result_text: str) -> None:
        msg = None
        result_text = (result_text or "").strip()
        if not result_text and self._streamed_final_text.strip():
            result_text = self._streamed_final_text.strip()

        has_real_result = bool(result_text) and not _is_trivial_final_result(result_text)

        # Decide what id the final message is persisted/emitted under. When the
        # model streamed its answer as plain content (no on_stream_chunk), the
        # commentary message flagged for drop IS the answer: promote it by
        # persisting under that same id so the client replaces the streamed
        # message in place (no flash). Otherwise, a real final answer was
        # streamed separately — discard the redundant commentary now.
        final_message_id = self.stream_message_id
        if self._deferred_drop_id is not None:
            if has_real_result and not self._streamed_final_text.strip():
                final_message_id = self._deferred_drop_id
            else:
                self._emit_discard(self._deferred_drop_id)
            self._deferred_drop_id = None

        if self.event_run.conversation_id and self.reply_publisher and has_real_result:
            ouro = self.reply_publisher.client
            content = content_from_markdown(ouro, result_text)
            msg = Messages(ouro).create(
                self.event_run.conversation_id,
                id=final_message_id,
                turn_id=self.turn_id,
                seq=self._seq_for_message(final_message_id),
                type="message",
                text=content.text,
                json=content.json,
                metadata={"turn_final": True},
            )
            self.persisted_message_ref.append(msg)
        elif not has_real_result:
            intermediate = self._last_substantial_intermediate()
            if intermediate is not None:
                promoted = self._promote_intermediate_to_final(intermediate)
                if promoted is not None:
                    msg = promoted
                    final_message_id = str(intermediate["id"])
                    self.persisted_message_ref.append(promoted)

        if self.reply_publisher and is_chat_event(self.event_run.event_type):
            if (
                self.stream_message_id != final_message_id
                and self._streamed_final_text.strip()
            ):
                self.reply_publisher.emit_llm_response_end(
                    conversation_id=self.event_run.conversation_id,
                    message_id=self.stream_message_id,
                    message=None,
                )
            self.reply_publisher.emit_llm_response_end(
                conversation_id=self.event_run.conversation_id,
                message_id=final_message_id,
                message=msg,
            )

    def on_step_persist(self, step: dict) -> None:
        # Step boundary: clear the per-step reasoning-stream flag so the next
        # step's on_reasoning_persist re-evaluates whether to emit live.
        self._has_streamed_reasoning = False
        self._reasoning_message_id = None
        self._reasoning_started_at = None

        if not self.reply_publisher or not self.event_run.conversation_id:
            return

        is_chat = is_chat_event(self.event_run.event_type)
        if is_chat:
            self._clear_activity()

        ouro = self.reply_publisher.client
        for payload in extract_tool_call_payloads(step):
            if not should_persist_tool_call_payload(payload):
                continue

            # A single id shared by the realtime tool events and the persisted
            # row. This lets the client dedupe the streamed tool part against the
            # DB row by id instead of fragile positional counting.
            tool_call_id = uuid7_str()
            tool_seq = self._seq_for_message(tool_call_id)
            if is_chat:
                self.reply_publisher.emit_tool_start(
                    conversation_id=self.event_run.conversation_id,
                    message_id=self.stream_message_id,
                    tool_name=payload["name"],
                    tool_call_id=tool_call_id,
                    input_data=payload["arguments"],
                    turn_id=self.turn_id,
                    seq=tool_seq,
                )
                self.reply_publisher.emit_tool_result(
                    conversation_id=self.event_run.conversation_id,
                    message_id=self.stream_message_id,
                    tool_call_id=tool_call_id,
                    output_data=payload["result"],
                    turn_id=self.turn_id,
                    seq=tool_seq,
                )
            try:
                Messages(ouro).create(
                    self.event_run.conversation_id,
                    id=tool_call_id,
                    turn_id=self.turn_id,
                    seq=tool_seq,
                    type="tool_call",
                    text=f"Called {payload['name']}",
                    json={
                        "name": payload["name"],
                        "arguments": payload["arguments"],
                        "result": payload["result"],
                    },
                )
            except Exception:
                logger.warning("Failed to persist tool_call message", exc_info=True)

    def on_reasoning_persist(self, content: str) -> None:
        text = (content or "").strip()
        if not text or not self.reply_publisher or not self.event_run.conversation_id:
            return

        if self._reasoning_message_id is None:
            self._reasoning_message_id = uuid7_str()
        reasoning_seq = self._seq_for_message(self._reasoning_message_id)

        duration_s: Optional[float] = None
        if self._reasoning_started_at is not None:
            duration_s = round(
                max(0.0, time.monotonic() - self._reasoning_started_at), 1
            )

        if (
            is_chat_event(self.event_run.event_type)
            and not self._has_streamed_reasoning
        ):
            self.reply_publisher.emit_reasoning(
                conversation_id=self.event_run.conversation_id,
                content=text,
                message_id=self._reasoning_message_id,
                turn_id=self.turn_id,
                seq=reasoning_seq,
            )

        try:
            Messages(self.reply_publisher.client).create(
                self.event_run.conversation_id,
                id=self._reasoning_message_id,
                turn_id=self.turn_id,
                seq=reasoning_seq,
                type="reasoning",
                text=text,
                json=(
                    {"text": text, "duration_s": duration_s}
                    if duration_s is not None
                    else {"text": text}
                ),
            )
        except Exception:
            logger.warning("Failed to persist reasoning message", exc_info=True)

    def on_reasoning_stream(self, content: str) -> None:
        if (
            not self.reply_publisher
            or not is_chat_event(self.event_run.event_type)
            or not self.event_run.conversation_id
        ):
            return
        if self._reasoning_message_id is None:
            self._reasoning_message_id = uuid7_str()
        if self._reasoning_started_at is None:
            self._reasoning_started_at = time.monotonic()
        reasoning_seq = self._seq_for_message(self._reasoning_message_id)
        self._has_streamed_reasoning = True
        self._clear_activity()
        self.reply_publisher.emit_reasoning(
            conversation_id=self.event_run.conversation_id,
            content=content,
            message_id=self._reasoning_message_id,
            turn_id=self.turn_id,
            seq=reasoning_seq,
        )

    def on_progress(self, event: ProgressEvent) -> None:
        if not event.phase.startswith("subagent_"):
            return
        if not self.reply_publisher or not self.event_run.conversation_id:
            return
        if not is_chat_event(self.event_run.event_type):
            return

        if event.phase == "subagent_started":
            self._handle_subagent_started(event)
        elif event.phase in {"subagent_completed", "subagent_failed"}:
            self._handle_subagent_finished(event)
        elif event.phase == "subagent_step":
            self._handle_subagent_step(event)

    def _subagent_name(self, event: ProgressEvent) -> str:
        name = event.detail.get("name") or event.detail.get("subagent")
        if name:
            return str(name)
        return (event.message or "subagent").strip()

    def _handle_subagent_started(self, event: ProgressEvent) -> None:
        run_id = str(event.detail.get("run_id") or uuid7_str())
        name = self._subagent_name(event)
        message_id = uuid7_str()
        self._subagent_runs[run_id] = {
            "message_id": message_id,
            "name": name,
            "started_at": time.monotonic(),
        }
        self._clear_activity()
        seq = self._seq_for_message(message_id)
        self.reply_publisher.emit_subagent_start(
            conversation_id=self.event_run.conversation_id,
            message_id=message_id,
            subagent_name=name,
            turn_id=self.turn_id,
            seq=seq,
        )

    def _handle_subagent_step(self, event: ProgressEvent) -> None:
        run_id = event.detail.get("run_id")
        if not run_id:
            return
        run = self._subagent_runs.get(str(run_id))
        if not run:
            return
        tool = event.detail.get("tool")
        name = str(run.get("name") or self._subagent_name(event))
        if tool:
            detail = str(tool)
        elif "retrying" in (event.message or ""):
            detail = "retrying"
        else:
            return
        message_id = str(run["message_id"])
        self.reply_publisher.emit_subagent_step(
            conversation_id=self.event_run.conversation_id,
            message_id=message_id,
            subagent_name=name,
            detail=detail,
            turn_id=self.turn_id,
            seq=self._seq_for_message(message_id),
        )

    def _handle_subagent_finished(self, event: ProgressEvent) -> None:
        run_id = event.detail.get("run_id")
        run = self._subagent_runs.pop(str(run_id), None) if run_id else None
        if not run:
            return

        name = str(run.get("name") or self._subagent_name(event))
        message_id = str(run["message_id"])
        usage = event.detail.get("usage") or {}
        wall_ms = usage.get("wall_time_ms")
        if isinstance(wall_ms, (int, float)) and wall_ms > 0:
            duration_s = round(wall_ms / 1000, 1)
        else:
            started_at = run.get("started_at")
            duration_s = (
                round(max(0.0, time.monotonic() - float(started_at)), 1)
                if isinstance(started_at, (int, float))
                else None
            )

        failed = event.phase == "subagent_failed"
        status = "failed" if failed else "completed"
        error = event.detail.get("error")
        steps = usage.get("steps")
        json_payload: dict[str, object] = {
            "name": name,
            "status": status,
        }
        if duration_s is not None:
            json_payload["duration_s"] = duration_s
        if isinstance(steps, int) and steps > 0:
            json_payload["steps"] = steps
        if failed and error:
            json_payload["error"] = str(error)

        if self._subagent_runs:
            active_names = [str(r.get("name", "subagent")) for r in self._subagent_runs.values()]
            self.on_activity(
                "thinking",
                f"Running {', '.join(active_names)} subagent(s)",
                True,
            )
        else:
            self._clear_activity()

        text = (
            f"{name} subagent failed"
            if failed
            else f"Ran {name} subagent"
        )
        msg = None
        try:
            msg = Messages(self.reply_publisher.client).create(
                self.event_run.conversation_id,
                id=message_id,
                turn_id=self.turn_id,
                seq=self._seq_for_message(message_id),
                type="subagent",
                text=text,
                json=json_payload,
            )
        except Exception:
            logger.warning("Failed to persist subagent message", exc_info=True)

        self.reply_publisher.emit_llm_response_end(
            conversation_id=self.event_run.conversation_id,
            message_id=message_id,
            message=msg,
        )


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
    capability_envelope = _event_envelope(event_run)

    # Route active/pending plan feedback to the dedicated review path
    prov = event_run.provenance
    if prov and prov.is_quest_feedback:
        try:
            await agent_instance.handle_quest_feedback(
                event_run,
                capability_envelope=capability_envelope,
            )
            get_display().flush_pending_run_summary()
        except RunCancelled:
            logger.info("Cancelled quest feedback event run")
        except Exception:
            logger.exception("Failed to handle quest feedback event")
        return

    if event_run.event_type == "new-conversation":
        # Conversation creation has no user message yet; we do not run the agent.
        return

    stream_message_id = uuid7_str()
    turn_id = uuid7_str()
    server_observer = ServerAgentObserver(
        event_run, stream_message_id, turn_id, reply_publisher
    )
    terminal_progress = TerminalRunProgress(
        event_run,
        get_display(),
        config=agent_instance.config.display.serve_progress,
    )
    observer = CompositeAgentObserver(server_observer, terminal_progress)

    cancellation_token = RunCancellationToken()
    conversation_id = event_run.conversation_id
    if conversation_id and is_chat_event(event_run.event_type):
        active_chat_tokens[conversation_id] = cancellation_token

    try:
        terminal_progress.start()
        with (
            reply_publisher.realtime_session()
            if reply_publisher and is_chat_event(event_run.event_type)
            else nullcontext()
        ):
            observer.on_activity("thinking", "Thinking about it...", True)

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
                capability_envelope=capability_envelope,
                cancellation_token=cancellation_token,
                trigger_turn_id=event_run.trigger_turn_id,
            )

            observer.on_activity("thinking", None, False)
            terminal_progress.finish(result)
            get_display().flush_pending_run_summary()
    except asyncio.CancelledError:
        observer.on_activity("thinking", None, False)
        server_observer.discard_pending_intermediate()
        terminal_progress.cancel("cancelled")
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
        observer.on_activity("thinking", None, False)
        server_observer.discard_pending_intermediate()
        terminal_progress.cancel("cancelled")
        _persist_interrupted_message(
            event_run,
            turn_id=turn_id,
            message_id=stream_message_id,
            partial_reply=server_observer._streamed_final_text,
            seq=server_observer._seq_for_message(stream_message_id),
        )
        if reply_publisher and is_chat_event(event_run.event_type):
            reply_publisher.emit_llm_response_end(
                conversation_id=event_run.conversation_id,
                message_id=stream_message_id,
                message=None,
            )
        logger.info("Cancelled webhook event run: %s", event_run.event_type)
    except Exception as exc:
        observer.on_activity("thinking", None, False)
        server_observer.discard_pending_intermediate()
        terminal_progress.fail("failed")
        if reply_publisher and is_chat_event(event_run.event_type):
            fail_msg = None
            if is_rate_limit_error(exc) and event_run.conversation_id:
                try:
                    fail_msg = _persist_rate_limit_fail_message(
                        event_run,
                        turn_id=turn_id,
                        message_id=stream_message_id,
                        seq=server_observer._seq_for_message(stream_message_id),
                    )
                except Exception:
                    logger.warning(
                        "Failed to persist rate-limit failure message",
                        exc_info=True,
                    )
            reply_publisher.emit_llm_response_end(
                conversation_id=event_run.conversation_id,
                message_id=stream_message_id,
                message=fail_msg,
            )
        logger.exception("Failed to process webhook event: %s", event_run.event_type)
    finally:
        if (
            conversation_id
            and active_chat_tokens.get(conversation_id) is cancellation_token
        ):
            del active_chat_tokens[conversation_id]


def _persist_interrupted_message(
    event_run: EventRunContext,
    *,
    turn_id: str,
    message_id: str,
    partial_reply: str,
    seq: int,
) -> None:
    """Persist an interrupted agent turn to the platform messages table."""
    if not reply_publisher or not event_run.conversation_id:
        return
    if not is_chat_event(event_run.event_type):
        return

    text = INTERRUPTED_REPLY_PREFIX
    partial_reply = (partial_reply or "").strip()
    if partial_reply:
        text += f"\n\nYour reply up to the cut-off:\n{partial_reply}"

    try:
        ouro = reply_publisher.client
        content = content_from_markdown(ouro, text)
        Messages(ouro).create(
            event_run.conversation_id,
            id=message_id,
            turn_id=turn_id,
            seq=seq,
            type="message",
            text=content.text,
            json=content.json,
            metadata={"turn_final": True, "interrupted": True},
        )
    except Exception:
        logger.warning("Failed to persist interrupted message", exc_info=True)


def _persist_rate_limit_fail_message(
    event_run: EventRunContext,
    *,
    turn_id: str,
    message_id: str,
    seq: int,
) -> Optional[dict]:
    """Persist a user-visible explanation when a chat turn dies on 429."""
    if not reply_publisher or not event_run.conversation_id:
        return None
    ouro = reply_publisher.client
    content = content_from_markdown(ouro, RATE_LIMIT_FAIL_MESSAGE)
    return Messages(ouro).create(
        event_run.conversation_id,
        id=message_id,
        turn_id=turn_id,
        seq=seq,
        type="message",
        text=content.text,
        json=content.json,
        metadata={"turn_final": True, "rate_limited": True},
    )


async def _handle_interrupt_event(event_run: EventRunContext) -> None:
    """Cancel the in-flight chat run (or pending pooled events) for a conversation."""
    conversation_id = event_run.conversation_id
    if not conversation_id:
        logger.warning("Interrupt event without conversation_id; ignoring")
        return

    discarded_events: list[EventRunContext] = []
    if event_pool:
        discarded_events = await event_pool.discard(f"conversation:{conversation_id}")

    token = active_chat_tokens.get(conversation_id)
    if token:
        token.cancel("interrupted by user")

    logger.info(
        "Interrupt for conversation %s: cancelled_run=%s discarded_pooled=%d",
        conversation_id,
        bool(token),
        len(discarded_events),
    )


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
async def run_task(request: RunRequest, http_request: Request):
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

        mode = _resolve_request_mode(request.mode)
        capability_envelope = _run_request_envelope(http_request, request)
        result = await agent_instance.run(
            task=request.task,
            conversation_id=conversation_id,
            mode=mode,
            user_id=request.user_id,
            capability_envelope=capability_envelope,
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
        agent_cfg = agent_instance.config.agent

        provenance = resolve_event_provenance(
            event_data=event_data,
            workspace=agent_cfg.workspace,
        )

        event_run = build_event_run_context(body, provenance=provenance)
    except Exception as exc:
        logger.warning("Invalid webhook payload: %s", body)
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}")

    if provenance and provenance.is_quest_feedback:
        logger.info(
            "Event provenance: quest_feedback on %s team_id=%s",
            provenance.root_asset_id,
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

    # Control event: cancel work for the conversation instead of running.
    if event_run.event_type == "interrupt":
        await _handle_interrupt_event(event_run)
        return {"status": "accepted", "event_type": "interrupt"}

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


def dev_reload_settings(config: OuroAgentsConfig) -> tuple[list[str], list[str]]:
    """Reload dirs/excludes for dev: watch package source only, not agent workspace."""
    package_root = Path(__file__).resolve().parent
    workspace = config.agent.workspace.resolve()
    chroma = (config.memory.path / "chroma").resolve()
    reload_dirs = [str(package_root)]
    reload_excludes = [
        str(workspace),
        str(chroma),
        "__pycache__",
        "*.pyc",
    ]
    return reload_dirs, reload_excludes


def start_server(config_path: str = "config.json"):
    os.environ["CONFIG_FILE"] = config_path
    config = OuroAgentsConfig.load_from_file(config_path)
    reload = os.getenv("PYTHON_ENV") != "production"
    reload_dirs = None
    reload_excludes = None
    if reload:
        reload_dirs, reload_excludes = dev_reload_settings(config)
    uvicorn.run(
        "ouro_agents.server:app",
        host=config.server.host,
        port=config.server.port,
        reload=reload,
        reload_dirs=reload_dirs,
        reload_excludes=reload_excludes,
        log_config=uvicorn_log_config(),
    )
