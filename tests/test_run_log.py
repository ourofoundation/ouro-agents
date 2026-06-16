"""Tests for the SQLite run log (ouro_agents.run_log) and step extraction."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from smolagents.memory import ActionStep, PlanningStep, TaskStep
from smolagents.monitoring import Timing

from ouro_agents.run_log import RunLogStore, RunRecord, RunStepRecord
from ouro_agents.tools.run_history_tools import make_run_history_tools
from ouro_agents.utils.conversation import extract_run_steps


def _row_to_dict(conn: sqlite3.Connection, table: str, run_id: str) -> dict:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f"SELECT * FROM {table} WHERE run_id = ?", (run_id,))
    row = cur.fetchone()
    return dict(row) if row else {}


def _fake_agent(steps):
    return SimpleNamespace(memory=SimpleNamespace(steps=steps))


def _timing(start=1.0, end=2.5):
    t = Timing(start_time=start)
    t.end_time = end
    return t


# --------------------------------------------------------------------------- #
# Store / schema
# --------------------------------------------------------------------------- #


def test_store_creates_schema_and_is_idempotent(tmp_path):
    db = tmp_path / "runs.db"
    store = RunLogStore(db)
    assert db.exists()
    store.close()

    # Re-opening the same path must not error and must keep the schema.
    store2 = RunLogStore(db)
    conn = sqlite3.connect(str(db))
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"runs", "run_steps"} <= tables
    conn.close()
    store2.close()


def test_write_round_trips_full_record(tmp_path):
    db = tmp_path / "runs.db"
    store = RunLogStore(db)
    record = RunRecord(
        run_id="run-1",
        agent_name="hermes",
        mode="chat",
        status="success",
        conversation_id="conv-1",
        team_id="team-1",
        user_id="user-1",
        event_type="new-message",
        trigger_turn_id="turn-1",
        capability_role="owner",
        capability_surface="chat",
        model="anthropic/claude",
        task="What teams am I on?",
        result="You are on team-1.",
        preflight_intent="question",
        preflight_complexity="simple",
        worth_remembering=True,
    )
    record.set_steps(
        [
            RunStepRecord(step_type="task", model_output="What teams am I on?"),
            RunStepRecord(
                step_number=1,
                step_type="action",
                model_output="Looking it up",
                tool_calls=[{"name": "get_teams", "args": {}}],
                observations="team-1",
            ),
        ]
    )
    store.write(record)

    conn = sqlite3.connect(str(db))
    row = _row_to_dict(conn, "runs", "run-1")
    assert row["agent_name"] == "hermes"
    assert row["mode"] == "chat"
    assert row["conversation_id"] == "conv-1"
    assert row["team_id"] == "team-1"
    assert row["event_type"] == "new-message"
    assert row["capability_role"] == "owner"
    assert row["task"] == "What teams am I on?"
    assert row["result"] == "You are on team-1."
    assert row["preflight_intent"] == "question"
    assert row["worth_remembering"] == 1  # stored as int
    assert row["num_steps"] == 2
    assert row["num_tool_calls"] == 1

    steps = conn.execute(
        "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index", ("run-1",)
    ).fetchall()
    assert len(steps) == 2
    conn.row_factory = sqlite3.Row
    action = conn.execute(
        "SELECT * FROM run_steps WHERE run_id=? AND step_type='action'", ("run-1",)
    ).fetchone()
    assert json.loads(action["tool_calls_json"]) == [{"name": "get_teams", "args": {}}]
    assert action["observations"] == "team-1"
    conn.close()
    store.close()


def test_write_is_idempotent_on_same_run_id(tmp_path):
    db = tmp_path / "runs.db"
    store = RunLogStore(db)
    rec = RunRecord(run_id="run-x", mode="chat")
    rec.set_steps([RunStepRecord(step_type="action")])
    store.write(rec)
    rec.mode = "autonomous"
    rec.set_steps([RunStepRecord(step_type="action"), RunStepRecord(step_type="final")])
    store.write(rec)

    conn = sqlite3.connect(str(db))
    runs = conn.execute("SELECT mode FROM runs WHERE run_id='run-x'").fetchall()
    assert len(runs) == 1
    assert runs[0][0] == "autonomous"
    steps = conn.execute(
        "SELECT COUNT(*) FROM run_steps WHERE run_id='run-x'"
    ).fetchone()[0]
    assert steps == 2  # old steps replaced, not duplicated
    conn.close()
    store.close()


# --------------------------------------------------------------------------- #
# Lifecycle / status
# --------------------------------------------------------------------------- #


def test_mark_error_captures_type_message_and_traceback():
    rec = RunRecord(run_id="r")
    try:
        raise ValueError("boom")
    except ValueError as e:
        rec.mark_error(e)
    assert rec.status == "error"
    assert rec.error_type == "ValueError"
    assert rec.error_message == "boom"
    assert "ValueError" in (rec.error_traceback or "")


def test_mark_cancelled_and_success():
    rec = RunRecord(run_id="r")
    rec.mark_cancelled("user cancelled")
    assert rec.status == "cancelled"
    assert rec.error_message == "user cancelled"

    rec2 = RunRecord(run_id="r2")
    rec2.mark_success("done")
    assert rec2.status == "success"
    assert rec2.result == "done"


def test_status_values_persist(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    for rid, status in [("a", "success"), ("b", "error"), ("c", "cancelled")]:
        store.write(RunRecord(run_id=rid, mode="heartbeat", status=status))
    conn = sqlite3.connect(str(tmp_path / "runs.db"))
    got = dict(conn.execute("SELECT run_id, status FROM runs").fetchall())
    assert got == {"a": "success", "b": "error", "c": "cancelled"}
    conn.close()
    store.close()


# --------------------------------------------------------------------------- #
# Grouping: parent / tick linkage
# --------------------------------------------------------------------------- #


def test_parent_and_tick_linkage(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    store.write(RunRecord(run_id="planning", mode="plan", tick_id="tick-1"))
    store.write(RunRecord(run_id="action", mode="heartbeat", tick_id="tick-1"))
    store.write(
        RunRecord(run_id="child", mode="subagent:research", parent_run_id="action")
    )
    conn = sqlite3.connect(str(tmp_path / "runs.db"))
    tick = conn.execute(
        "SELECT run_id FROM runs WHERE tick_id='tick-1' ORDER BY run_id"
    ).fetchall()
    assert [r[0] for r in tick] == ["action", "planning"]
    child = conn.execute(
        "SELECT parent_run_id FROM runs WHERE run_id='child'"
    ).fetchone()
    assert child[0] == "action"
    conn.close()
    store.close()


# --------------------------------------------------------------------------- #
# Disabled store
# --------------------------------------------------------------------------- #


def test_disabled_store_is_noop(tmp_path):
    db = tmp_path / "runs.db"
    store = RunLogStore(db, enabled=False)
    store.write(RunRecord(run_id="r", mode="chat"))
    assert not db.exists()
    store.close()


# --------------------------------------------------------------------------- #
# Step extraction
# --------------------------------------------------------------------------- #


def test_extract_run_steps_structures_each_step_type():
    msg = SimpleNamespace(reasoning="because reasons")
    tc = SimpleNamespace(
        function=SimpleNamespace(name="get_asset", arguments={"id": "x"})
    )
    steps = [
        TaskStep(task="do the thing"),
        PlanningStep(
            model_input_messages=[],
            model_output_message=msg,
            plan="1. step one",
            timing=_timing(),
        ),
        ActionStep(
            step_number=1,
            timing=_timing(),
            tool_calls=[tc],
            observations="OK",
            model_output="calling tool",
            model_output_message=msg,
            is_final_answer=False,
        ),
        ActionStep(
            step_number=2,
            timing=_timing(),
            tool_calls=[],
            observations="final answer text",
            model_output="here is the answer",
            is_final_answer=True,
        ),
    ]
    out = extract_run_steps(_fake_agent(steps))
    assert [s.step_type for s in out] == ["task", "planning", "action", "final"]

    task_step = out[0]
    assert task_step.model_output == "do the thing"

    planning = out[1]
    assert planning.model_output == "1. step one"
    assert planning.reasoning == "because reasons"

    action = out[2]
    assert action.step_number == 1
    assert action.tool_calls == [{"name": "get_asset", "args": {"id": "x"}}]
    assert action.observations == "OK"
    assert action.reasoning == "because reasons"
    assert action.is_final_answer is False
    assert action.duration_s == 1.5

    final = out[3]
    assert final.is_final_answer is True
    assert final.step_type == "final"


def test_extract_run_steps_captures_errors():
    step = ActionStep(
        step_number=1,
        timing=_timing(),
        tool_calls=[],
        error=RuntimeError("tool exploded"),
    )
    out = extract_run_steps(_fake_agent([step]))
    assert out[0].error is not None
    assert "tool exploded" in out[0].error


def test_extract_run_steps_truncates_observations_when_capped():
    big = "x" * 5000
    step = ActionStep(
        step_number=1, timing=_timing(), tool_calls=[], observations=big
    )
    capped = extract_run_steps(_fake_agent([step]), max_observation_chars=100)
    assert capped[0].observations is not None
    assert len(capped[0].observations) == 103  # 100 + "..."
    assert capped[0].observations.endswith("...")

    uncapped = extract_run_steps(_fake_agent([step]), max_observation_chars=0)
    assert uncapped[0].observations == big


def test_set_steps_computes_rollups():
    rec = RunRecord(run_id="r")
    rec.set_steps(
        [
            RunStepRecord(step_type="action", tool_calls=[{"name": "a", "args": {}}]),
            RunStepRecord(
                step_type="action",
                tool_calls=[{"name": "b", "args": {}}, {"name": "c", "args": {}}],
            ),
        ]
    )
    assert rec.num_steps == 2
    assert rec.num_tool_calls == 3


# --------------------------------------------------------------------------- #
# Query API
# --------------------------------------------------------------------------- #


def _seed(store):
    store.write(RunRecord(run_id="cur", mode="chat", team_id="t1", task="current"))
    store.write(
        RunRecord(
            run_id="mine",
            mode="chat",
            team_id="t1",
            task="research widgets",
            result="found three",
            status="success",
        )
    )
    store.write(RunRecord(run_id="shared", mode="heartbeat", team_id=None, task="tick"))
    store.write(
        RunRecord(run_id="other", mode="chat", team_id="t2", task="other team secret")
    )


def test_query_runs_filters(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    _seed(store)
    assert {r["run_id"] for r in store.query_runs(mode="heartbeat")} == {"shared"}
    assert {r["run_id"] for r in store.query_runs(grep="widgets")} == {"mine"}
    assert {r["run_id"] for r in store.query_runs(grep="secret")} == {"other"}
    excl = {r["run_id"] for r in store.query_runs(exclude_run_id="cur")}
    assert "cur" not in excl
    store.close()


def test_query_runs_team_scope_includes_shared(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    _seed(store)
    scoped = {r["run_id"] for r in store.query_runs(team_id="t1")}
    assert scoped == {"cur", "mine", "shared"}  # own team + shared, never t2
    exact = {r["run_id"] for r in store.query_runs(team_id="t1", include_shared_team=False)}
    assert exact == {"cur", "mine"}
    store.close()


def test_stats_by_mode(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    store.write(RunRecord(run_id="a", mode="chat", status="success", total_tokens=10))
    store.write(RunRecord(run_id="b", mode="chat", status="error", total_tokens=5))
    store.write(RunRecord(run_id="c", mode="heartbeat", status="success"))
    by_mode = {r["mode"]: r for r in store.stats_by_mode()}
    assert by_mode["chat"]["runs"] == 2
    assert by_mode["chat"]["failures"] == 1
    assert by_mode["chat"]["total_tokens"] == 15
    store.close()


def test_readonly_over_missing_db_is_empty(tmp_path):
    store = RunLogStore(tmp_path / "nope.db", readonly=True)
    assert store.enabled is False
    assert store.query_runs() == []
    assert store.get_run("x") is None


# --------------------------------------------------------------------------- #
# Agent recall tools (scoping / privacy)
# --------------------------------------------------------------------------- #


def test_recall_runs_team_scope_excludes_other_teams_and_current(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    _seed(store)
    recall, _ = make_run_history_tools(
        store, current_run_id="cur", team_id="t1", conversation_id=None,
        default_scope="team",
    )
    out = json.loads(recall())
    ids = {r["run_id"] for r in out["runs"]}
    assert ids == {"mine", "shared"}  # excludes current run + other team
    assert out["scope"] == "team"
    store.close()


def test_recall_runs_cannot_widen_past_ceiling(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    _seed(store)
    recall, _ = make_run_history_tools(
        store, current_run_id="cur", team_id="t1", conversation_id=None,
        default_scope="team",
    )
    out = json.loads(recall(scope="all"))  # tries to widen
    assert out["scope"] == "team"  # clamped
    assert "other" not in {r["run_id"] for r in out["runs"]}
    store.close()


def test_get_run_detail_blocks_out_of_scope(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    _seed(store)
    _, detail = make_run_history_tools(
        store, current_run_id="cur", team_id="t1", conversation_id=None,
        default_scope="team",
    )
    blocked = json.loads(detail("other"))
    assert "error" in blocked
    ok = json.loads(detail("mine"))
    assert ok["run_id"] == "mine"
    store.close()


def test_all_ceiling_sees_every_team(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    _seed(store)
    recall, detail = make_run_history_tools(
        store, current_run_id="cur", team_id="t1", conversation_id=None,
        default_scope="all",
    )
    ids = {r["run_id"] for r in json.loads(recall())["runs"]}
    assert ids == {"mine", "shared", "other"}
    assert json.loads(detail("other"))["run_id"] == "other"
    store.close()


def test_get_run_detail_truncates_to_budget(tmp_path):
    store = RunLogStore(tmp_path / "runs.db")
    rec = RunRecord(run_id="big", mode="chat", team_id="t1")
    rec.set_steps([RunStepRecord(step_type="action", observations="y" * 10000)])
    store.write(rec)
    _, detail = make_run_history_tools(
        store, current_run_id=None, team_id="t1", conversation_id=None,
        default_scope="team", max_detail_chars=500,
    )
    out = json.loads(detail("big"))
    assert "truncated" in out["steps"][0]["observations"]
    assert len(out["steps"][0]["observations"]) < 10000
    store.close()
