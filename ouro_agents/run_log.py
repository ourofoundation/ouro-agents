"""SQLite-backed run logging.

Every agent run — in every mode (chat, autonomous, heartbeat,
plan, review, dream) — writes one rich, structured record to a single SQLite database
(`<workspace>/runs.db`) so past runs can be revisited in full. Writes happen on
success, error, and cancellation alike.

The schema has two core run tables plus small controller-workflow tables:

- ``runs`` — one row per run: identity, routing/context, lifecycle/status,
  full task + result, preflight, flattened usage rollups for cheap querying,
  plus full JSON blobs (usage / subagent ledger / memory ledger).
- ``run_steps`` — one row per smolagents memory step: the full replay trace
  (model output, reasoning, tool calls, observations, errors, timing).
- ``controller_questions`` — cross-run controller decisions and resume state.
- ``action_gate_observations`` — argument-free observe-mode gate hits.

Writes are best-effort: a logging failure must never break a run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Sentinel for query filters where None is a meaningful value (e.g. team_id).
_UNSET = object()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunStepRecord:
    """One smolagents memory step, structured for storage."""

    step_number: Optional[int] = None
    step_type: str = "other"  # task | action | planning | final | other
    model_output: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: list[dict] = field(default_factory=list)  # [{"name", "args"}]
    observations: Optional[str] = None
    error: Optional[str] = None
    is_final_answer: bool = False
    duration_s: Optional[float] = None


@dataclass
class RunRecord:
    """A single agent run. Built up incrementally over the run lifecycle."""

    run_id: str
    agent_name: str = ""
    mode: str = ""
    status: str = "success"  # success | error | cancelled
    started_at: str = field(default_factory=_utc_now_iso)
    ended_at: str = ""
    duration_s: Optional[float] = None

    # Identity / grouping
    parent_run_id: Optional[str] = None
    tick_id: Optional[str] = None

    # Routing / context
    event_type: Optional[str] = None
    conversation_id: Optional[str] = None
    team_id: Optional[str] = None
    user_id: Optional[str] = None
    trigger_turn_id: Optional[str] = None
    capability_role: Optional[str] = None
    capability_surface: Optional[str] = None

    # IO
    model: str = ""
    task: str = ""
    result: str = ""
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    error_traceback: Optional[str] = None

    # Preflight
    preflight_intent: Optional[str] = None
    preflight_complexity: Optional[str] = None
    worth_remembering: Optional[bool] = None

    # Rollups (flattened for cheap querying)
    num_steps: int = 0
    num_tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    num_api_calls: int = 0
    cost_usd: Optional[float] = None

    # Full blobs
    usage_json: Optional[str] = None
    subagent_ledger_json: Optional[str] = None
    memory_ledger_json: Optional[str] = None

    steps: list[RunStepRecord] = field(default_factory=list)

    # Runtime-only handles used to capture steps/usage in a finally block even
    # when a run errors mid-flight. Never persisted.
    _agent: Any = field(default=None, repr=False, compare=False)
    _model_obj: Any = field(default=None, repr=False, compare=False)

    # ---- lifecycle helpers -------------------------------------------------

    def mark_success(self, result: Any) -> None:
        self.status = "success"
        self.result = "" if result is None else str(result)

    def mark_cancelled(self, reason: str = "") -> None:
        self.status = "cancelled"
        if reason and not self.error_message:
            self.error_message = reason

    def mark_error(self, exc: BaseException) -> None:
        import traceback

        self.status = "error"
        self.error_type = type(exc).__name__
        self.error_message = str(exc)
        self.error_traceback = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )

    def set_usage(self, usage: Any) -> None:
        """Populate flattened rollups + the full usage blob from a RunUsage."""
        if usage is None:
            return
        try:
            self.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            self.cached_input_tokens = int(
                getattr(usage, "cached_input_tokens", 0) or 0
            )
            self.reasoning_tokens = int(getattr(usage, "reasoning_tokens", 0) or 0)
            self.total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            self.num_api_calls = int(getattr(usage, "num_api_calls", 0) or 0)
            self.cost_usd = getattr(usage, "cost_usd", None)
            if hasattr(usage, "dict"):
                self.usage_json = json.dumps(usage.dict(), default=str)
        except Exception:
            logger.debug("Failed to read usage into run record", exc_info=True)

    def set_subagent_ledger(self, ledger: Any) -> None:
        self.subagent_ledger_json = _serialize_ledger(ledger)

    def set_memory_ledger(self, ledger: Any) -> None:
        self.memory_ledger_json = _serialize_ledger(ledger)

    def set_steps(self, steps: list[RunStepRecord]) -> None:
        self.steps = steps
        self.num_steps = len(steps)
        self.num_tool_calls = sum(len(s.tool_calls) for s in steps)

    def finalize_timing(self, duration_s: Optional[float] = None) -> None:
        self.ended_at = _utc_now_iso()
        if duration_s is not None:
            self.duration_s = max(0.0, duration_s)


@dataclass
class ChatTurnRecord:
    """Per-turn chat context accounting for one chat run.

    Chat quality work (history policy, compaction thresholds, cache behavior)
    is untunable without knowing what actually went into each turn's prompt and
    what came back. One row per chat turn records the history that was injected,
    the estimated size of each prompt section, and the provider's cache accounting.
    """

    run_id: str
    conversation_id: str = ""
    agent_name: str = ""
    trigger_turn_id: Optional[str] = None
    model: str = ""

    # History accounting
    turns_fetched: int = 0
    turns_injected: int = 0
    history_steps: int = 0
    history_covers_conversation: bool = True
    dropped_oldest_turns: int = 0

    # Estimated prompt composition (chars // CHARS_PER_TOKEN)
    est_system_tokens: int = 0
    est_dynamic_tokens: int = 0
    est_history_tokens: int = 0
    est_task_tokens: int = 0
    est_prompt_tokens: int = 0

    # Actual provider accounting for the whole run
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    num_api_calls: int = 0
    cost_usd: Optional[float] = None

    # Compaction
    compacted: bool = False
    compaction_reason: Optional[str] = None

    duration_s: Optional[float] = None
    created_at: str = field(default_factory=_utc_now_iso)

    @property
    def cache_hit_ratio(self) -> float:
        """Share of input tokens served from the prompt cache."""
        if self.input_tokens <= 0:
            return 0.0
        return self.cached_input_tokens / self.input_tokens


@dataclass
class ControllerQuestionRecord:
    """A controller question that may outlive the run which asked it."""

    question_id: str
    agent_name: str
    origin_run_id: str
    origin_mode: str
    controller_user_id: str
    conversation_id: str
    question: str
    context: str = ""
    options: list[str] = field(default_factory=list)
    recommendation: str = ""
    proposed_action: str = ""
    team_id: Optional[str] = None
    status: str = "waiting"
    answer: Optional[str] = None
    answer_message_id: Optional[str] = None
    resume_run_id: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_utc_now_iso)
    answered_at: Optional[str] = None
    updated_at: str = field(default_factory=_utc_now_iso)


def _serialize_ledger(ledger: Any) -> Optional[str]:
    """Serialize a ``list[tuple[str, usage]]`` ledger to JSON, or None."""
    if not ledger:
        return None
    try:
        out: list[dict] = []
        for name, usage in ledger:
            entry: dict = {"name": name}
            if hasattr(usage, "dict"):
                entry.update(usage.dict())
            out.append(entry)
        return json.dumps(out, default=str)
    except Exception:
        logger.debug("Failed to serialize usage ledger", exc_info=True)
        return None


_RUNS_COLUMNS: tuple[str, ...] = (
    "run_id",
    "parent_run_id",
    "tick_id",
    "agent_name",
    "mode",
    "event_type",
    "status",
    "started_at",
    "ended_at",
    "duration_s",
    "conversation_id",
    "team_id",
    "user_id",
    "trigger_turn_id",
    "capability_role",
    "capability_surface",
    "model",
    "task",
    "result",
    "error_type",
    "error_message",
    "error_traceback",
    "preflight_intent",
    "preflight_complexity",
    "worth_remembering",
    "num_steps",
    "num_tool_calls",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_tokens",
    "num_api_calls",
    "cost_usd",
    "usage_json",
    "subagent_ledger_json",
    "memory_ledger_json",
    "created_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    tick_id TEXT,
    agent_name TEXT,
    mode TEXT,
    event_type TEXT,
    status TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_s REAL,
    conversation_id TEXT,
    team_id TEXT,
    user_id TEXT,
    trigger_turn_id TEXT,
    capability_role TEXT,
    capability_surface TEXT,
    model TEXT,
    task TEXT,
    result TEXT,
    error_type TEXT,
    error_message TEXT,
    error_traceback TEXT,
    preflight_intent TEXT,
    preflight_complexity TEXT,
    worth_remembering INTEGER,
    num_steps INTEGER,
    num_tool_calls INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    num_api_calls INTEGER,
    cost_usd REAL,
    usage_json TEXT,
    subagent_ledger_json TEXT,
    memory_ledger_json TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    step_number INTEGER,
    step_type TEXT,
    model_output TEXT,
    reasoning TEXT,
    tool_calls_json TEXT,
    observations TEXT,
    error TEXT,
    is_final_answer INTEGER,
    duration_s REAL
);

CREATE TABLE IF NOT EXISTS controller_questions (
    question_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    origin_run_id TEXT NOT NULL,
    origin_mode TEXT,
    team_id TEXT,
    controller_user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT,
    options_json TEXT,
    recommendation TEXT,
    proposed_action TEXT,
    status TEXT NOT NULL,
    answer TEXT,
    answer_message_id TEXT,
    resume_run_id TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_gate_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_turns (
    run_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    agent_name TEXT,
    trigger_turn_id TEXT,
    model TEXT,
    turns_fetched INTEGER,
    turns_injected INTEGER,
    history_steps INTEGER,
    history_covers_conversation INTEGER,
    dropped_oldest_turns INTEGER,
    est_system_tokens INTEGER,
    est_dynamic_tokens INTEGER,
    est_history_tokens INTEGER,
    est_task_tokens INTEGER,
    est_prompt_tokens INTEGER,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    cache_write_tokens INTEGER,
    output_tokens INTEGER,
    num_api_calls INTEGER,
    cost_usd REAL,
    compacted INTEGER,
    compaction_reason TEXT,
    duration_s REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_mode ON runs(mode);
CREATE INDEX IF NOT EXISTS idx_runs_conversation_id ON runs(conversation_id);
CREATE INDEX IF NOT EXISTS idx_runs_team_id ON runs(team_id);
CREATE INDEX IF NOT EXISTS idx_runs_parent_run_id ON runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_tick_id ON runs(tick_id);
CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_controller_questions_conversation
    ON controller_questions(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_controller_questions_origin_run
    ON controller_questions(origin_run_id);
CREATE INDEX IF NOT EXISTS idx_action_gate_observations_run
    ON action_gate_observations(run_id);
CREATE INDEX IF NOT EXISTS idx_chat_turns_conversation
    ON chat_turns(conversation_id, created_at);
"""

