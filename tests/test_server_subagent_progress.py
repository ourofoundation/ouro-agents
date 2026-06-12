from types import SimpleNamespace
from unittest.mock import MagicMock

from ouro_agents.config import RunMode
from ouro_agents.events import EventRunContext
from ouro_agents.observer import ProgressEvent
from ouro_agents.server import ServerAgentObserver


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
        mode=RunMode.CHAT_REPLY,
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


def test_subagent_started_emits_realtime_start():
    observer, publisher = _observer()

    observer.on_progress(
        ProgressEvent(
            "subagent_started",
            "research",
            detail={"name": "research", "run_id": "run-1"},
        )
    )

    publisher.emit_subagent_start.assert_called_once()
    kwargs = publisher.emit_subagent_start.call_args.kwargs
    assert kwargs["conversation_id"] == "conv-1"
    assert kwargs["subagent_name"] == "research"
    assert kwargs["turn_id"] == "turn-1"
    assert "run-1" in observer._subagent_runs


def test_subagent_step_updates_existing_run():
    observer, publisher = _observer()
    observer._subagent_runs["run-1"] = {
        "message_id": "msg-1",
        "name": "research",
        "started_at": 0.0,
    }

    observer.on_progress(
        ProgressEvent(
            "subagent_step",
            "research: using tavily_search",
            detail={"name": "research", "run_id": "run-1", "tool": "tavily_search"},
        )
    )

    publisher.emit_subagent_step.assert_called_once()
    kwargs = publisher.emit_subagent_step.call_args.kwargs
    assert kwargs["message_id"] == "msg-1"
    assert kwargs["detail"] == "tavily_search"
    publisher.emit_activity.assert_not_called()


def test_subagent_completed_persists_duration_and_emits_end(monkeypatch):
    observer, publisher = _observer()
    created = {}

    def fake_create(_conversation_id, **kwargs):
        created.update(kwargs)
        return {"id": kwargs["id"], **kwargs}

    publisher.client = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.server.Messages",
        lambda _client: MagicMock(create=fake_create),
    )

    observer._subagent_runs["run-1"] = {
        "message_id": "msg-1",
        "name": "research",
        "started_at": 0.0,
    }

    observer.on_progress(
        ProgressEvent(
            "subagent_completed",
            "research",
            state="complete",
            detail={
                "name": "research",
                "run_id": "run-1",
                "usage": {"wall_time_ms": 4500, "steps": 3},
            },
        )
    )

    assert created["type"] == "subagent"
    assert created["json"]["name"] == "research"
    assert created["json"]["duration_s"] == 4.5
    assert created["json"]["steps"] == 3
    assert created["json"]["status"] == "completed"
    publisher.emit_llm_response_end.assert_called_once()
    assert "run-1" not in observer._subagent_runs


def test_delegate_tool_call_is_not_persisted_or_streamed(monkeypatch):
    observer, publisher = _observer()
    create = MagicMock()
    publisher.client = MagicMock()
    monkeypatch.setattr(
        "ouro_agents.server.Messages",
        lambda _client: MagicMock(create=create),
    )

    observer.on_step_persist(
        SimpleNamespace(
            is_final_answer=False,
            error=None,
            observations="subagent result",
            tool_calls=[
                {
                    "name": "delegate",
                    "arguments": {"tasks": [{"subagent": "research"}]},
                }
            ],
        )
    )

    publisher.emit_tool_start.assert_not_called()
    publisher.emit_tool_result.assert_not_called()
    create.assert_not_called()
