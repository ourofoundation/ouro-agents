"""Tests for the refinement runner — windowed scoping and applying edits."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from ouro_agents.refinement.queue import (
    ChangeEntry,
    ChangeKind,
    ChangeSetQueue,
)
from ouro_agents.refinement.runner import (
    _apply_window_replacements,
    _parse_llm_response,
    build_doc_view,
    collect_affected_docs,
    run_refinement,
)


_SUBJECT = "01999999-1111-7222-8333-444444444444"
_OTHER = "01988888-aaaa-7bbb-8ccc-dddddddddddd"


# ---------------------------------------------------------------------------
# build_doc_view + collect_affected_docs
# ---------------------------------------------------------------------------


def test_build_doc_view_creates_windows_around_each_match(tmp_path: Path):
    body_lines = [f"line {i}" for i in range(50)]
    body_lines[5] = f"first hit references {_SUBJECT}"
    body_lines[40] = f"second hit references {_SUBJECT}"
    doc = tmp_path / "MEMORY.md"
    doc.write_text("---\nlast_updated: 2026-05-01T00:00:00+00:00\n---\n" + "\n".join(body_lines))

    related = [
        ChangeEntry(kind=ChangeKind.CORRECTION, subject_id=_SUBJECT)
    ]
    view = build_doc_view(doc, related, window_lines=2)

    assert view is not None
    assert view.frontmatter.startswith("---\nlast_updated:")
    # Two non-overlapping windows because hits are 35 lines apart.
    assert len(view.windows) == 2
    first, second = view.windows
    assert first.anchor == 1
    assert second.anchor == 2
    assert _SUBJECT in first.text
    assert _SUBJECT in second.text


def test_build_doc_view_merges_overlapping_windows(tmp_path: Path):
    body = "\n".join(
        [
            "intro",
            f"hit a {_SUBJECT}",
            "between",
            f"hit b {_SUBJECT}",
            "tail",
        ]
    )
    doc = tmp_path / "MEMORY.md"
    doc.write_text(body)

    view = build_doc_view(
        doc,
        [ChangeEntry(kind=ChangeKind.CORRECTION, subject_id=_SUBJECT)],
        window_lines=2,
    )
    assert view is not None
    assert len(view.windows) == 1
    only = view.windows[0]
    assert only.anchor == 1
    assert "hit a" in only.text and "hit b" in only.text


def test_collect_affected_docs_skips_non_markdown(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text(f"see {_SUBJECT}\n")
    (tmp_path / "notes.json").write_text(json.dumps({"id": _SUBJECT}))

    affected = collect_affected_docs(
        tmp_path,
        [ChangeEntry(kind=ChangeKind.CORRECTION, subject_id=_SUBJECT)],
    )
    names = {p.name for p in affected}
    assert "MEMORY.md" in names
    assert "notes.json" not in names


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------


def test_parse_llm_response_strips_code_fences():
    raw = (
        "```json\n"
        '{"window_replacements": [{"anchor": 1, "new_text": "ok"}],'
        ' "memory_deletes": ["m-1"], "summary": "did things"}\n'
        "```"
    )
    replacements, deletes, summary = _parse_llm_response(raw)
    assert replacements == [{"anchor": 1, "new_text": "ok"}]
    assert deletes == ["m-1"]
    assert summary == "did things"


# ---------------------------------------------------------------------------
# _apply_window_replacements
# ---------------------------------------------------------------------------


def test_apply_window_replacements_writes_anchored_edits_in_reverse(tmp_path: Path):
    body_lines = [
        "intro",
        f"first hit {_SUBJECT}",
        "middle",
        f"second hit {_SUBJECT}",
        "tail",
    ]
    doc = tmp_path / "doc.md"
    doc.write_text("\n".join(body_lines) + "\n")

    view = build_doc_view(
        doc,
        [ChangeEntry(kind=ChangeKind.CORRECTION, subject_id=_SUBJECT)],
        window_lines=0,
    )
    assert view is not None
    assert len(view.windows) == 2

    replacements = [
        {"anchor": 1, "new_text": "FIRST_FIXED"},
        {"anchor": 2, "new_text": "SECOND_FIXED"},
    ]
    new_text, applied = _apply_window_replacements(view, replacements)

    assert applied == 2
    assert "FIRST_FIXED" in new_text
    assert "SECOND_FIXED" in new_text
    # Other lines preserved verbatim.
    assert "intro" in new_text
    assert "middle" in new_text
    assert "tail" in new_text
    assert _SUBJECT not in new_text


def test_apply_window_replacements_ignores_unknown_anchor(tmp_path: Path):
    doc = tmp_path / "doc.md"
    doc.write_text(f"only line {_SUBJECT}\n")
    view = build_doc_view(
        doc,
        [ChangeEntry(kind=ChangeKind.CORRECTION, subject_id=_SUBJECT)],
        window_lines=0,
    )
    assert view is not None

    text, applied = _apply_window_replacements(
        view, [{"anchor": 99, "new_text": "noop"}]
    )
    assert applied == 0
    assert text == ""  # signals no edits applied


# ---------------------------------------------------------------------------
# Full run_refinement with stubbed LLM + agent
# ---------------------------------------------------------------------------


class _StubBackend:
    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, memory_id: str) -> None:
        self.deleted.append(memory_id)


def _stub_agent(workspace: Path) -> SimpleNamespace:
    """Build a SimpleNamespace that satisfies the runner's attribute access."""
    return SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(workspace=workspace, name="test-agent"),
            heartbeat=SimpleNamespace(model="cheap-model"),
            refinement=SimpleNamespace(model=None),
        ),
        memory=_StubBackend(),
        soul="test soul",
        _build_model=lambda *a, **kw: None,
    )


