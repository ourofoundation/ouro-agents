"""Tests for plan task items and review prompts."""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ouro_agents.memory.focus import build_focus_memory_context
from ouro_agents.modes.planning import (
    PlanCycle,
    PlanStore,
    PlanItem,
    build_feedback_review_prompt,
    build_planning_prompt,
    next_action,
    reconcile_plan_with_remote_quest,
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


def test_build_planning_prompt_uses_natural_goal_quest_name():
    prompt = build_planning_prompt(
        cadence="1d",
        agent_name="hermes",
        goal="Explore XRD route status.",
    )

    assert "Name it with a concise, natural title that is just the goal" in prompt
    assert "Explore XRD route status." in prompt
    assert "planning quest" not in prompt
    assert "PLAN:hermes" not in prompt


def test_build_focus_memory_context_filters_and_formats_guidance():
    class FakeMemory:
        def search(self, **kwargs):
            assert kwargs["team_id"] == "team-1"
            return [
                SimpleNamespace(
                    text="Focus on benchmarking model quality before adding more routes.",
                    category="direction",
                    score=0.1,
                    strength=0.9,
                    basis="stated",
                    created_at="2026-04-01T00:00:00+00:00",
                ),
                SimpleNamespace(
                    text="The weather was nice.",
                    category="episode",
                    score=0.9,
                    strength=0.5,
                    created_at="2026-04-01T00:00:00+00:00",
                ),
                SimpleNamespace(
                    text="Focus on the 2:17 RE-Fe/Co structure family because a CIF was uploaded.",
                    category="direction",
                    subject_type="asset",
                    score=0.99,
                    strength=0.95,
                    basis="observed",
                    asset_ids=["asset-cif"],
                    source="team-feed",
                    created_at="2026-04-01T00:00:00+00:00",
                ),
            ]

    context = build_focus_memory_context(
        FakeMemory(),
        "hermes",
        team_id="team-1",
        heading="Work Direction Guidance",
    )

    assert "### Work Direction Guidance" in context
    assert "Focus on benchmarking model quality" in context
    assert "The weather was nice" not in context
    assert "2:17 RE-Fe/Co" not in context


def test_build_planning_prompt_includes_heartbeat_budget():
    prompt = build_planning_prompt(cadence="4h", heartbeat_every="30m")

    assert "roughly 8 heartbeat work sessions" in prompt
    assert "each sized to one session" in prompt


def test_build_planning_prompt_includes_item_quality_bar():
    prompt = build_planning_prompt(cadence="1d")

    assert "concrete deliverable or observable outcome" in prompt
    assert "sized to roughly one heartbeat work session" in prompt


def test_fresh_plan_carries_over_previous_cycle_outcome():
    previous = PlanCycle(
        id="cycle-prev",
        status="completed",
        plan_text="# Old Plan",
        heartbeats_completed=6,
        items=[
            PlanItem(description="Shipped thing", status="done"),
            PlanItem(description="Unfinished thread", status="pending"),
        ],
    )

    prompt = build_planning_prompt(cadence="1d", previous_plan=previous)

    assert "## Previous Plan Outcome" in prompt
    assert "1/2 items completed over 6 heartbeats" in prompt
    assert "Unfinished thread" in prompt
    assert "adopt it into the new plan" in prompt
    assert "retrospective" in prompt


def test_build_recent_activity_context_digests_run_log():
    from ouro_agents.modes.planning import build_recent_activity_context

    class FakeRunLog:
        def query_runs(self, **kwargs):
            assert kwargs["team_id"] == "team-1"
            return [
                {
                    "mode": "heartbeat",
                    "status": "success",
                    "started_at": "2026-07-05T12:00:00+00:00",
                    "task": "Work the plan",
                    "result": "Completed quest item and posted results.",
                },
                {
                    "mode": "plan",
                    "status": "success",
                    "started_at": "2026-07-05T08:00:00+00:00",
                    "task": "Planning run should be excluded",
                    "result": "{}",
                },
            ]

    agent = SimpleNamespace(_run_log=FakeRunLog())
    context = build_recent_activity_context(agent, "team-1")

    assert "## Recent Activity" in context
    assert "heartbeat (success): Work the plan" in context
    assert "Completed quest item" in context
    assert "should be excluded" not in context


def test_build_planning_prompt_includes_direction_extra_context():
    prompt = build_planning_prompt(
        cadence="1d",
        extra_context=(
            "### Work Direction Guidance\n"
            "- [direction] Focus on dataset quality before new posts."
        ),
    )

    assert "## Additional Context" in prompt
    assert "Work Direction Guidance" in prompt
    assert "Focus on dataset quality" in prompt
    assert "use it to choose focus" in prompt
    assert "Recent platform\nactivity alone is not a reason to prioritize a topic" in prompt


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


def test_plan_item_is_waiting_by_date_and_reason():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    future = (now + timedelta(days=7)).isoformat()
    past = (now - timedelta(days=1)).isoformat()

    # Future date → waiting; past date → actionable again.
    assert PlanItem(description="x", status="in_progress", waiting_until=future).is_waiting(now)
    assert not PlanItem(description="x", status="in_progress", waiting_until=past).is_waiting(now)
    # waiting_on with no date parks indefinitely until cleared.
    assert PlanItem(description="x", status="in_progress", waiting_on="a reply").is_waiting(now)
    # Finished items are never "waiting".
    assert not PlanItem(description="x", status="done", waiting_on="a reply").is_waiting(now)
    # No deferral metadata → not waiting.
    assert not PlanItem(description="x", status="in_progress").is_waiting(now)


def test_recurring_waiting_item_is_due_without_next_time():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    # Recurring check with a future next-time → parked.
    parked = PlanItem(
        description="Scan for replies",
        status="in_progress",
        waiting_on="reply from authors",
        waiting_until=(now + timedelta(hours=6)).isoformat(),
        waiting_check_every="1d",
    )
    assert parked.is_waiting(now)
    # Recurring check with no next-time set → due now (not waiting).
    due = PlanItem(
        description="Scan for replies",
        status="in_progress",
        waiting_on="reply from authors",
        waiting_check_every="1d",
    )
    assert not due.is_waiting(now)


def test_next_action_replans_when_only_waiting_items_remain():
    """A plan whose sole remaining item is parked should start a new cycle."""
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    current = PlanCycle(
        id="cycle-waiting",
        status="active",
        kind="default",
        created_at="2026-06-23T09:00:00+00:00",
        activated_at="2026-06-23T09:00:00+00:00",
        heartbeats_completed=8,
        quest_id="quest-waiting",
        items=[
            PlanItem(id="done-1", description="Shipped", status="done"),
            PlanItem(
                id="wait-1",
                description="Follow up if no reply",
                status="in_progress",
                waiting_on="reply from authors",
                waiting_until=(now + timedelta(days=7)).isoformat(),
            ),
        ],
    )

    assert current.all_items_complete is False
    assert current.has_actionable_items(now) is False

    action = next_action(
        current=current,
        cadence="4h",
        min_heartbeats=4,
        review_window="1h",
        auto_approve=True,
        now=now,
    )

    assert action == "plan"


def test_next_action_executes_when_a_waiting_item_comes_due():
    """Once waiting_until passes, the item is actionable again — keep executing."""
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    current = PlanCycle(
        id="cycle-due",
        status="active",
        kind="default",
        created_at="2026-06-23T09:00:00+00:00",
        activated_at="2026-06-23T09:00:00+00:00",
        heartbeats_completed=8,
        quest_id="quest-due",
        items=[
            PlanItem(id="done-1", description="Shipped", status="done"),
            PlanItem(
                id="wait-1",
                description="Follow up if no reply",
                status="in_progress",
                waiting_on="reply from authors",
                waiting_until="2026-07-07T14:00:00+00:00",
            ),
        ],
    )

    assert current.has_actionable_items(now) is True

    action = next_action(
        current=current,
        cadence="4h",
        min_heartbeats=4,
        review_window="1h",
        auto_approve=True,
        now=now,
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


def test_load_all_active_archives_deleted_quest_husks(tmp_path):
    plan_store = PlanStore(tmp_path / "plans")
    plan_store.save(
        PlanCycle(id="cycle-good", status="active", kind="default", quest_id="q-1")
    )
    husk = tmp_path / "plans" / "active" / "goal-husk.json"
    husk.write_text(
        json.dumps({"id": "husk-1", "status": "active", "quest_id": "[deleted]"})
    )

    plans = plan_store.load_all_active()

    assert [p.id for p in plans] == ["cycle-good"]
    assert not husk.exists()
    assert (tmp_path / "plans" / "history" / "goal-husk.json").exists()


def test_reconcile_plan_with_remote_closed_quest_archives_without_update(tmp_path):
    plan_store = PlanStore(tmp_path / "plans")
    current = PlanCycle(
        id="cycle-closed",
        status="active",
        kind="default",
        quest_id="quest-closed",
        items=[PlanItem(id="local", description="Stale local item", status="pending")],
    )
    plan_store.save(current)

    class FakeQuests:
        def __init__(self):
            self.updates = []

        def retrieve(self, quest_id):
            assert quest_id == "quest-closed"
            return SimpleNamespace(
                quest=SimpleNamespace(status="closed"),
                items=[
                    SimpleNamespace(
                        id="remote",
                        description="Remote completed item",
                        status="done",
                        notes="Finished on Ouro",
                    )
                ],
            )

        def update(self, quest_id, **kwargs):
            self.updates.append((quest_id, kwargs))

    ouro_client = SimpleNamespace(quests=FakeQuests())

    result = reconcile_plan_with_remote_quest(plan_store, current, ouro_client)

    assert result is None
    assert plan_store.load_default() is None
    archived = plan_store.load_history(limit=1)[0]
    assert archived.status == "completed"
    assert archived.items[0].id == "remote"
    assert ouro_client.quests.updates == []


def test_reconcile_plan_with_remote_open_quest_refreshes_items(tmp_path):
    plan_store = PlanStore(tmp_path / "plans")
    current = PlanCycle(
        id="cycle-open",
        status="active",
        kind="default",
        quest_id="quest-open",
        items=[PlanItem(id="local", description="Stale local item", status="pending")],
    )
    plan_store.save(current)

    class FakeQuests:
        def retrieve(self, quest_id):
            assert quest_id == "quest-open"
            return SimpleNamespace(
                quest=SimpleNamespace(status="open"),
                items=[
                    SimpleNamespace(
                        id="remote",
                        description="Remote live item",
                        status="in_progress",
                        notes="Started on Ouro",
                    )
                ],
            )

    result = reconcile_plan_with_remote_quest(
        plan_store,
        current,
        SimpleNamespace(quests=FakeQuests()),
    )

    assert result is not None
    loaded = plan_store.load_default()
    assert loaded is not None
    assert loaded.status == "active"
    assert loaded.items[0].id == "remote"
    assert loaded.items[0].description == "Remote live item"
    assert loaded.items[0].status == "in_progress"


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
                security=SimpleNamespace(controller_username="reviewer"),
            )
            self.doc_store = None
            self._ouro_client = ouro_client

        async def run(self, *args, **kwargs):
            return json.dumps({"quest_id": "plan-quest-1", "plan": "# New Plan"})

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _get_ouro_client(self):
            return self._ouro_client

        def doc_store_for(self, _team_id):
            return None

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
                ("plan-quest-1", "`{@reviewer}` this quest is ready for review.")
            ]

    asyncio.run(_exercise())


