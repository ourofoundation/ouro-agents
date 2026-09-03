from unittest.mock import MagicMock

from ouro_agents.cli.observer import TUIObserver


def test_cli_observer_does_not_persist_terminal_no_action(monkeypatch):
    create = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.cli.observer.Messages",
        lambda _client: MagicMock(create=create),
    )
    events = []
    observer = TUIObserver(
        emit=events.append,
        agent_client=MagicMock(),
        conversation_id="conv-1",
        stream_message_id="stream-1",
    )

    observer.on_intermediate_chunk("silent-1", "Reasoning complete.\nNO_ACTION")
    observer.on_intermediate_end(
        "silent-1",
        "Reasoning complete.\nNO_ACTION",
        turn_final=True,
    )
    observer.on_result_ready("NO_ACTION")

    create.assert_not_called()
    assert events[-1].kind == "result"
    assert events[-1].payload is None


def test_cli_observer_drops_content_beside_no_action_tool(monkeypatch):
    create = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.cli.observer.Messages",
        lambda _client: MagicMock(create=create),
    )
    events = []
    observer = TUIObserver(
        emit=events.append,
        agent_client=MagicMock(),
        conversation_id="conv-1",
        stream_message_id="stream-1",
    )

    observer.on_intermediate_chunk("silent-1", "I will stay quiet.")
    observer.on_intermediate_drop("silent-1")
    observer.on_result_ready("NO_ACTION")

    create.assert_not_called()
    assert events[-2].kind == "intermediate_end"
    assert events[-2].text == ""
    assert events[-1].kind == "result"
    assert events[-1].payload is None
