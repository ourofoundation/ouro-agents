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
    _truncate_at_sentence,
    render_standing_controller_decisions,
)
from ouro_agents.run_context import RunContext, bind_run_context
from ouro_agents.run_log import ControllerQuestionRecord, RunLogStore
from ouro_agents.security.action_gates import observed_action_category


def _manager(tmp_path, *, fast_wait_seconds=1.0, remember_direction=None):
    store = RunLogStore(tmp_path / "runs.db")
    manager = ControllerQuestionManager(
        agent_name="hermes",
        org_id="org-1",
        controller_ids=lambda: ["controller-1"],
        own_user_id=lambda: "agent-1",
        ouro_client=lambda: SimpleNamespace(),
        store=store,
        fast_wait_seconds=fast_wait_seconds,
        remember_direction=remember_direction,
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


def test_recent_controller_questions_returns_newest_waiting_and_settled(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    waiting = ControllerQuestionRecord(
        question_id="019faf77-aaaa-bbbb-cccc-ddddeeee0001",
        agent_name="hermes",
        origin_run_id="run-a",
        origin_mode="heartbeat",
        controller_user_id="controller-1",
        conversation_id="conv-1",
        question="Will you email Janine to schedule a call?",
        status="waiting",
    )
    completed = ControllerQuestionRecord(
        question_id="019faf44-aaaa-bbbb-cccc-ddddeeee0002",
        agent_name="hermes",
        origin_run_id="run-b",
        origin_mode="heartbeat",
        controller_user_id="controller-1",
        conversation_id="conv-1",
        question="Should I send spinel correction emails?",
        status="completed",
        answer="No corrections. Drop the topic.",
        answered_at="2026-07-29T19:09:39+00:00",
    )
    assert store.create_controller_question(waiting)
    assert store.create_controller_question(completed)
    rows = store.recent_controller_questions(days=14, limit=8)
    assert len(rows) == 2
    assert {row["question_id"] for row in rows} == {
        waiting.question_id,
        completed.question_id,
    }


def test_standing_decisions_block_renders_settled_and_pending():
    block = render_standing_controller_decisions(
        [
            {
                "question_id": "019faf44-aaaa",
                "status": "completed",
                "question": "Should I send spinel correction emails to Shapeev?",
                "answer": "No corrections, drop the topic, move on.",
                "answered_at": "2026-07-29T19:09:39+00:00",
            },
            {
                "question_id": "019faf77-bbbb",
                "status": "waiting",
                "question": "Will you email Janine to schedule a call?",
                "created_at": "2026-07-29T20:01:30+00:00",
            },
        ]
    )
    assert "## Standing Controller Decisions" in block
    assert "Settled" in block
    assert "Pending" in block
    assert "019faf44" in block
    assert "019faf77" in block
    assert "No corrections" in block


def test_duplicate_pending_ask_is_blocked(tmp_path):
    manager, _store, _sent = _manager(tmp_path, fast_wait_seconds=0)
    with bind_run_context(RunContext(run_id="run-dup-1", mode="heartbeat")):
        first = json.loads(
            manager.ask(
                question=(
                    "Three outreach emails falsely present the retracted Co3O4 "
                    "spinel collapse. Should I prepare correction emails?"
                ),
                options=["Prepare drafts", "Stand down"],
                recommendation="Prepare drafts",
                context="Shapeev and Batatia emails.",
                proposed_action="Prepare correction email drafts for Shapeev.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    assert first["status"] == "waiting"
    with bind_run_context(RunContext(run_id="run-dup-2", mode="heartbeat")):
        second = json.loads(
            manager.ask(
                question=(
                    "The Shapeev email falsely claims Co3O4 spinel collapse. "
                    "Should I send a correction email to Shapeev?"
                ),
                options=["Send correction", "Do not send"],
                recommendation="Send correction",
                context="Shapeev email false claims.",
                proposed_action="Send correction email to Shapeev tomorrow.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    assert second["status"] == "already_pending"
    assert second["question_id"] == first["question_id"]


def test_duplicate_settled_ask_returns_prior_answer(tmp_path):
    manager, store, _sent = _manager(tmp_path, fast_wait_seconds=0)
    with bind_run_context(RunContext(run_id="run-set-1", mode="heartbeat")):
        first = json.loads(
            manager.ask(
                question=(
                    "Should I send spinel Co3O4 correction emails for Shapeev "
                    "and Batatia?"
                ),
                options=["Send corrections", "Do not send"],
                recommendation="Do not send",
                context="",
                proposed_action="Send correction emails about spinel claims.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    qid = first["question_id"]
    assert store.answer_controller_question(
        qid, answer="No correction follow ups. Move on.", answer_message_id="m1"
    )
    store.update_controller_question(qid, status="completed")
    with bind_run_context(RunContext(run_id="run-set-2", mode="heartbeat")):
        second = json.loads(
            manager.ask(
                question=(
                    "Should I send a spinel Co3O4 correction email to Shapeev "
                    "tomorrow?"
                ),
                options=["Send", "Do not send"],
                recommendation="Send",
                context="",
                proposed_action="Send Shapeev spinel correction email.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    assert second["status"] == "already_decided"
    assert "Move on" in second["answer"]
    assert second["question_id"] == qid


def test_supersedes_bypasses_dedupe_guard(tmp_path):
    manager, store, _sent = _manager(tmp_path, fast_wait_seconds=0)
    with bind_run_context(RunContext(run_id="run-sup-1", mode="heartbeat")):
        first = json.loads(
            manager.ask(
                question="Should I email Janine George about scheduling a call?",
                options=["Email her", "Stand down"],
                recommendation="Stand down",
                context="",
                proposed_action="Email Janine George to schedule a call.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    qid = first["question_id"]
    assert store.answer_controller_question(
        qid, answer="Matt owns the Janine thread.", answer_message_id="m2"
    )
    store.update_controller_question(qid, status="completed")
    with bind_run_context(RunContext(run_id="run-sup-2", mode="heartbeat")):
        second = json.loads(
            manager.ask(
                question="Should I email Janine George about scheduling a call?",
                options=["Email her", "Stand down"],
                recommendation="Stand down",
                context="New facts arrived.",
                proposed_action="Email Janine George to schedule a call.",
                supersedes=qid[:8],
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    assert second["status"] == "waiting"
    assert second["question_id"] != qid


def test_send_question_truncates_verbose_fields(tmp_path):
    manager, _store, sent = _manager(tmp_path, fast_wait_seconds=0)
    long_question = ("Spinel correction email issue. " * 40).strip()
    long_context = ("Extra context about the benchmark dataset. " * 30).strip()
    with bind_run_context(RunContext(run_id="run-trunc", mode="heartbeat")):
        result = json.loads(
            manager.ask(
                question=long_question,
                options=["Yes", "No"],
                recommendation=("Recommend preparing drafts. " * 20).strip(),
                context=long_context,
                proposed_action=("Prepare correction drafts tonight. " * 20).strip(),
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    assert result["status"] == "waiting"
    assert len(sent) == 1
    body = sent[0][1]
    assert len(body) < len(long_question) + len(long_context)
    assert "…" in body or body.count("Spinel correction") < 40


def test_send_resume_result_is_capped(tmp_path):
    manager, _store, sent = _manager(tmp_path)
    long_result = (
        "Here is what I accomplished.\n\n"
        + ("I cleaned CRM rows and published research tables. " * 40)
    )
    manager.send_resume_result(
        {
            "question_id": "019faf61-aaaa-bbbb-cccc-ddddeeee0003",
            "conversation_id": "conv-1",
        },
        long_result,
    )
    assert len(sent) == 1
    body = sent[0][1]
    assert "019faf61" in body
    assert "full details in the run log" in body
    assert len(body) < len(long_result)
    assert "Here is what I accomplished." in body


def test_live_answer_consolidates_direction_memory(tmp_path):
    remembered: list[dict] = []

    def remember(direction, *, run_id="", team_id=None):
        remembered.append(
            {"direction": direction, "run_id": run_id, "team_id": team_id}
        )
        return True

    manager, _store, _sent = _manager(
        tmp_path, fast_wait_seconds=1.0, remember_direction=remember
    )
    result: dict[str, str] = {}

    def ask():
        with bind_run_context(
            RunContext(run_id="run-mem", mode="heartbeat", team_id="team-9")
        ):
            result.update(
                json.loads(
                    manager.ask(
                        question="Should I keep discussing the spinel issue?",
                        options=["Continue", "Stop discussing it"],
                        recommendation="Stop discussing it",
                        context="",
                        proposed_action="Send more spinel correction emails.",
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
        pending = manager.store.pending_controller_questions(
            conversation_id="conv-1", controller_user_id="controller-1"
        )
        if pending:
            break
        time.sleep(0.01)
    qid = pending[0]["question_id"]
    manager.resolve_reply(
        conversation_id="conv-1",
        controller_user_id="controller-1",
        text=f"Decision {qid[:8]}: Stop discussing spinel and move on to new research.",
        message_id="message-mem",
    )
    thread.join(timeout=2)
    assert result["status"] == "answered"
    assert len(remembered) == 1
    assert "Stop discussing" in remembered[0]["direction"]
    assert remembered[0]["run_id"] == "run-mem"
    assert remembered[0]["team_id"] == "team-9"


def test_mark_resume_result_consolidates_direction_memory(tmp_path):
    remembered: list[str] = []

    def remember(direction, **kwargs):
        remembered.append(direction)
        return True

    manager, store, _sent = _manager(
        tmp_path,
        fast_wait_seconds=0,
        remember_direction=remember,
    )
    with bind_run_context(RunContext(run_id="run-resume-ask", mode="heartbeat")):
        first = json.loads(
            manager.ask(
                question="Should Hermes email Janine George again?",
                options=["Email", "Do not email"],
                recommendation="Do not email",
                context="",
                proposed_action="Email Janine George from Hermes.",
                cancellation_token=None,
                preferred_conversation_id="conv-1",
            )
        )
    qid = first["question_id"]
    store.answer_controller_question(
        qid,
        answer="Matt owns Janine; Hermes must not email her.",
        answer_message_id="m3",
    )
    claimed = manager.claim_for_resume(qid)
    assert claimed is not None
    manager.mark_resume_result(qid, result="Updated CRM stand-down note.")
    assert any("Janine" in text for text in remembered)


def test_truncate_at_sentence_prefers_boundary():
    text = "First sentence about outreach. Second sentence keeps going forever " + (
        "and ever " * 40
    )
    truncated = _truncate_at_sentence(text, 80)
    assert truncated.endswith(".")
    assert "First sentence about outreach." in truncated
    assert len(truncated) <= 80


def test_recent_tick_digest_includes_controller_decisions():
    from types import SimpleNamespace
    from ouro_agents.modes.heartbeat import build_recent_tick_digest

    class FakeRunLog:
        def query_runs(self, **kwargs):
            if kwargs.get("mode") == "heartbeat":
                return [
                    {
                        "mode": "heartbeat",
                        "event_type": None,
                        "status": "success",
                        "started_at": "2026-07-29T19:00:00+00:00",
                        "preflight_intent": "act",
                        "preflight_complexity": "priority:1",
                        "result": '{"action": "flagged_integrity", "details": "audited emails"}',
                    }
                ]
            if kwargs.get("event_type") == "controller-decision":
                return [
                    {
                        "mode": "autonomous",
                        "event_type": "controller-decision",
                        "status": "success",
                        "started_at": "2026-07-29T19:09:00+00:00",
                        "result": "Applied no spinel corrections and moved on.",
                    }
                ]
            return []

    digest = build_recent_tick_digest(SimpleNamespace(_run_log=FakeRunLog()), limit=5)
    assert "controller-decision" in digest
    assert "flagged_integrity" in digest
    assert "Applied no spinel corrections" in digest