_CHAT_TURN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "conversation_id",
    "agent_name",
    "trigger_turn_id",
    "model",
    "turns_fetched",
    "turns_injected",
    "history_steps",
    "history_covers_conversation",
    "dropped_oldest_turns",
    "est_system_tokens",
    "est_dynamic_tokens",
    "est_history_tokens",
    "est_task_tokens",
    "est_prompt_tokens",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "num_api_calls",
    "cost_usd",
    "compacted",
    "compaction_reason",
    "duration_s",
    "created_at",
)


class RunLogStore:
    """Append-only SQLite store of agent runs and their step traces.

    Thread-safe: a single connection (``check_same_thread=False``) guarded by a
    lock, since post-run reflection and subagents run on background threads.
    WAL mode keeps concurrent reads non-blocking.
    """

    def __init__(
        self, path: Path | str, *, enabled: bool = True, readonly: bool = False
    ) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.readonly = readonly
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        if not self.enabled:
            return
        # A read-only store over a missing DB is simply empty (nothing has run
        # yet) — disable quietly so queries return [] instead of erroring.
        if readonly and not self.path.exists():
            self.enabled = False
            return
        try:
            self._connect()
        except Exception:
            logger.warning(
                "Failed to initialize run log at %s; disabling", self.path,
                exc_info=True,
            )
            self.enabled = False

    def _connect(self) -> None:
        if self.readonly:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(_SCHEMA)
            # chat_turns is new telemetry; drop+recreate if an earlier schema
            # still has the removed goals/topic columns.
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(chat_turns)").fetchall()
            }
            if "goals_before_json" in cols:
                conn.execute("DROP TABLE chat_turns")
                conn.executescript(_SCHEMA)
            conn.commit()
        conn.row_factory = sqlite3.Row
        self._conn = conn

    def write(self, record: RunRecord) -> None:
        """Persist a run and its steps. Best-effort; never raises."""
        if not self.enabled or self._conn is None:
            return
        try:
            with self._lock:
                self._write_locked(record)
        except Exception:
            logger.warning("Failed to write run log entry", exc_info=True)

    def _write_locked(self, record: RunRecord) -> None:
        assert self._conn is not None
        values = {
            "run_id": record.run_id,
            "parent_run_id": record.parent_run_id,
            "tick_id": record.tick_id,
            "agent_name": record.agent_name,
            "mode": record.mode,
            "event_type": record.event_type,
            "status": record.status,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "duration_s": record.duration_s,
            "conversation_id": record.conversation_id,
            "team_id": record.team_id,
            "user_id": record.user_id,
            "trigger_turn_id": record.trigger_turn_id,
            "capability_role": record.capability_role,
            "capability_surface": record.capability_surface,
            "model": record.model,
            "task": record.task,
            "result": record.result,
            "error_type": record.error_type,
            "error_message": record.error_message,
            "error_traceback": record.error_traceback,
            "preflight_intent": record.preflight_intent,
            "preflight_complexity": record.preflight_complexity,
            "worth_remembering": (
                None if record.worth_remembering is None
                else int(record.worth_remembering)
            ),
            "num_steps": record.num_steps,
            "num_tool_calls": record.num_tool_calls,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cached_input_tokens": record.cached_input_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "total_tokens": record.total_tokens,
            "num_api_calls": record.num_api_calls,
            "cost_usd": record.cost_usd,
            "usage_json": record.usage_json,
            "subagent_ledger_json": record.subagent_ledger_json,
            "memory_ledger_json": record.memory_ledger_json,
            "created_at": _utc_now_iso(),
        }
        cols = ", ".join(_RUNS_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _RUNS_COLUMNS)
        # INSERT OR REPLACE so a re-write of the same run_id is idempotent.
        self._conn.execute(
            f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({placeholders})", values
        )
        self._conn.execute("DELETE FROM run_steps WHERE run_id = ?", (record.run_id,))
        for idx, step in enumerate(record.steps):
            self._conn.execute(
                """
                INSERT INTO run_steps (
                    run_id, step_index, step_number, step_type, model_output,
                    reasoning, tool_calls_json, observations, error,
                    is_final_answer, duration_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    idx,
                    step.step_number,
                    step.step_type,
                    step.model_output,
                    step.reasoning,
                    json.dumps(step.tool_calls, default=str) if step.tool_calls else None,
                    step.observations,
                    step.error,
                    int(step.is_final_answer),
                    step.duration_s,
                ),
            )
        self._conn.commit()

    # ---- controller questions --------------------------------------------

    def create_controller_question(self, record: ControllerQuestionRecord) -> bool:
        """Persist a waiting controller question before its notification is sent."""
        if not self.enabled or self._conn is None or self.readonly:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO controller_questions (
                        question_id, agent_name, origin_run_id, origin_mode, team_id,
                        controller_user_id, conversation_id, question, context,
                        options_json, recommendation, proposed_action, status,
                        answer, answer_message_id, resume_run_id, result, error,
                        created_at, answered_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.question_id,
                        record.agent_name,
                        record.origin_run_id,
                        record.origin_mode,
                        record.team_id,
                        record.controller_user_id,
                        record.conversation_id,
                        record.question,
                        record.context,
                        json.dumps(record.options, default=str),
                        record.recommendation,
                        record.proposed_action,
                        record.status,
                        record.answer,
                        record.answer_message_id,
                        record.resume_run_id,
                        record.result,
                        record.error,
                        record.created_at,
                        record.answered_at,
                        record.updated_at,
                    ),
                )
                self._conn.commit()
            return True
        except Exception:
            logger.warning("Failed to persist controller question", exc_info=True)
            return False

    def record_action_gate_observation(
        self, *, run_id: str, tool_name: str, category: str
    ) -> None:
        """Record an observe-mode gate hit without storing tool arguments."""
        if not self.enabled or self._conn is None or self.readonly:
            return
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO action_gate_observations
                        (run_id, tool_name, category, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, tool_name, category, _utc_now_iso()),
                )
                self._conn.commit()
        except Exception:
            logger.warning("Failed to record action-gate observation", exc_info=True)

    # ---- chat turn telemetry ---------------------------------------------

    def record_chat_turn(self, record: ChatTurnRecord) -> None:
        """Persist one chat turn's context accounting. Never raises."""
        if not self.enabled or self._conn is None or self.readonly:
            return
        values = {
            "run_id": record.run_id,
            "conversation_id": record.conversation_id,
            "agent_name": record.agent_name,
            "trigger_turn_id": record.trigger_turn_id,
            "model": record.model,
            "turns_fetched": record.turns_fetched,
            "turns_injected": record.turns_injected,
            "history_steps": record.history_steps,
            "history_covers_conversation": int(record.history_covers_conversation),
            "dropped_oldest_turns": record.dropped_oldest_turns,
            "est_system_tokens": record.est_system_tokens,
            "est_dynamic_tokens": record.est_dynamic_tokens,
            "est_history_tokens": record.est_history_tokens,
            "est_task_tokens": record.est_task_tokens,
            "est_prompt_tokens": record.est_prompt_tokens,
            "input_tokens": record.input_tokens,
            "cached_input_tokens": record.cached_input_tokens,
            "cache_write_tokens": record.cache_write_tokens,
            "output_tokens": record.output_tokens,
            "num_api_calls": record.num_api_calls,
            "cost_usd": record.cost_usd,
            "compacted": int(record.compacted),
            "compaction_reason": record.compaction_reason,
            "duration_s": record.duration_s,
            "created_at": record.created_at,
        }
        cols = ", ".join(_CHAT_TURN_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _CHAT_TURN_COLUMNS)
        try:
            with self._lock:
                self._conn.execute(
                    f"INSERT OR REPLACE INTO chat_turns ({cols}) "
                    f"VALUES ({placeholders})",
                    values,
                )
                self._conn.commit()
        except Exception:
            logger.warning("Failed to record chat turn", exc_info=True)

    def update_chat_turn_usage(self, record: ChatTurnRecord) -> None:
        """Fill provider usage onto an already-inserted chat turn row.

        Usage is only known after the run, while the row is inserted before it
        so cancelled and errored turns are still measured.
        """
        if not self.enabled or self._conn is None or self.readonly:
            return
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE chat_turns SET
                        input_tokens = ?, cached_input_tokens = ?,
                        cache_write_tokens = ?, output_tokens = ?,
                        num_api_calls = ?, cost_usd = ?, duration_s = ?,
                        model = COALESCE(NULLIF(?, ''), model),
                        compacted = ?, compaction_reason = ?
                    WHERE run_id = ?
                    """,
                    (
                        record.input_tokens,
                        record.cached_input_tokens,
                        record.cache_write_tokens,
                        record.output_tokens,
                        record.num_api_calls,
                        record.cost_usd,
                        record.duration_s,
                        record.model,
                        int(record.compacted),
                        record.compaction_reason,
                        record.run_id,
                    ),
                )
                self._conn.commit()
        except Exception:
            logger.warning("Failed to update chat turn usage", exc_info=True)

    def query_chat_turns(
        self,
        *,
        conversation_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> list[dict]:
        """Return chat turn rows for analysis of history/cache behavior."""
        if not self.enabled or self._conn is None:
            return []
        where: list[str] = []
        params: list[Any] = []
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        sql = "SELECT * FROM chat_turns"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at " + ("DESC" if newest_first else "ASC")
        sql += " LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            item["history_covers_conversation"] = bool(
                item.get("history_covers_conversation")
            )
            item["compacted"] = bool(item.get("compacted"))
            input_tokens = item.get("input_tokens") or 0
            item["cache_hit_ratio"] = (
                (item.get("cached_input_tokens") or 0) / input_tokens
                if input_tokens
                else 0.0
            )
            out.append(item)
        return out

    def get_controller_question(self, question_id: str) -> Optional[dict]:
        if not self.enabled or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM controller_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
        return self._decode_controller_question(row)

    def pending_controller_questions(
        self,
        *,
        conversation_id: str,
        controller_user_id: Optional[str] = None,
    ) -> list[dict]:
        if not self.enabled or self._conn is None:
            return []
        sql = (
            "SELECT * FROM controller_questions "
            "WHERE conversation_id = ? AND status = 'waiting'"
        )
        params: list[Any] = [conversation_id]
        if controller_user_id:
            sql += " AND controller_user_id = ?"
            params.append(controller_user_id)
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode_controller_question(row) for row in rows if row]

    def recent_controller_questions(
        self,
        *,
        days: int = 14,
        limit: int = 8,
    ) -> list[dict]:
        """Return recent waiting/settled controller questions, newest first.

        Pure SQL by recency — used for heartbeat standing-decision injection and
        ask-time dedupe. Includes ``waiting`` plus statuses that already have an
        answer (``answered`` / ``resuming`` / ``completed``).
        """
        if not self.enabled or self._conn is None:
            return []
        cutoff = (
            datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(days=max(0, int(days)))
        ).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM controller_questions
                WHERE created_at >= ?
                  AND status IN ('waiting', 'answered', 'resuming', 'completed')
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (cutoff, int(limit)),
            ).fetchall()
        return [self._decode_controller_question(row) for row in rows if row]

    def controller_question_by_answer_message(
        self, answer_message_id: str
    ) -> Optional[dict]:
        if not self.enabled or self._conn is None or not answer_message_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM controller_questions
                WHERE answer_message_id = ?
                ORDER BY answered_at DESC LIMIT 1
                """,
                (answer_message_id,),
            ).fetchone()
        return self._decode_controller_question(row)

    def controller_question_by_reference(
        self,
        reference: str,
        *,
        conversation_id: str,
        controller_user_id: str,
    ) -> Optional[dict]:
        if not self.enabled or self._conn is None or not reference:
            return None
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM controller_questions
                WHERE conversation_id = ? AND controller_user_id = ?
                    AND lower(question_id) LIKE ?
                ORDER BY created_at DESC LIMIT 2
                """,
                (
                    conversation_id,
                    controller_user_id,
                    f"{reference.lower()}%",
                ),
            ).fetchall()
        return self._decode_controller_question(rows[0]) if len(rows) == 1 else None

    def answer_controller_question(
        self,
        question_id: str,
        *,
        answer: str,
        answer_message_id: Optional[str] = None,
    ) -> bool:
        """Atomically answer a waiting question; duplicate webhooks return False."""
        if not self.enabled or self._conn is None or self.readonly:
            return False
        now = _utc_now_iso()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE controller_questions
                SET status = 'answered', answer = ?, answer_message_id = ?,
                    answered_at = ?, updated_at = ?
                WHERE question_id = ? AND status = 'waiting'
                """,
                (answer, answer_message_id, now, now, question_id),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def update_controller_question(
        self,
        question_id: str,
        *,
        status: Optional[str] = None,
        resume_run_id: Optional[str] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        if not self.enabled or self._conn is None or self.readonly:
            return False
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [_utc_now_iso()]
        for column, value in (
            ("status", status),
            ("resume_run_id", resume_run_id),
            ("result", result),
            ("error", error),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)
        params.append(question_id)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE controller_questions SET {', '.join(updates)} "
                "WHERE question_id = ?",
                params,
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def claim_controller_question_for_resume(
        self, question_id: str
    ) -> Optional[dict]:
        """Atomically claim one answered question for a single resume run."""
        if not self.enabled or self._conn is None or self.readonly:
            return None
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE controller_questions
                SET status = 'resuming', updated_at = ?
                WHERE question_id = ? AND status = 'answered'
                """,
                (_utc_now_iso(), question_id),
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM controller_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
        return self._decode_controller_question(row)

    @staticmethod
    def _decode_controller_question(row: sqlite3.Row | None) -> Optional[dict]:
        if row is None:
            return None
        result = dict(row)
        try:
            result["options"] = json.loads(result.pop("options_json") or "[]")
        except (TypeError, ValueError):
            result["options"] = []
            result.pop("options_json", None)
        return result

    # ---- read / query API -------------------------------------------------

    def query_runs(
        self,
        *,
        mode: Optional[str] = None,
        event_type: Optional[str] = None,
        status: Optional[str] = None,
        team_id: Any = _UNSET,
        include_shared_team: bool = True,
        conversation_id: Optional[str] = None,
        since: Optional[str] = None,
        grep: Optional[str] = None,
        tick_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        exclude_run_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return run rows (newest first) matching the given filters.

        ``team_id`` is a sentinel-defaulted filter: omit it for no team filter,
        pass ``None`` for shared/no-team runs only, or pass an id. When an id is
        given and ``include_shared_team`` is True, shared runs are included too.
        """
        if not self.enabled or self._conn is None:
            return []
        where: list[str] = []
        params: list[Any] = []
        if mode:
            where.append("mode = ?")
            params.append(mode)
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if status:
            where.append("status = ?")
            params.append(status)
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        if team_id is not _UNSET:
            if team_id is None:
                where.append("team_id IS NULL")
            elif include_shared_team:
                where.append("(team_id = ? OR team_id IS NULL)")
                params.append(team_id)
            else:
                where.append("team_id = ?")
                params.append(team_id)
        if since:
            where.append("started_at >= ?")
            params.append(since)
        if grep:
            where.append("(task LIKE ? OR result LIKE ?)")
            like = f"%{grep}%"
            params.extend([like, like])
        if tick_id:
            where.append("tick_id = ?")
            params.append(tick_id)
        if parent_run_id:
            where.append("parent_run_id = ?")
            params.append(parent_run_id)
        if exclude_run_id:
            where.append("run_id != ?")
            params.append(exclude_run_id)
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_run(self, run_id: str) -> Optional[dict]:
        if not self.enabled or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_run_steps(self, run_id: str) -> list[dict]:
        if not self.enabled or self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats_by_mode(self, *, since: Optional[str] = None) -> list[dict]:
        """Aggregate run counts, cost, tokens, and failures grouped by mode."""
        if not self.enabled or self._conn is None:
            return []
        sql = (
            "SELECT mode, COUNT(*) AS runs, "
            "SUM(COALESCE(cost_usd, 0)) AS cost_usd, "
            "SUM(COALESCE(total_tokens, 0)) AS total_tokens, "
            "SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failures "
            "FROM runs"
        )
        params: list[Any] = []
        if since:
            sql += " WHERE started_at >= ?"
            params.append(since)
        sql += " GROUP BY mode ORDER BY runs DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
