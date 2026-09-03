from unittest.mock import MagicMock

from ouro_agents.config import RunMode
from ouro_agents.events import EventRunContext
from ouro_agents.server import ServerAgentObserver, _is_trivial_final_result


def _observer() -> tuple[ServerAgentObserver, MagicMock]:
    publisher = MagicMock()
    event_run = EventRunContext(
        event_type="new-message",
        task="hello",
        conversation_id="conv-1",
        user_id="user-1",
        team_id=None,
        actor_user_id="agent-1",
        actor_username="hermes",
        actor_is_agent=True,
        mode=RunMode.CHAT,
        asset_id=None,
        asset_type=None,
        root_asset_id=None,
        root_asset_type=None,
        preload_tools=[],
        prefetch=None,
        provenance=None,
        trigger_turn_id=None,
    )
    observer = ServerAgentObserver(
        event_run,
        stream_message_id="stream-1",
        turn_id="turn-1",
        reply_publisher=publisher,
    )
    return observer, publisher


def test_is_trivial_final_result():
    assert _is_trivial_final_result("")
    assert _is_trivial_final_result("NO_ACTION")
    assert _is_trivial_final_result(
        "MODEL_EMPTY_RESPONSE: model returned no content and no tool calls."
    )
    assert not _is_trivial_final_result("Here is the actual answer.")


def test_agent_no_action_variants_are_trivial_only_for_agent_messages():
    variants = [
        "NO ACTION",
        "No action needed.",
        "No action required",
        "No response needed",
        "Nothing to add.",
        "无需操作",
    ]

    for text in variants:
        assert _is_trivial_final_result(text, actor_is_agent=True)
        assert not _is_trivial_final_result(text, actor_is_agent=False)


def test_last_step_content_is_turn_final(monkeypatch):
    observer, publisher = _observer()
    created = {}

    def fake_create(_conversation_id, **kwargs):
        created.update(kwargs)
        return {"id": kwargs["id"], **kwargs}

    publisher.client = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.server.Messages",
        lambda _client: MagicMock(create=fake_create, update=MagicMock()),
    )
    monkeypatch.setattr(
        "ouro_agents.server.content_from_markdown",
        lambda _ouro, text: MagicMock(text=text, json={"type": "doc", "content": []}),
    )

    observer.on_intermediate_chunk("answer-1", "Here's the reply.")
    observer.on_intermediate_end("answer-1", "Here's the reply.", turn_final=True)
    observer.on_result_ready("Here's the reply.")

    assert created["id"] == "answer-1"
    assert created["metadata"] == {"turn_final": True}
    publisher.emit_llm_response_end.assert_called_once()
    assert publisher.emit_llm_response_end.call_args.kwargs["message_id"] == "answer-1"
    publisher.emit_llm_response.assert_called_once()


def test_streamed_no_action_is_not_persisted(monkeypatch):
    observer, publisher = _observer()
    create = MagicMock()

    publisher.client = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.server.Messages",
        lambda _client: MagicMock(create=create, update=MagicMock()),
    )

    observer.on_intermediate_chunk("answer-1", "NO_ACTION")
    observer.on_intermediate_end("answer-1", "NO_ACTION", turn_final=True)
    observer.on_result_ready("NO_ACTION")

    create.assert_not_called()
    publisher.emit_llm_response_end.assert_called_once_with(
        conversation_id="conv-1",
        message_id="answer-1",
        message=None,
    )


def test_streamed_translated_no_action_is_not_persisted(monkeypatch):
    observer, publisher = _observer()
    create = MagicMock()

    publisher.client = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.server.Messages",
        lambda _client: MagicMock(create=create, update=MagicMock()),
    )

    observer.on_intermediate_chunk("answer-1", "无需操作")
    observer.on_intermediate_end("answer-1", "无需操作", turn_final=True)
    observer.on_result_ready("无需操作")

    create.assert_not_called()
    publisher.emit_llm_response_end.assert_called_once_with(
        conversation_id="conv-1",
        message_id="answer-1",
        message=None,
    )


def test_no_action_tool_drops_accidental_streamed_content():
    observer, publisher = _observer()

    observer.on_intermediate_chunk("answer-1", "I have nothing to add.")
    observer.on_intermediate_drop("answer-1")
    observer.on_result_ready("NO_ACTION")

    publisher.client.assert_not_called()
    publisher.emit_llm_response_end.assert_called_once_with(
        conversation_id="conv-1",
        message_id="answer-1",
        message=None,
    )


def test_result_ready_fallback_when_nothing_streamed(monkeypatch):
    observer, publisher = _observer()
    created = {}

    def fake_create(_conversation_id, **kwargs):
        created.update(kwargs)
        return {"id": kwargs["id"], **kwargs}

    publisher.client = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.server.Messages",
        lambda _client: MagicMock(create=fake_create, update=MagicMock()),
    )
    monkeypatch.setattr(
        "ouro_agents.server.content_from_markdown",
        lambda _ouro, text: MagicMock(text=text, json={"type": "doc", "content": []}),
    )

    observer.on_intermediate_end(
        "commentary-1", "Short note.", turn_final=False
    )
    observer.on_result_ready("The finished reply.")

    assert created["id"] == "stream-1"
    assert created["metadata"] == {"turn_final": True}
    assert (
        publisher.emit_llm_response_end.call_args.kwargs["message_id"] == "stream-1"
    )