def test_run_planning_heartbeat_injects_recalled_direction_context():
    captured = {}

    class FakeMemory:
        def search(self, **_kwargs):
            return [
                SimpleNamespace(
                    text="Focus next planning on route reliability before new demos.",
                    category="direction",
                    score=0.2,
                    strength=0.9,
                    basis="stated",
                    created_at="2026-04-01T00:00:00+00:00",
                )
            ]

    class FakeQuests:
        def list_items(self, quest_id):
            assert quest_id == "plan-quest-1"
            return []

    class FakeOuroClient:
        def __init__(self):
            self.quests = FakeQuests()

    class FakeAgent:
        def __init__(self, workspace: Path):
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
                security=SimpleNamespace(controller_username=None),
            )
            self.doc_store = None
            self.memory = FakeMemory()
            self._ouro_client = FakeOuroClient()

        def doc_store_for(self, _team_id):
            return None

        async def run(self, prompt, **_kwargs):
            captured["prompt"] = prompt
            return json.dumps({"quest_id": "plan-quest-1", "plan": "# New Plan"})

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _get_ouro_client(self):
            return self._ouro_client

    async def _exercise():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            plan_store = PlanStore(
                workspace / "teams" / "team-1" / "plans",
                team_id="team-1",
            )
            agent = FakeAgent(workspace)

            await run_planning_heartbeat(
                agent,
                hb_model=SimpleNamespace(model_id="heartbeat-model"),
                plan_store=plan_store,
                servers=["ouro"],
            )

            assert "## Additional Context" in captured["prompt"]
            assert "### Work Direction Guidance" in captured["prompt"]
            assert "route reliability before new demos" in captured["prompt"]

    asyncio.run(_exercise())


