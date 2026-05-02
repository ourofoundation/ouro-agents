"""Tests for the refinement change-set queue."""

from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory

from ouro_agents.refinement.queue import (
    ChangeEntry,
    ChangeKind,
    ChangeSetQueue,
)


def _entry(
    kind: ChangeKind = ChangeKind.CORRECTION,
    subject_id: str = "subject-1",
    occurred_at: str | None = None,
) -> ChangeEntry:
    kwargs = {"kind": kind, "subject_id": subject_id}
    if occurred_at:
        kwargs["occurred_at"] = occurred_at
    return ChangeEntry(**kwargs)


def test_enqueue_persists_and_pending_returns_unapplied():
    with TemporaryDirectory() as tmp:
        queue = ChangeSetQueue(Path(tmp) / "change_queue.jsonl")
        e1 = _entry(occurred_at="2026-05-01T10:00:00+00:00")
        e2 = _entry(
            subject_id="subject-2", occurred_at="2026-05-01T11:00:00+00:00"
        )
        assert queue.enqueue(e1) is True
        assert queue.enqueue(e2) is True

        pending = queue.pending()
        assert [e.subject_id for e in pending] == ["subject-1", "subject-2"]
        assert queue.stats() == {"pending": 2, "oldest": e1.occurred_at, "total": 2}


def test_enqueue_dedupes_pending_by_kind_and_subject():
    with TemporaryDirectory() as tmp:
        queue = ChangeSetQueue(Path(tmp) / "change_queue.jsonl")
        assert queue.enqueue(_entry(subject_id="abc")) is True
        # Different occurred_at, same dedupe key — should be rejected.
        assert (
            queue.enqueue(
                _entry(subject_id="abc", occurred_at="2099-01-01T00:00:00+00:00")
            )
            is False
        )
        assert queue.stats()["pending"] == 1


def test_enqueue_allows_same_subject_after_previous_marked_applied():
    with TemporaryDirectory() as tmp:
        queue = ChangeSetQueue(Path(tmp) / "change_queue.jsonl")
        e1 = _entry(subject_id="abc")
        queue.enqueue(e1)
        queue.mark_applied([e1.id], summary="done")

        # Same dedupe key now permitted because the prior entry is applied.
        e2 = _entry(subject_id="abc")
        assert queue.enqueue(e2) is True
        pending = queue.pending()
        assert [p.id for p in pending] == [e2.id]


def test_mark_applied_records_summary_and_timestamp():
    with TemporaryDirectory() as tmp:
        queue = ChangeSetQueue(Path(tmp) / "change_queue.jsonl")
        e1 = _entry(subject_id="abc")
        e2 = _entry(subject_id="def")
        queue.enqueue(e1)
        queue.enqueue(e2)
        marked = queue.mark_applied([e1.id], summary="rewrote MEMORY.md")
        assert marked == 1

        rows = queue.load()
        applied = [r for r in rows if r.id == e1.id][0]
        unapplied = [r for r in rows if r.id == e2.id][0]
        assert applied.applied_at is not None
        assert applied.applied_summary == "rewrote MEMORY.md"
        assert unapplied.applied_at is None
        assert queue.stats()["pending"] == 1


def test_pending_respects_limit():
    with TemporaryDirectory() as tmp:
        queue = ChangeSetQueue(Path(tmp) / "change_queue.jsonl")
        for i in range(5):
            queue.enqueue(_entry(subject_id=f"s-{i}"))
        assert len(queue.pending(limit=3)) == 3
        assert len(queue.pending()) == 5


def test_load_skips_malformed_jsonl_rows(tmp_path: Path):
    queue_path = tmp_path / "change_queue.jsonl"
    queue_path.write_text(
        "\n".join(
            [
                '{"id":"row-1","kind":"correction","subject_id":"abc",'
                '"occurred_at":"2026-05-01T00:00:00+00:00"}',
                "this is not json",
                '{"missing":"required-fields"}',
            ]
        )
        + "\n"
    )
    queue = ChangeSetQueue(queue_path)
    rows = queue.load()
    assert len(rows) == 1
    assert rows[0].id == "row-1"
