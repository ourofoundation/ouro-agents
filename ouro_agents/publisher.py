from functools import cached_property
from typing import Literal, Optional

from ouro import Ouro
from ouro.resources.conversations import Messages

ReplyTargetType = Literal["comment", "conversation"]


class OuroReplyPublisher:
    """Publish agent replies back to Ouro.

    Wraps an ``Ouro`` client with convenience helpers for emitting
    real-time activity / streaming events and persisting final replies.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self._api_key = api_key
        self._base_url = base_url

    @cached_property
    def client(self) -> Ouro:
        return Ouro(
            api_key=self._api_key,
            base_url=self._base_url,
        )

    def describe_config(self) -> dict:
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

    def publish(
        self,
        reply_target_type: Optional[ReplyTargetType],
        reply_target_id: Optional[str],
        reply_text: str,
        message_id: Optional[str] = None,
    ):
        reply = reply_text.strip()
        if not reply_target_type or not reply_target_id or not reply:
            return None

        if reply_target_type == "conversation":
            content = self.client.posts.Content()
            content.from_markdown(reply)
            return Messages(self.client).create(
                conversation_id=reply_target_id,
                id=message_id,
                text=content.text,
                json=content.json,
            )

        if reply_target_type == "comment":
            content = self.client.comments.Content()
            content.from_markdown(reply)
            return self.client.comments.create(
                content=content,
                parent_id=reply_target_id,
            )

        raise ValueError(f"Unsupported reply target type: {reply_target_type}")