def test_run_refinement_applies_anchored_edit_and_marks_queue(tmp_path: Path):
    workspace = tmp_path
    doc = workspace / "MEMORY.md"
    # Pad with enough unrelated lines on either side that they sit outside
    # the ±window_lines slice and are guaranteed to survive verbatim.
    pre = "\n".join(f"pre line {i}" for i in range(10))
    post = "\n".join(f"post line {i}" for i in range(10))
    doc.write_text(
        "---\nlast_updated: 2026-05-01T00:00:00+00:00\n---\n"
        f"{pre}\n"
        f"this learning references {_SUBJECT} and is wrong\n"
        f"{post}\n"
    )

    queue = ChangeSetQueue(workspace / "data" / "change_queue.jsonl")
    entry = ChangeEntry(
        kind=ChangeKind.CORRECTION,
        subject_id=_SUBJECT,
        payload={"note": "drop the wrong learning"},
    )
    queue.enqueue(entry)

    def fake_model(messages, **_kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "window_replacements": [
                        {
                            "anchor": 1,
                            "new_text": "this learning was corrected and no longer references the deleted asset",
                        }
                    ],
                    "memory_deletes": ["mem-123"],
                    "summary": "dropped wrong learning",
                }
            )
        )

    agent = _stub_agent(workspace)
    summary = run_refinement(
        agent=agent,
        queue=queue,
        model=fake_model,
        max_changes_per_pass=10,
        max_docs_per_pass=5,
        window_lines=2,
    )

    assert summary.pending_seen == 1
    assert summary.windows_applied == 1
    assert summary.memory_deletes == 1
    assert summary.queue_marked_applied == 1
    assert agent.memory.deleted == ["mem-123"]

    rewritten = doc.read_text()
    assert _SUBJECT not in rewritten
    # Lines outside the window survive verbatim.
    assert "pre line 0" in rewritten
    assert "post line 9" in rewritten
    # Frontmatter timestamp updated.
    assert "last_updated:" in rewritten
    assert "2026-05-01T00:00:00+00:00" not in rewritten

    # Queue entry was marked applied.
    pending_after = queue.pending()
    assert pending_after == []


def test_run_refinement_marks_queue_when_no_docs_match(tmp_path: Path):
    workspace = tmp_path
    queue = ChangeSetQueue(workspace / "data" / "change_queue.jsonl")
    queue.enqueue(
        ChangeEntry(
            kind=ChangeKind.CORRECTION,
            subject_id="ghost-subject-id-not-in-any-doc",
        )
    )

    agent = _stub_agent(workspace)
    summary = run_refinement(
        agent=agent,
        queue=queue,
        model=lambda *a, **kw: SimpleNamespace(content="{}"),
    )
    assert summary.pending_seen == 1
    assert summary.queue_marked_applied == 1
    assert summary.files_rewritten == []
    assert summary.memory_deletes == 0
