"""Tests for plan markdown task parsing, item sync, and review prompts."""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ouro_agents.modes.planning import (
    PlanCycle,
    PlanStore,
    PlanItem,
    build_feedback_review_prompt,
    next_action,
    parse_task_lines_from_markdown,
    rebuild_plan_markdown,
    run_review_heartbeat,
    sync_plan_items_from_markdown,
)


def test_parse_task_lines_basic():
    md = """## Tasks
- [ ] First task
- [x] Done task
* [ ] Star bullet
"""
    rows = parse_task_lines_from_markdown(md)
    assert rows == [
        ("First task", False, ""),
        ("Done task", True, ""),
        ("Star bullet", False, ""),
    ]


def test_parse_task_lines_with_notes_and_id():
    md = "- [ ] (a1b2c3d4) Do thing — some note\n"
    rows = parse_task_lines_from_markdown(md)
    assert len(rows) == 1
    desc, done, notes = rows[0]
    assert desc == "Do thing"
    assert done is False
    assert notes == "some note"


def test_sync_drops_removed_tasks_and_adds_new():
    existing = [
        PlanItem(id="aaaaaaaa", description="Old Iran task", status="pending"),
        PlanItem(id="bbbbbbbb", description="Keep me", status="in_progress"),
    ]
    md = "## Tasks\n- [ ] Keep me\n- [ ] Brand new item\n"
    out = sync_plan_items_from_markdown(md, existing)
    assert len(out) == 2
    assert out[0].id == "bbbbbbbb"
    assert out[0].status == "in_progress"
    assert out[1].description == "Brand new item"
    assert out[1].status == "pending"
    assert len(out[1].id) == 8


def test_sync_marks_done_from_checkbox():
    existing = [PlanItem(id="cccccccc", description="Ship it", status="pending")]
    md = "- [x] Ship it\n"
    out = sync_plan_items_from_markdown(md, existing)
    assert out[0].status == "done"


def test_rebuild_no_longer_appends_stale_items_after_sync():
    plan_md = """## Tasks
- [ ] One
- [ ] Two
"""
    items = sync_plan_items_from_markdown(plan_md, [])
    rebuilt = rebuild_plan_markdown(plan_md, items)
    assert "One" in rebuilt
    assert "Two" in rebuilt
    assert rebuilt.count("One") == 1


def test_feedback_review_prompt_targets_same_thread_reply():
    prompt = build_feedback_review_prompt(
        post_id="plan-post-1",
        plan_text="# Plan",
        feedback_text="Please narrow scope.",
        current_status="pending_review",
        reply_parent_id="comment-123",
        thread_parent_id="thread-456",
    )

    assert "parent_id `comment-123`" in prompt
    assert "get_comments(parent_id=`thread-456`)" in prompt
    assert "Please narrow scope." in prompt


def test_feedback_review_prompt_for_active_plan_preserves_active_state():
    prompt = build_feedback_review_prompt(
        post_id="plan-post-1",
        plan_text="# Plan",
        feedback_text="Tighten task 2 and keep going.",
        current_status="active",
        reply_parent_id="comment-123",
    )

    assert "Current plan status: active" in prompt
    assert "already active" in prompt
    assert 'set "next_status": "active"' in prompt
    assert "waiting for initial approval" in prompt


def test_feedback_review_prompt_mentions_next_status_for_cancellation():
    prompt = build_feedback_review_prompt(
        post_id="plan-post-1",
        plan_text="# Plan",
        feedback_text="Please deactivate this plan.",
        current_status="active",
    )

    assert '"next_status": "active|pending_review|cancelled"' in prompt
    assert 'set "next_status": "cancelled"' in prompt


def test_next_action_keeps_executing_active_incomplete_plan_after_cadence():
    current = PlanCycle(
        id="cycle-1",
        status="active",
        kind="default",
        created_at="2026-04-01T09:00:00+00:00",
        activated_at="2026-04-01T09:00:00+00:00",
        heartbeats_completed=6,
        items=[PlanItem(id="task-123", description="Keep going", status="in_progress")],
    )

    action = next_action(
        current=current,
        cadence="4h",
        min_heartbeats=4,
        review_window="1h",
        auto_approve=True,
        now=datetime.fromisoformat("2026-04-01T18:00:00+00:00"),
    )

    assert action == "execute"


def test_run_review_heartbeat_cancels_without_rewriting_plan():
    class FakePosts:
        def __init__(self):
            self.updates = []

        def update(self, post_id, **kwargs):
            self.updates.append((post_id, kwargs))

    class FakeOuroClient:
        def __init__(self):
            self.posts = FakePosts()

    class FakeAgent:
        def __init__(self, workspace: Path, ouro_client: FakeOuroClient):
            self.config = SimpleNamespace(
                agent=SimpleNamespace(workspace=workspace, name="hermes"),
                planning=SimpleNamespace(model=None),
            )
            self.doc_store = None
            self._ouro_client = ouro_client

        async def run(self, *args, **kwargs):
            return json.dumps(
                {
                    "revised_plan": "# Rewritten Plan\n\nThis should be ignored.",
                    "feedback_summary": "User asked to deactivate the plan.",
                    "next_status": "cancelled",
                }
            )

        def _get_ouro_client(self):
            return self._ouro_client

    async def _exercise():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            plan_store = PlanStore(workspace / "plans")
            current = PlanCycle(
                id="cycle-1",
                status="active",
                kind="default",
                plan_text="# Original Plan\n\n## Tasks\n- [ ] Keep body",
                items=[PlanItem(id="keepbody", description="Keep body", status="pending")],
                post_id="plan-post-1",
            )
            plan_store.save(current)
            ouro_client = FakeOuroClient()
            agent = FakeAgent(workspace, ouro_client)

            archived = await run_review_heartbeat(
                agent,
                hb_model=None,
                plan_store=plan_store,
                current=current,
                servers=["ouro"],
                inline_feedback="Please deactivate this plan.",
            )

            assert archived is not None
            assert archived.status == "cancelled"
            assert archived.plan_text == "# Original Plan\n\n## Tasks\n- [ ] Keep body"
            assert not (workspace / "plans" / "active" / "default.json").exists()
            assert ouro_client.posts.updates
            _, kwargs = ouro_client.posts.updates[-1]
            assert kwargs["description"] == "[cancelled]"
            assert "content" not in kwargs

    asyncio.run(_exercise())
