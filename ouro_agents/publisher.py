import logging
from contextlib import contextmanager
from functools import cached_property
from typing import Iterator, Optional

from ouro import Ouro

log = logging.getLogger(__name__)


class OuroReplyPublisher:
    """Emit real-time activity and streaming events to Ouro over the websocket.

    Websocket connections are opened lazily per-event via ``realtime_session``
    and torn down when the context exits.  All emit helpers swallow connection
    errors so a flaky socket never crashes the event handler.
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

    @contextmanager
    def realtime_session(self) -> Iterator[None]:
        """Open a websocket for the duration of a block, refreshing the token first."""
        self.client.ensure_valid_token()
        try:
            with self.client.websocket.session():
                yield
        except Exception:
            log.warning("Websocket session failed — falling back to non-realtime", exc_info=True)
            yield

    def _safe_emit(self, fn, **kwargs) -> None:
        """Call an emit function, swallowing websocket errors."""
        try:
            fn(**kwargs)
        except Exception:
            log.warning("Websocket emit failed (%s), skipping", fn.__name__, exc_info=True)

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
    ) -> None:
        if not conversation_id or not content:
            return
        self._safe_emit(
            self.client.websocket.emit_llm_response,
            conversation_id=conversation_id,
            content=content,
            message_id=message_id,
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
    ) -> None:
        if not conversation_id or not content:
            return
        self._safe_emit(
            self.client.websocket.emit_reasoning,
            conversation_id=conversation_id,
            content=content,
            message_id=message_id,
        )

    def emit_tool_start(
        self,
        *,
        conversation_id: Optional[str],
        message_id: str,
        tool_name: str,
        tool_call_id: str,
        input_data: Optional[dict] = None,
    ) -> None:
        if not conversation_id:
            return
        self._safe_emit(
            self.client.websocket.emit_tool_start,
            conversation_id=conversation_id,
            message_id=message_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            input_data=input_data,
        )

    def emit_tool_result(
        self,
        *,
        conversation_id: Optional[str],
        message_id: str,
        tool_call_id: str,
        output_data: Optional[dict] = None,
    ) -> None:
        if not conversation_id:
            return
        self._safe_emit(
            self.client.websocket.emit_tool_result,
            conversation_id=conversation_id,
            message_id=message_id,
            tool_call_id=tool_call_id,
            output_data=output_data,
        )
