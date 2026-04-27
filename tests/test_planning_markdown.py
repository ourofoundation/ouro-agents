"""Tests for plan task items and review prompts."""

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
    run_planning_heartbeat,
    run_review_heartbeat,
)


def test_feedback_review_prompt_targets_same_thread_reply():
    prompt = build_feedback_review_prompt(
        quest_id="plan-quest-1",
        plan_text="# Plan",
        current_items_section="[ ] Do first thing (item_id: item-1)",
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
        quest_id="plan-quest-1",
        plan_text="# Plan",
        current_items_section="[ ] Do first thing (item_id: item-1)",
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
        quest_id="plan-quest-1",
        plan_text="# Plan",
        current_items_section="[ ] Keep body (item_id: item-1)",
        feedback_text="Please deactivate this plan.",
        current_status="active",
    )

    assert '"next_status": "active|pending_review|cancelled"' in prompt
    assert 'set "next_status": "cancelled"' in prompt


def test_feedback_review_prompt_uses_structured_item_numbering_and_sort_order():
    prompt = build_feedback_review_prompt(
        quest_id="plan-quest-1",
        plan_text="# Plan",
        current_items_section="\n".join(
            [
                "[ ] First task (item_id: item-1)",
                "[ ] Explore XRD route status (item_id: item-2)",
            ]
        ),
        feedback_text="Please remove item 2.",
        current_status="active",
    )

    assert "frontend numbering is 1-indexed" in prompt
    assert "1. [ ] First task (item_id: item-1)" in prompt
    assert "2. [ ] Explore XRD route status (item_id: item-2)" in prompt
    assert "Do NOT infer item numbers from prose headings" in prompt
    assert "update_quest_item(quest_id, item_id, ...): change description, notes, status," in prompt
    assert "or sort_order" in prompt
    assert "normalize sort_order to match the frontend's 1-indexed numbering" in prompt


def test_next_action_keeps_executing_active_incomplete_plan_after_cadence():
    current = PlanCycle(
        id="cycle-1",
        status="active",
        kind="default",
        created_at="2026-04-01T09:00:00+00:00",
        activated_at="2026-04-01T09:00:00+00:00",
        heartbeats_completed=6,
        quest_id="quest-1",
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


def test_next_action_replans_stale_active_without_quest():
    """Active plans without quest-backed task items should be replaced."""
    current = PlanCycle(
        id="cycle-stale",
        status="active",
        kind="default",
        created_at="2026-04-08T15:00:00+00:00",
        activated_at="2026-04-08T16:00:00+00:00",
        heartbeats_completed=40,
        items=[],
        quest_id=None,
        plan_text="corrupted tool output",
    )

    assert current.needs_replan_stale_active is True

    action = next_action(
        current=current,
        cadence="4h",
        min_heartbeats=4,
        review_window="1h",
        auto_approve=True,
    )

    assert action == "plan"


def test_legacy_post_id_does_not_become_plan_quest_id():
    current = PlanCycle(
        id="cycle-post-backed",
        status="active",
        kind="default",
        created_at="2026-04-08T15:00:00+00:00",
        activated_at="2026-04-08T16:00:00+00:00",
        items=[PlanItem(description="Legacy local item")],
        post_id="legacy-plan-post",
    )

    assert current.quest_id is None
    assert current.needs_replan_stale_active is True


def test_next_action_active_with_quest_but_no_local_items_still_executes():
    current = PlanCycle(
        id="cycle-quest",
        status="active",
        kind="default",
        created_at="2026-04-01T09:00:00+00:00",
        activated_at="2026-04-01T09:00:00+00:00",
        heartbeats_completed=2,
        items=[],
        quest_id="01900000-0000-7000-8000-000000000001",
    )

    assert current.needs_replan_stale_active is False

    action = next_action(
        current=current,
        cadence="4h",
        min_heartbeats=4,
        review_window="1h",
        auto_approve=True,
    )

    assert action == "execute"


def test_run_review_heartbeat_cancels_without_rewriting_plan():
    class FakeQuests:
        def __init__(self):
            self.updates = []

        def update(self, quest_id, **kwargs):
            self.updates.append((quest_id, kwargs))

    class FakeOuroClient:
        def __init__(self):
            self.quests = FakeQuests()

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
                quest_id="plan-quest-1",
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
            assert ouro_client.quests.updates
            _, kwargs = ouro_client.quests.updates[-1]
            assert kwargs["status"] == "closed"

    asyncio.run(_exercise())


def test_run_planning_heartbeat_mentions_controller_when_plan_needs_review():
    class FakeContent:
        def __init__(self):
            self.markdown = None

        def from_markdown(self, markdown: str) -> None:
            self.markdown = markdown

    class FakeComments:
        def __init__(self):
            self.created = []

        def create(self, *, content, parent_id):
            self.created.append((parent_id, content.markdown))

    class FakeQuests:
        @staticmethod
        def Content():
            return FakeContent()

        def list_items(self, quest_id):
            assert quest_id == "plan-quest-1"
            return []

    class FakeOuroClient:
        def __init__(self):
            self.comments = FakeComments()
            self.quests = FakeQuests()

    class FakeAgent:
        def __init__(self, workspace: Path, ouro_client: FakeOuroClient):
            self.config = SimpleNamespace(
                agent=SimpleNamespace(
                    workspace=workspace,
                    name="hermes",
                    org_id="org-1",
                ),
                planning=SimpleNamespace(
                    cadence="4h",
                    model=None,
                ),
                controller=SimpleNamespace(username="@reviewer"),
            )
            self.doc_store = None
            self._ouro_client = ouro_client

        async def run(self, *args, **kwargs):
            return json.dumps({"quest_id": "plan-quest-1", "plan": "# New Plan"})

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _get_ouro_client(self):
            return self._ouro_client

    async def _exercise():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            plan_store = PlanStore(workspace / "plans")
            ouro_client = FakeOuroClient()
            agent = FakeAgent(workspace, ouro_client)

            await run_planning_heartbeat(
                agent,
                hb_model=SimpleNamespace(model_id="heartbeat-model"),
                plan_store=plan_store,
                servers=["ouro"],
            )

            current = plan_store.load_default()
            assert current is not None
            assert current.status == "pending_review"
            assert ouro_client.comments.created == [
                ("plan-quest-1", "{@reviewer} this quest is ready for review.")
            ]

    asyncio.run(_exercise())