def test_run_review_heartbeat_stores_directional_feedback_memory():
    class FakeMemory:
        def __init__(self):
            self.added = []

        def add(self, text, **kwargs):
            self.added.append((text, kwargs))

    class FakeQuests:
        def __init__(self):
            self.updates = []

        def update(self, quest_id, **kwargs):
            self.updates.append((quest_id, kwargs))

        def list_items(self, _quest_id):
            return []

    class FakeOuroClient:
        def __init__(self):
            self.quests = FakeQuests()

    class FakeAgent:
        def __init__(self, workspace: Path):
            self.config = SimpleNamespace(
                agent=SimpleNamespace(workspace=workspace, name="hermes"),
                planning=SimpleNamespace(model=None),
            )
            self.doc_store = None
            self.memory = FakeMemory()
            self._ouro_client = FakeOuroClient()

        async def run(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "revised_plan": "# Revised Plan",
                    "feedback_summary": "User asked the agent to focus on evaluation quality before publishing more demos.",
                    "next_status": "pending_review",
                }
            )

        def _get_ouro_client(self):
            return self._ouro_client

        def doc_store_for(self, _team_id):
            return None

    async def _exercise():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            plan_store = PlanStore(
                workspace / "teams" / "team-1" / "plans",
                team_id="team-1",
            )
            current = PlanCycle(
                id="cycle-1",
                status="pending_review",
                kind="default",
                team_id="team-1",
                plan_text="# Original Plan",
                quest_id="plan-quest-1",
            )
            plan_store.save(current)
            agent = FakeAgent(workspace)

            await run_review_heartbeat(
                agent,
                hb_model=None,
                plan_store=plan_store,
                current=current,
                servers=["ouro"],
                inline_feedback="Please focus on evaluation quality.",
            )

            assert agent.memory.added
            text, kwargs = agent.memory.added[0]
            assert "Planning guidance from review feedback" in text
            assert kwargs["metadata"]["category"] == "direction"
            assert kwargs["metadata"]["asset_ids"] == "plan-quest-1"
            assert kwargs["team_id"] == "team-1"

    asyncio.run(_exercise())
