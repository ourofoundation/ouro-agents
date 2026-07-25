from __future__ import annotations

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from ouro_agents.cancellation import RunCancellationToken, RunCancelled
from ouro_agents.controller_questions import (
    ControllerDecisionBroker,
    ControllerQuestionManager,
)
from ouro_agents.run_context import RunContext, bind_run_context
from ouro_agents.run_log import RunLogStore
from ouro_agents.security.action_gates import observed_action_category


def _manager(tmp_path, *, fast_wait_seconds=1.0):
    store = RunLogStore(tmp_path / "runs.db")
    manager = ControllerQuestionManager(
        agent_name="hermes",
        org_id="org-1",
        controller_ids=lambda: ["controller-1"],
        own_user_id=lambda: "agent-1",
        ouro_client=lambda: SimpleNamespace(),
        store=store,
        fast_wait_seconds=fast_wait_seconds,
    )
    sent: list[tuple[str, str]] = []
    manager._send_message = lambda conversation_id, text: sent.append(
        (conversation_id, text)
    )
    return manager, store, sent


def test_quick_controller_answer_continues_waiting_call(tmp_path):
    manager, store, sent = _manager(tmp_path)
    result: dict[str, str] = {}

    def ask():
        with bind_run_context(
            RunContext(run_id="run-1", mode="heartbeat", team_id="team-1")
        ):
            result.update(
                json.loads(
                    manager.ask(
                        question="Which date should I confirm?",
                        options=["Tuesday", "Wednesday"],
                        recommendation="Tuesday",
                        context="The weekday and numeric date conflict.",
                        proposed_action="Reply with the confirmed meeting date.",
                        cancellation_token=None,
                        preferred_conversation_id="conv-1",
                    )
                )
            )

    thread = threading.Thread(target=ask)
    thread.start()
    deadline = time.monotonic() + 1
    pending = []
    while time.monotonic() < deadline:
        pending = store.pending_controller_questions(
            conversation_id="conv-1", controller_user_id="controller-1"
        )
        if pending:
            break
        time.sleep(0.01)
    assert len(pending) == 1
    question_id = pending[0]["question_id"]

    resolution = manager.resolve_reply(
        conversation_id="conv-1",
        controller_user_id="controller-1",
        text=f"Decision {question_id[:8]}: Tuesday",
        message_id="message-1",
    )
    assert resolution.handled is True
    assert resolution.continued_live_run is True

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result["status"] == "answered"
    assert "Tuesday" in result["answer"]
    assert store.get_controller_question(question_id)["status"] == "completed"
    assert len(sent) == 1


def test_timeout_remains_durable_and_late_answer_can_be_claimed(tmp_path):
    manager, store, _sent = _manager(tmp_path, fast_wait_seconds=0.01)
    with bind_run_context(RunContext(run_id="run-2", mode="autonomous")):
        result = json.loads(
            manager.ask(
                question="Publish this claim?",
                options=["Publish", "Hold"],
                recommendation="Hold",
                context="The benchmark result is not independently verified.",
                proposed_action="Publish a public post.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    assert result["status"] == "waiting"
    question_id = result["question_id"]

    resolution = manager.resolve_reply(
        conversation_id="conv-1",
        controller_user_id="controller-1",
        text=f"Decision {question_id[:8]}: Hold",
        message_id="message-2",
    )
    assert resolution.handled is True
    assert resolution.continued_live_run is False
    claimed = manager.claim_for_resume(question_id)
    assert claimed is not None
    assert claimed["status"] == "resuming"
    assert manager.claim_for_resume(question_id) is None


def test_duplicate_answer_message_is_consumed_without_second_resume(tmp_path):
    manager, store, _sent = _manager(tmp_path, fast_wait_seconds=0)
    with bind_run_context(RunContext(run_id="run-3", mode="heartbeat")):
        result = json.loads(
            manager.ask(
                question="Send it?",
                options=["Send", "Do not send"],
                recommendation="Do not send",
                context="",
                proposed_action="Send an external email.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    question_id = result["question_id"]
    first = manager.resolve_reply(
        conversation_id="conv-1",
        controller_user_id="controller-1",
        text=f"Decision {question_id[:8]}: Do not send",
        message_id="same-message",
    )
    second = manager.resolve_reply(
        conversation_id="conv-1",
        controller_user_id="controller-1",
        text=f"Decision {question_id[:8]}: Do not send",
        message_id="duplicate-webhook-message",
    )
    assert first.handled and second.handled
    assert store.get_controller_question(question_id)["answer_message_id"] == "same-message"


def test_broker_discards_timed_out_waiter():
    broker = ControllerDecisionBroker()
    broker.register("q1")
    assert broker.wait("q1", timeout_seconds=0, cancellation_token=None) is None
    assert broker.resolve("q1", "late") is False


def test_cancelled_wait_does_not_resume_later(tmp_path):
    manager, store, _sent = _manager(tmp_path)
    token = RunCancellationToken()
    token.cancel("user stopped")
    with (
        bind_run_context(RunContext(run_id="run-4", mode="heartbeat")),
        pytest.raises(RunCancelled),
    ):
        manager.ask(
            question="Proceed?",
            options=["Proceed", "Stop"],
            recommendation="Stop",
            context="",
            proposed_action="External action",
            cancellation_token=token,
            preferred_conversation_id="conv-1",
        )
    rows = store.pending_controller_questions(
        conversation_id="conv-1", controller_user_id="controller-1"
    )
    assert rows == []


def test_mvp_action_categories_are_narrow():
    assert observed_action_category("ouro:send_money") == "money"
    assert observed_action_category("delete_asset") == "destructive"
    assert observed_action_category("create_scheduled_task") == "scheduling"
    assert observed_action_category("resend:send_email") == "externally_visible"
    assert observed_action_category("query_dataset") is None


def test_observe_mode_gate_record_omits_arguments(tmp_path):
    db = tmp_path / "runs.db"
    store = RunLogStore(db)
    store.record_action_gate_observation(
        run_id="run-5", tool_name="send_email", category="externally_visible"
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        """
        SELECT run_id, tool_name, category
        FROM action_gate_observations
        """
    ).fetchone()
    assert row == ("run-5", "send_email", "externally_visible")
    columns = {
        item[1]
        for item in conn.execute(
            "PRAGMA table_info(action_gate_observations)"
        ).fetchall()
    }
    assert "arguments" not in columns

