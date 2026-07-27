"""Tests for planning cursors, quest helpers, and planning prompts."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ouro_agents.memory.focus import build_focus_memory_context
from ouro_agents.modes.planning import (
    PlanningCursor,
    auto_approve_due_drafts,
    build_planning_prompt,
    build_previous_quest_context,
    item_is_waiting,
    load_cursor,
    planning_due,
    render_quest_items,
    run_planning_run,
    save_cursor,
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_build_planning_prompt_uses_natural_goal_quest_name():
    prompt = build_planning_prompt(
        cadence="1d",
        goal="Explore XRD route status.",
    )

    assert "Name it with a concise, natural title that is just the goal" in prompt
    assert "Explore XRD route status." in prompt
    assert "planning quest" not in prompt
    assert "PLAN:hermes" not in prompt


def test_build_planning_prompt_asks_for_draft_status():
    prompt = build_planning_prompt(cadence="1d")

    assert 'status="draft"' in prompt
    assert "create_quest" in prompt
    assert "create_quest_items" in prompt
    assert "update_quest" in prompt
    assert "Call create_quest exactly once" in prompt
    assert "never publish a second plan quest" in prompt
    assert "Call create_quest alone" in prompt
    assert "create_quest_items / update_quest" in prompt


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
    assert "choosing focus" in prompt
    assert "Recent platform\nactivity alone is not a reason to prioritize a topic" in prompt or (
        "Recent platform activity alone is not a reason to prioritize a topic" in prompt
    )


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


def test_build_previous_quest_context_summarizes_outcome():
    class FakeQuests:
        def retrieve(self, quest_id):
            assert quest_id == "quest-prev"
            return SimpleNamespace(
                id="quest-prev",
                name="Old Plan",
                description="Background prose.",
                quest=SimpleNamespace(status="open"),
                items=[
                    SimpleNamespace(id="i1", description="Shipped thing", status="done"),
                    SimpleNamespace(
                        id="i2", description="Unfinished thread", status="pending"
                    ),
                ],
            )

    context = build_previous_quest_context(
        SimpleNamespace(quests=FakeQuests()), "quest-prev"
    )

    assert "## Previous Plan Outcome" in context
    assert "1/2 items resolved" in context
    assert "Unfinished thread" in context
    assert "Do NOT copy them into this plan" in context


# ---------------------------------------------------------------------------
# Waiting semantics
# ---------------------------------------------------------------------------


def test_item_is_waiting_by_date_and_reason():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    future = (now + timedelta(days=7)).isoformat()
    past = (now - timedelta(days=1)).isoformat()

    # Future date → waiting; past date → actionable again.
    assert item_is_waiting(
        {"status": "in_progress", "waiting_until": future}, now
    )
    assert not item_is_waiting(
        {"status": "in_progress", "waiting_until": past}, now
    )
    # waiting_on with no date parks indefinitely until cleared.
    assert item_is_waiting({"status": "in_progress", "waiting_on": "a reply"}, now)
    # Finished items are never "waiting".
    assert not item_is_waiting({"status": "done", "waiting_on": "a reply"}, now)
    # No deferral metadata → not waiting.
    assert not item_is_waiting({"status": "in_progress"}, now)


def test_recurring_waiting_item_is_due_without_next_time():
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    # Recurring check with a future next-time → parked.
    parked = {
        "status": "in_progress",
        "waiting_on": "reply from authors",
        "waiting_until": (now + timedelta(hours=6)).isoformat(),
        "waiting_check_every": "1d",
    }
    assert item_is_waiting(parked, now)
    # Recurring check with no next-time set → due now (not waiting).
    due = {
        "status": "in_progress",
        "waiting_on": "reply from authors",
        "waiting_check_every": "1d",
    }
    assert not item_is_waiting(due, now)


def test_render_quest_items_marks_waiting_and_progress():
    now = datetime.now(timezone.utc)
    rendered = render_quest_items(
        [
            {"id": "a", "description": "Done thing", "status": "done"},
            {
                "id": "b",
                "description": "Parked thing",
                "status": "in_progress",
                "waiting_on": "a reply",
                "waiting_until": (now + timedelta(days=2)).isoformat(),
            },
            {"id": "c", "description": "Live thing", "status": "in_progress"},
        ]
    )

    assert "[x] Done thing (item_id: a)" in rendered
    assert "waiting on a reply until" in rendered
    assert "[ ] Live thing (item_id: c) [in_progress]" in rendered


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def test_cursor_roundtrip_and_planning_due(tmp_path):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    # No cursor on disk → planning is due.
    assert planning_due(load_cursor(tmp_path, "team-1"), "1d", now)

    save_cursor(
        tmp_path,
        "team-1",
        PlanningCursor(
            last_planned_at=(now - timedelta(hours=2)).isoformat(),
            last_quest_id="quest-1",
            pending_quest_ids=["quest-1"],
        ),
    )
    cursor = load_cursor(tmp_path, "team-1")
    assert cursor.last_quest_id == "quest-1"
    assert cursor.pending_quest_ids == ["quest-1"]
    assert not planning_due(cursor, "1d", now)
    assert planning_due(cursor, "1h", now)


# ---------------------------------------------------------------------------
# Planning run
# ---------------------------------------------------------------------------


def _fake_agent(workspace, ouro_client, run_result, controller="reviewer"):
    class FakeAgent:
        def __init__(self):
            self.config = SimpleNamespace(
                agent=SimpleNamespace(
                    workspace=workspace, name="hermes", org_id="org-1"
                ),
                planning=SimpleNamespace(
                    cadence="4h",
                    model=None,
                    review_window="1h",
                    auto_approve=True,
                ),
                heartbeat=SimpleNamespace(every="30m"),
                security=SimpleNamespace(controller_username=controller),
            )
            self.doc_store = None
            self._ouro_client = ouro_client

        async def run(self, prompt, **kwargs):
            self.prompt = prompt
            return run_result

        def _build_model(self, model_id, heartbeat=False, **kwargs):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _get_ouro_client(self):
            return self._ouro_client

        def doc_store_for(self, _team_id):
            return None

    return FakeAgent()


class _FakeContent:
    def __init__(self):
        self.markdown = None

    def from_markdown(self, markdown: str) -> None:
        self.markdown = markdown


class _FakeComments:
    def __init__(self):
        self.created = []

    def create(self, *, content, parent_id):
        self.created.append((parent_id, content.markdown))


def test_run_planning_run_records_cursor_and_notifies_controller(tmp_path):
    class FakeQuests:
        @staticmethod
        def Content():
            return _FakeContent()

    ouro = SimpleNamespace(comments=_FakeComments(), quests=FakeQuests())
    agent = _fake_agent(
        tmp_path, ouro, json.dumps({"quest_id": "plan-quest-1"})
    )
    captured = {}

    async def _run(prompt, **kwargs):
        captured["kwargs"] = kwargs
        agent.prompt = prompt
        return json.dumps({"quest_id": "plan-quest-1"})

    agent.run = _run

    result = asyncio.run(
        run_planning_run(
            agent,
            hb_model=SimpleNamespace(model_id="heartbeat-model"),
            team_id="team-1",
            servers=["ouro"],
        )
    )

    assert result is not None
    assert "ouro:create_quest" in captured["kwargs"]["preload_tools"]
    assert "ouro:create_quest_items" in captured["kwargs"]["preload_tools"]
    assert "ouro:update_quest" in captured["kwargs"]["preload_tools"]
    cursor = load_cursor(tmp_path, "team-1")
    assert cursor.last_quest_id == "plan-quest-1"
    assert cursor.pending_quest_ids == ["plan-quest-1"]
    assert cursor.last_planned_at
    assert ouro.comments.created == [
        ("plan-quest-1", "`{@reviewer}` this quest is ready for review.")
    ]


def test_run_planning_run_injects_recalled_direction_context(tmp_path):
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
        @staticmethod
        def Content():
            return _FakeContent()

    ouro = SimpleNamespace(comments=_FakeComments(), quests=FakeQuests())
    agent = _fake_agent(
        tmp_path, ouro, json.dumps({"quest_id": "plan-quest-1"}), controller=None
    )
    agent.memory = FakeMemory()

    asyncio.run(
        run_planning_run(
            agent,
            hb_model=SimpleNamespace(model_id="heartbeat-model"),
            team_id="team-1",
            servers=["ouro"],
        )
    )

    assert "## Additional Context" in agent.prompt
    assert "### Work Direction Guidance" in agent.prompt
    assert "route reliability before new demos" in agent.prompt


# ---------------------------------------------------------------------------
# Auto-approval
# ---------------------------------------------------------------------------


def test_auto_approve_opens_expired_drafts_and_prunes_cursor(tmp_path):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    save_cursor(
        tmp_path,
        "team-1",
        PlanningCursor(
            last_planned_at=now.isoformat(),
            last_quest_id="quest-old",
            pending_quest_ids=["quest-old", "quest-fresh", "quest-open"],
        ),
    )

    class FakeQuests:
        def __init__(self):
            self.updates = []

        @staticmethod
        def Content():
            return _FakeContent()

        def retrieve(self, quest_id):
            status = {
                "quest-old": "draft",
                "quest-fresh": "draft",
                "quest-open": "open",
            }[quest_id]
            created = {
                "quest-old": (now - timedelta(hours=3)).isoformat(),
                "quest-fresh": (now - timedelta(minutes=10)).isoformat(),
                "quest-open": (now - timedelta(hours=5)).isoformat(),
            }[quest_id]
            return SimpleNamespace(
                id=quest_id,
                created_at=created,
                quest=SimpleNamespace(status=status),
                items=[],
            )

        def update(self, quest_id, **kwargs):
            self.updates.append((quest_id, kwargs))

    ouro = SimpleNamespace(quests=FakeQuests(), comments=_FakeComments())
    agent = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(workspace=tmp_path),
            planning=SimpleNamespace(auto_approve=True, review_window="1h"),
        ),
        _get_ouro_client=lambda: ouro,
    )

    opened = auto_approve_due_drafts(agent, ["team-1"], now=now)

    assert opened == 1
    assert ouro.quests.updates == [("quest-old", {"status": "open"})]
    # Only the still-fresh draft stays pending; opened/already-open drop off.
    assert load_cursor(tmp_path, "team-1").pending_quest_ids == ["quest-fresh"]


# ---------------------------------------------------------------------------
# Feedback / review run
# ---------------------------------------------------------------------------


def _feedback_agent(workspace, ouro_client, run_result):
    agent = _fake_agent(workspace, ouro_client, run_result)
    return agent


def test_build_planning_prompt_allows_skip_and_requires_novelty():
    prompt = build_planning_prompt(cadence="12h", heartbeat_every="1h")

    assert '"quest_id": null' in prompt
    assert "skip_reason" in prompt
    assert "Standing Planning Guidance" in prompt or "decline to plan" in prompt
    assert "checkpoint item" in prompt
    assert "what is *different*" in prompt or "different from your" in prompt
    assert "Completion without engagement is not success" in prompt or "outcomes" in prompt


def test_build_quest_history_context_summarizes_recent_quests():
    from ouro_agents.modes.planning import build_quest_history_context

    class FakeQuests:
        def retrieve(self, quest_id):
            return SimpleNamespace(
                id=quest_id,
                name=f"Quest {quest_id}",
                quest=SimpleNamespace(status="open"),
                items=[
                    SimpleNamespace(
                        id="i1",
                        description="Paper deep-read and CIF pipeline",
                        status="done",
                    ),
                    SimpleNamespace(
                        id="i2",
                        description="Draft outreach email",
                        status="pending",
                    ),
                ],
            )

    agent = SimpleNamespace(
        own_user_id="user-1",
        config=SimpleNamespace(agent=SimpleNamespace(org_id=None)),
        _get_ouro_client=lambda: SimpleNamespace(
            quests=FakeQuests(),
            assets=SimpleNamespace(
                search=lambda **kwargs: [
                    {
                        "id": "q1",
                        "name": "Cycle 22",
                        "team_id": "team-aaa",
                        "created_at": "2026-07-11T00:00:00+00:00",
                    }
                ]
            ),
        ),
    )

    context = build_quest_history_context(agent)
    assert "## Your Recent Quests" in context
    assert "Cycle 22" in context
    assert "Paper deep-read" in context
    assert "1/2 resolved" in context


def test_run_planning_run_skip_advances_cursor_without_quest(tmp_path):
    ouro = SimpleNamespace(comments=_FakeComments(), quests=SimpleNamespace())
    agent = _fake_agent(
        tmp_path,
        ouro,
        json.dumps({"quest_id": None, "skip_reason": "recent cycles are repetitive"}),
    )

    result = asyncio.run(
        run_planning_run(
            agent,
            hb_model=SimpleNamespace(model_id="heartbeat-model"),
            team_id="team-1",
            servers=["ouro"],
        )
    )

    assert result is not None
    cursor = load_cursor(tmp_path, "team-1")
    assert cursor.last_planned_at
    assert cursor.last_quest_id == ""
    assert cursor.pending_quest_ids == []
    assert ouro.comments.created == []


def test_append_and_load_planning_guidance(tmp_path):
    from ouro_agents.modes.planning import (
        append_planning_guidance,
        load_planning_guidance,
    )

    assert load_planning_guidance(tmp_path) == ""
    assert append_planning_guidance(
        tmp_path,
        "Stop repeating the CIF pipeline.",
        source="controller",
        when=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    loaded = load_planning_guidance(tmp_path)
    assert "## Standing Planning Guidance" in loaded
    assert "Stop repeating the CIF pipeline." in loaded
    assert "2026-07-14" in loaded

