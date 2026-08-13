from unittest.mock import MagicMock

from ouro_agents.publisher import (
    COALESCE_MAX_CHARS,
    OuroReplyPublisher,
)


def _publisher() -> tuple[OuroReplyPublisher, MagicMock]:
    client = MagicMock()
    publisher = OuroReplyPublisher(client=client)
    return publisher, client.websocket.emit_llm_response


def test_first_chunk_emits_immediately():
    publisher, emit = _publisher()
    publisher.emit_llm_response(
        conversation_id="conv-1",
        content="Hi",
        message_id="msg-1",
        turn_id="turn-1",
        seq=0,
    )
    emit.assert_called_once()
    assert emit.call_args.kwargs["content"] == "Hi"


def test_later_chunks_coalesce_until_char_threshold():
    publisher, emit = _publisher()
    publisher.emit_llm_response(
        conversation_id="conv-1",
        content="Hi",
        message_id="msg-1",
        turn_id="turn-1",
        seq=0,
    )
    emit.reset_mock()
    publisher.emit_llm_response(
        conversation_id="conv-1",
        content="a" * (COALESCE_MAX_CHARS - 1),
        message_id="msg-1",
        turn_id="turn-1",
        seq=0,
    )
    emit.assert_not_called()
    publisher.emit_llm_response(
        conversation_id="conv-1",
        content="!",
        message_id="msg-1",
        turn_id="turn-1",
        seq=0,
    )
    emit.assert_called_once()
    assert emit.call_args.kwargs["content"] == ("a" * (COALESCE_MAX_CHARS - 1) + "!")


def test_activity_flushes_pending_coalesce():
    publisher, emit = _publisher()
    publisher.emit_llm_response(
        conversation_id="conv-1",
        content="Hi",
        message_id="msg-1",
    )
    emit.reset_mock()
    publisher.emit_llm_response(
        conversation_id="conv-1",
        content=" more",
        message_id="msg-1",
    )
    emit.assert_not_called()
    publisher.emit_activity(
        conversation_id="conv-1",
        status="thinking",
        active=True,
    )
    emit.assert_called_once()
    assert emit.call_args.kwargs["content"] == " more"
