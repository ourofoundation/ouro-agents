from functools import cached_property
from typing import Optional

from ouro import Ouro


class OuroReplyPublisher:
    """Emit real-time activity and streaming events to Ouro over the websocket."""

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

    def realtime_session(self):
        """Return a context manager that keeps the websocket connected."""
        return self.client.websocket.session()

    def emit_activity(
        self,
        *,
        recipient_id: Optional[str],
        conversation_id: Optional[str],
        status: str,
        active: bool,
        message: Optional[str] = None,
    ) -> None:
        if not recipient_id or not conversation_id:
            return
        self.client.websocket.emit_activity(
            recipient_id=recipient_id,
            conversation_id=conversation_id,
            status=status,
            active=active,
            message=message,
        )

    def emit_llm_response(
        self,
        *,
        recipient_id: Optional[str],
        conversation_id: Optional[str],
        content: str,
        message_id: str,
    ) -> None:
        if not recipient_id or not conversation_id or not content:
            return
        self.client.websocket.emit_llm_response(
            recipient_id=recipient_id,
            conversation_id=conversation_id,
            content=content,
            message_id=message_id,
        )

    def emit_llm_response_end(
        self,
        *,
        recipient_id: Optional[str],
        conversation_id: Optional[str],
        message_id: str,
        message: Optional[dict] = None,
    ) -> None:
        if not recipient_id or not conversation_id:
            return
        self.client.websocket.emit_llm_response_end(
            recipient_id=recipient_id,
            conversation_id=conversation_id,
            message_id=message_id,
            message=message,
        )
