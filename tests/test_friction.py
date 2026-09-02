import json
from pathlib import Path

from ouro_agents.memory.friction import (
    FrictionEntry,
    FrictionQueue,
    FrictionStatus,
)


def test_friction_queue_deduplicates_pending_and_keeps_resolved_audit(tmp_path: Path):
    queue = FrictionQueue.for_workspace(tmp_path)
    entry = FrictionEntry(
        kind="wasted_steps",
        evidence="Repeated the same schema lookup.",
        severity="med",
        run_id="run-1",
        mode="heartbeat",
        team_id="team-1",
    )

    assert queue.path == tmp_path / "protected" / "data" / "friction.jsonl"
    assert queue.enqueue(entry) is True
    assert queue.enqueue(entry.model_copy(update={"id": "duplicate"})) is False
    assert [row.id for row in queue.pending()] == [entry.id]

    assert queue.resolve(
        [entry.id],
        dream_run_id="dream-1",
        disposition="skill_update",
        note="Teach schema reuse.",
    ) == 1
    assert queue.pending() == []

    rows = queue.load()
    assert len(rows) == 1
    assert rows[0].status == FrictionStatus.RESOLVED
    assert rows[0].dream_run_id == "dream-1"
    assert rows[0].disposition == "skill_update"
    assert rows[0].note == "Teach schema reuse."


def test_friction_queue_skips_malformed_jsonl_rows(tmp_path: Path):
    queue = FrictionQueue(tmp_path / "friction.jsonl")
    valid = FrictionEntry(
        kind="tool_failure",
        evidence="Route execution timed out.",
        run_id="run-2",
    )
    queue.path.write_text(
        "{not-json}\n"
        + json.dumps({"kind": "unknown", "evidence": "bad enum"})
        + "\n"
        + valid.model_dump_json()
        + "\n"
    )

    assert queue.load() == [valid]
    assert queue.pending(limit=1) == [valid]
