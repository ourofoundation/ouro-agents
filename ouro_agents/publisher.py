import logging
import time
from contextlib import contextmanager
from functools import cached_property
from typing import Iterator, Optional

from ouro import Ouro

log = logging.getLogger(__name__)

# Providers emit deltas of a few characters each; forwarding every one as its
# own websocket event floods the socket for no visual benefit (the client
# smooths display locally). Coalesce until either threshold is hit.
COALESCE_MAX_CHARS = 24
COALESCE_MAX_INTERVAL_S = 0.05


class OuroReplyPublisher:
    """Emit real-time activity and streaming events to Ouro over the websocket.

    The websocket is kept open for the agent process lifetime via
    ``ensure_connected`` / ``connect_persistent``. All emit helpers swallow
    connection errors so a flaky socket never crashes the event handler.
    """

    def __init__(
        self,
        client: Optional[Ouro] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self._client = client
        self._api_key = api_key
        self._base_url = base_url
        # Pending coalesced stream chunk: {emit, kwargs, content, first_at}.
        # At most one stream (message_id + event kind) buffers at a time; a
        # chunk for a different stream flushes the previous one first.
        self._pending: Optional[dict] = None

    @cached_property
    def client(self) -> Ouro:
        if self._client is not None:
            return self._client
        return Ouro(
            api_key=self._api_key,
            base_url=self._base_url,
        )

    def describe_config(self) -> dict:
        if self._client is not None:
            return {
                "base_url": str(getattr(self._client, "base_url", "shared")),
                "shared_client": True,
            }
        return {
            "base_url": self._base_url,
            "api_key_present": bool(self._api_key),
        }

    def ensure_ready(self) -> None:
        _ = self.client

    def connect_persistent(self) -> None:
        """Open the websocket at process start and leave it connected."""
        self.ensure_ready()
        self.client.ensure_valid_token()
        self.client.websocket.ensure_connected()

    def disconnect_persistent(self) -> None:
        """Tear down the process-lifetime websocket on shutdown."""
        self._flush_pending()
        try:
            ws = self.client.websocket
        except Exception:
            return
        if ws.is_connected:
            ws.disconnect()

    @contextmanager
    def realtime_session(self) -> Iterator[None]:
        """Ensure the websocket is up for the duration of a block.

        Only falls back to a non-realtime body when *opening* the websocket
        fails. Exceptions raised by the wrapped body (e.g. model 429s) must
        propagate unchanged — catching them and yielding again triggers
        ``RuntimeError: generator didn't stop after throw()`` and hides the
        original error from the chat fail-path.
        """
        self.client.ensure_valid_token()
        try:
            self.client.websocket.ensure_connected()
        except Exception:
            log.warning(
                "Websocket session failed — falling back to non-realtime",
                exc_info=True,
            )
            yield
            return

        try:
            yield
        finally:
            self._flush_pending()

    def _safe_emit(self, fn, **kwargs) -> None:
        """Call an emit function, swallowing websocket errors."""
        try:
            fn(**kwargs)
        except Exception:
            log.warning("Websocket emit failed (%s), skipping", fn.__name__, exc_info=True)

    def _flush_pending(self) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        if not pending["content"]:
            return
        self._safe_emit(pending["emit"], content=pending["content"], **pending["kwargs"])

    def _emit_coalesced(self, emit_fn, *, content: str, **kwargs) -> None:
        """Buffer a stream delta, flushing once it grows big or old enough.

        The first chunk of a new stream is sent immediately so TTFT is not
        gated on the coalesce window. Later chunks still batch.
        """
        pending = self._pending
        if pending is not None and (
            pending["emit"] is not emit_fn or pending["kwargs"] != kwargs
        ):
            self._flush_pending()
            pending = None
        if pending is None:
            self._safe_emit(emit_fn, content=content, **kwargs)
            self._pending = {
                "emit": emit_fn,
                "kwargs": kwargs,
                "content": "",
                "first_at": time.monotonic(),
            }
            return
        pending["content"] += content
        if (
            len(pending["content"]) >= COALESCE_MAX_CHARS
            or time.monotonic() - pending["first_at"] >= COALESCE_MAX_INTERVAL_S
        ):
            self._flush_pending()

    def emit_activity(
        self,
        *,
        conversation_id: Optional[str],
        status: str,
        active: bool,
        message: Optional[str] = None,
    ) -> None:
        if not conversation_id:
            return
        self._flush_pending()
        self._safe_emit(
            self.client.websocket.emit_activity,
            conversation_id=conversation_id,
            status=status,
            active=active,
            message=message,
        )

    def emit_llm_response(
        self,
        *,
        conversation_id: Optional[str],
        content: str,
        message_id: str,
        turn_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        if not conversation_id or not content:
            return
        self._emit_coalesced(
            self.client.websocket.emit_llm_response,
            content=content,
            conversation_id=conversation_id,
            message_id=message_id,
            turn_id=turn_id,
            seq=seq,
        )

    def emit_llm_response_end(
        self,
        *,
        conversation_id: Optional[str],
        message_id: str,
        message: Optional[dict] = None,
    ) -> None:
        if not conversation_id:
            return
        self._flush_pending()
        self._safe_emit(
            self.client.websocket.emit_llm_response_end,
            conversation_id=conversation_id,
            message_id=message_id,
            message=message,
        )

    def emit_reasoning(
        self,
        *,
        conversation_id: Optional[str],
        content: str,
        message_id: str,
        turn_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        if not conversation_id or not content:
            return
        self._emit_coalesced(
            self.client.websocket.emit_reasoning,
            content=content,
            conversation_id=conversation_id,
            message_id=message_id,
            turn_id=turn_id,
            seq=seq,
        )

    def emit_subagent_start(
        self,
        *,
        conversation_id: Optional[str],
        message_id: str,
        subagent_name: str,
        turn_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        if not conversation_id or not subagent_name:
            return
        self._flush_pending()
        self._safe_emit(
            self.client.websocket.emit_subagent_start,
            conversation_id=conversation_id,
            message_id=message_id,
            subagent_name=subagent_name,
            turn_id=turn_id,
            seq=seq,
        )

    def emit_subagent_step(
        self,
        *,
        conversation_id: Optional[str],
        message_id: str,
        subagent_name: str,
        detail: str,
        turn_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        if not conversation_id or not detail:
            return
        self._flush_pending()
        self._safe_emit(
            self.client.websocket.emit_subagent_step,
            conversation_id=conversation_id,
            message_id=message_id,
            subagent_name=subagent_name,
            detail=detail,
            turn_id=turn_id,
            seq=seq,
        )

    def emit_tool_start(
        self,
        *,
        conversation_id: Optional[str],
        message_id: str,
        tool_name: str,
        tool_call_id: str,
        input_data: Optional[dict] = None,
        turn_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        if not conversation_id:
            return
        self._flush_pending()
        self._safe_emit(
            self.client.websocket.emit_tool_start,
            conversation_id=conversation_id,
            message_id=message_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            input_data=input_data,
            turn_id=turn_id,
            seq=seq,
        )

    def emit_tool_result(
        self,
        *,
        conversation_id: Optional[str],
        message_id: str,
        tool_call_id: str,
        output_data: Optional[dict] = None,
        turn_id: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> None:
        if not conversation_id:
            return
        self._flush_pending()
        self._safe_emit(
            self.client.websocket.emit_tool_result,
            conversation_id=conversation_id,
            message_id=message_id,
            tool_call_id=tool_call_id,
            output_data=output_data,
            turn_id=turn_id,
            seq=seq,
        )
