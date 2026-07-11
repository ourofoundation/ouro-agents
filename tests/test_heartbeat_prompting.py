import asyncio
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ouro_agents.agent import OuroAgent
from ouro_agents.config import HeartbeatConfig
from ouro_agents.display import OuroDisplay
from ouro_agents.modes.framing import HEARTBEAT_FRAMING
from ouro_agents.modes.heartbeat import (
    _advance_due_recurring_items,
    _load_owned_open_quest_items,
    build_quest_work_playbook,
    force_planning_heartbeat,
    has_future_heartbeat_in_active_window,
    load_work_inbox,
    run_heartbeat,
)
from ouro_agents.modes.profiles import HEARTBEAT
from ouro_agents.subagents.context import SubAgentUsage
from ouro_agents.usage import RunUsage, UsageTracker


def test_advance_due_recurring_items_reschedules_only_due_recurring():
    from datetime import timedelta, timezone

    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)

    class _FakeQuests:
        def __init__(self):
            self.calls = []

        def update_item(self, quest_id, item_id, **kwargs):
            self.calls.append((quest_id, item_id, kwargs))

    fake_quests = _FakeQuests()
    agent = SimpleNamespace(
        _get_ouro_client=lambda: SimpleNamespace(quests=fake_quests)
    )

    due_recurring = {
        "id": "item-due",
        "quest_id": "quest-1",
        "status": "in_progress",
        "waiting_on": "reply",
        "waiting_check_every": "1d",
    }
    parked_recurring = {
        "id": "item-parked",
        "quest_id": "quest-1",
        "status": "in_progress",
        "waiting_on": "reply",
        "waiting_until": (now + timedelta(hours=6)).isoformat(),
        "waiting_check_every": "1d",
    }
    plain = {
        "id": "item-plain",
        "quest_id": "quest-1",
        "status": "in_progress",
    }

    _advance_due_recurring_items(agent, [due_recurring, parked_recurring, plain], now)

    # Only the due recurring item is rescheduled, one day out.
    assert len(fake_quests.calls) == 1
    quest_id, item_id, kwargs = fake_quests.calls[0]
    assert (quest_id, item_id) == ("quest-1", "item-due")
    assert kwargs["waiting_until"] == (now + timedelta(days=1)).isoformat()
    # And the in-memory dict reflects the new next-check.
    assert due_recurring["waiting_until"] == (now + timedelta(days=1)).isoformat()


def test_heartbeat_framing_prefers_bounded_progress():
    assert "bounded work session" in HEARTBEAT_FRAMING
    assert "one meaningful slice of progress" in HEARTBEAT_FRAMING
    assert "concrete platform work" in HEARTBEAT_FRAMING


def test_heartbeat_framing_allows_direction_proposal_posts():
    assert "direction" in HEARTBEAT_FRAMING
    assert "3-5 concrete directions" in HEARTBEAT_FRAMING
    assert "do not create a quest" in HEARTBEAT_FRAMING
    assert "ouro:search_assets" in HEARTBEAT.preload_tools
    assert "ouro:create_post" in HEARTBEAT.preload_tools
    assert HEARTBEAT.restricted_servers is False
    assert HEARTBEAT.allow_delegation is True


def test_quest_work_playbook_mentions_bounded_progress_and_tools():
    playbook = build_quest_work_playbook(
        [
            {
                "id": "item-a",
                "quest_id": "quest-a",
                "status": "pending",
                "description": "Contact a researcher",
                "quest_asset": {"id": "quest-a", "name": "Outreach"},
                "inbox_source": "owned",
            }
        ]
    )

    assert "one meaningful slice of progress" in playbook
    assert "changes platform state or produces a useful artifact" in playbook
    assert "Contact a researcher" in playbook
    assert "`update_quest_item`" in playbook
    assert "`complete_quest_item`" in playbook
    assert "`write_comment`" in playbook
    assert "waiting_check_every" in playbook


def test_quest_work_playbook_adds_assigned_guidance_when_needed():
    owned_only = build_quest_work_playbook(
        [{"id": "a", "quest_id": "q", "inbox_source": "owned"}]
    )
    mixed = build_quest_work_playbook(
        [
            {"id": "a", "quest_id": "q", "inbox_source": "owned"},
            {"id": "b", "quest_id": "q2", "inbox_source": "assigned"},
        ]
    )

    assert "submit_quest_entry" not in owned_only
    assert "submit_quest_entry" in mixed
    assert "assigned to you" in mixed


def test_quest_work_playbook_surfaces_parked_handoff_for_due_recurring_items():
    """Due recurring items must show the prior tick's waiting_on note.

    Without it the item reads as fresh work and the agent redoes slices that
    were already finished before the item was parked (e.g. DCVC follow-up
    sent + prospects seeded, then resurfaced on a 1d check).
    """
    playbook = build_quest_work_playbook(
        [
            {
                "id": "019f438b-b65d-7d50-8a6e-d7bc73f2e8a7",
                "quest_id": "019f438b-b62e-72ce-9a90-b5c578c57e23",
                "status": "in_progress",
                "description": (
                    "Sponsor follow-up and new prospect identification: "
                    "send DCVC follow-up to Kiersten Stead."
                ),
                "waiting_on": (
                    "DCVC 14-day follow-up threshold (Kiersten Stead sent "
                    "June 27, eligible July 11). 2 new sponsor prospects "
                    "already seeded: Heising-Simons Foundation and Simons "
                    "Foundation."
                ),
                "waiting_check_every": "1d",
                "quest_asset": {
                    "id": "019f438b-b62e-72ce-9a90-b5c578c57e23",
                    "name": "Outreach: cycle 16 pipeline",
                },
                "inbox_source": "owned",
            }
        ]
    )

    assert "resurfaced from a waiting state" in playbook
    assert "2 new sponsor prospects already seeded" in playbook
    assert "recurring check every 1d" in playbook
    assert "complete the item if its work is already done" in playbook
    assert "Do not redo finished slices" in playbook


def test_quest_work_playbook_omits_waiting_note_for_plain_items():
    playbook = build_quest_work_playbook(
        [
            {
                "id": "item-plain",
                "quest_id": "quest-a",
                "status": "pending",
                "description": "Draft a sponsor email",
                "quest_asset": {"id": "quest-a", "name": "Outreach"},
                "inbox_source": "owned",
            }
        ]
    )

    assert "Draft a sponsor email" in playbook
    # Preamble mentions resurfaced items in general; plain items must not get
    # a per-item parked handoff line.
    assert "note from when it was parked" not in playbook
    assert "recurring check every" not in playbook.split("## Quest Inbox", 1)[1]


def test_has_future_heartbeat_in_active_window_before_last_tick():
    cfg = HeartbeatConfig(
        model="test-model",
        every="1h",
        active_hours={"start": "09:00", "end": "17:00", "timezone": "America/Chicago"},
    )
    now = datetime(2026, 4, 1, 16, 0, tzinfo=ZoneInfo("America/Chicago"))

    assert has_future_heartbeat_in_active_window(cfg, now=now) is True


def test_has_future_heartbeat_in_active_window_on_last_tick():
    cfg = HeartbeatConfig(
        model="test-model",
        every="1h",
        active_hours={"start": "09:00", "end": "17:00", "timezone": "America/Chicago"},
    )
    now = datetime(2026, 4, 1, 17, 0, tzinfo=ZoneInfo("America/Chicago"))

    assert has_future_heartbeat_in_active_window(cfg, now=now) is False


def test_run_heartbeat_preserves_existing_usage_for_main_run(tmp_path):
    captured = {}

    class _DocStore:
        def read(self, key):
            if key == "HEARTBEAT:hermes":
                return "Review the world and act."
            return None

    class _FakeAgent:
        def __init__(self, workspace: Path):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    every="1h",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(
                    model="main-model", workspace=workspace, name="hermes"
                ),
            )
            self.doc_store = _DocStore()

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        async def run(self, task, **kwargs):
            captured["task"] = task
            captured["kwargs"] = kwargs
            return '{"action":"none"}'

    agent = _FakeAgent(tmp_path)

    result = asyncio.run(run_heartbeat(agent))

    assert result is None
    assert captured["task"] == "Review the world and act."
    assert captured["kwargs"]["preserve_existing_usage"] is True
    assert captured["kwargs"]["model_override"].model_id == "heartbeat-model"


def test_run_heartbeat_appends_team_direction_when_unscoped(tmp_path):
    captured = {}

    class _MemoryItem:
        text = "Work direction: the rest of the week should be fully dedicated to outreach."
        category = "direction"
        basis = "stated"
        source = "controller-comment:comment-a"
        strength = 0.9
        score = 0.9
        created_at = "2026-06-16T00:00:00Z"

    class _Memory:
        def search(self, **kwargs):
            if kwargs.get("team_id") == "team-outreach":
                return [_MemoryItem()]
            return []

    class _DocStore:
        def read(self, key):
            if key == "HEARTBEAT:hermes":
                return "General heartbeat can browse or research."
            return None

    class _Registry:
        def team_ids(self):
            return {"team-outreach", "team-research"}

    class _FakeAgent:
        def __init__(self):
            self.memory = _Memory()
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    every="1h",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(enabled=False),
                agent=SimpleNamespace(
                    model="main-model",
                    workspace=tmp_path,
                    name="hermes",
                ),
            )
            self.doc_store = _DocStore()
            self.team_registry = _Registry()

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        def _get_ouro_client(self):
            return None

        def doc_store_for(self, _team_id):
            return self.doc_store

        async def run(self, task, **kwargs):
            captured["task"] = task
            captured["kwargs"] = kwargs
            return '{"action":"none"}'

    result = asyncio.run(run_heartbeat(_FakeAgent()))

    assert result is None
    assert "fully dedicated to outreach" in captured["task"]
    assert "Do not choose unrelated research" in captured["task"]


def test_run_heartbeat_selects_planning_team_from_direction_memory(tmp_path):
    captured = {}

    class _MemoryItem:
        text = "Work direction: dedicate the rest of the week to outreach."
        category = "direction"
        basis = "stated"
        source = "controller-comment:comment-a"
        strength = 0.9
        score = 0.9
        created_at = "2026-06-16T00:00:00+00:00"

    class _Memory:
        def search(self, **kwargs):
            if kwargs.get("team_id") == "team-outreach":
                return [_MemoryItem()]
            return []

    class _DocStore:
        def read(self, _key):
            return None

    class _Registry:
        def team_ids(self):
            return {"team-outreach", "team-research"}

        def get_team(self, team_id):
            return SimpleNamespace(
                id=team_id,
                name="outreach" if team_id == "team-outreach" else "research",
                slug="outreach" if team_id == "team-outreach" else "research",
                agent_can_create=True,
            )

    class _FakeAgent:
        own_user_id = None  # no inbox — planning gets the tick

        def __init__(self):
            self.memory = _Memory()
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    every="1h",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(
                    enabled=True,
                    cadence="4h",
                    review_window="1h",
                    auto_approve=False,
                ),
                agent=SimpleNamespace(
                    model="main-model",
                    workspace=tmp_path,
                    name="hermes",
                ),
            )
            self.doc_store = _DocStore()
            self.team_registry = _Registry()

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        def _get_ouro_client(self):
            return None

        def doc_store_for(self, team_id):
            return self.doc_store

    async def _fake_planning(_agent, _model, team_id, _servers, **_kwargs):
        captured["planning_team_id"] = team_id
        return "planned"

    with patch("ouro_agents.modes.planning.run_planning_run", new=_fake_planning):
        result = asyncio.run(run_heartbeat(_FakeAgent()))

    assert result == "planned"
    assert captured["planning_team_id"] == "team-outreach"


def test_load_owned_open_quest_items_includes_unassigned_items():
    class _Assets:
        def search(self, **kwargs):
            assert kwargs["asset_type"] == "quest"
            assert kwargs["user_id"] == "agent-user"
            return [
                {
                    "id": "quest-a",
                    "name": "Outreach",
                    "org_id": "org-a",
                    "team_id": "team-a",
                    "user_id": "agent-user",
                }
            ]

    class _Quests:
        def retrieve(self, quest_id):
            assert quest_id == "quest-a"
            return SimpleNamespace(
                id="quest-a",
                name="Outreach",
                org_id="org-a",
                team_id="team-a",
                user_id="agent-user",
                quest=SimpleNamespace(status="open", type="closable"),
                items=[
                    SimpleNamespace(
                        id="item-a",
                        quest_id="quest-a",
                        status="in_progress",
                        assignee_id=None,
                        description="Contact a researcher",
                    ),
                    SimpleNamespace(
                        id="item-b",
                        quest_id="quest-a",
                        status="done",
                        assignee_id=None,
                        description="Already done",
                    ),
                ],
            )

    agent = SimpleNamespace(
        own_user_id="agent-user",
        config=SimpleNamespace(agent=SimpleNamespace(org_id="org-a")),
        _get_ouro_client=lambda: SimpleNamespace(assets=_Assets(), quests=_Quests()),
    )

    items = _load_owned_open_quest_items(agent)

    assert len(items) == 1
    assert items[0]["id"] == "item-a"
    assert items[0]["inbox_source"] == "owned"
    assert items[0]["quest_asset"]["id"] == "quest-a"


def test_load_owned_open_quest_items_excludes_draft_quests():
    class _Assets:
        def search(self, **_kwargs):
            return [
                {
                    "id": "quest-draft",
                    "name": "Draft plan quest",
                    "org_id": "org-a",
                    "team_id": "team-a",
                    "user_id": "agent-user",
                }
            ]

    class _Quests:
        def retrieve(self, _quest_id):
            return SimpleNamespace(
                id="quest-draft",
                quest=SimpleNamespace(status="draft", type="closable"),
                items=[
                    SimpleNamespace(
                        id="item-a",
                        quest_id="quest-draft",
                        status="pending",
                        description="Not approved yet",
                    )
                ],
            )

    agent = SimpleNamespace(
        own_user_id="agent-user",
        config=SimpleNamespace(agent=SimpleNamespace(org_id="org-a")),
        _get_ouro_client=lambda: SimpleNamespace(assets=_Assets(), quests=_Quests()),
    )

    assert _load_owned_open_quest_items(agent) == []


def test_load_work_inbox_puts_assigned_items_first_and_dedupes():
    class _Assets:
        def search(self, **_kwargs):
            return [
                {
                    "id": "quest-a",
                    "name": "Outreach",
                    "org_id": "org-a",
                    "team_id": "team-a",
                    "user_id": "agent-user",
                }
            ]

    class _Quests:
        def list_assigned_items(self, **_kwargs):
            return [
                {
                    "id": "item-assigned",
                    "quest_id": "quest-other",
                    "status": "pending",
                    "description": "Assigned by someone else",
                },
                {
                    "id": "item-a",
                    "quest_id": "quest-a",
                    "status": "pending",
                    "description": "Also on my own quest",
                },
            ]

        def retrieve(self, _quest_id):
            return SimpleNamespace(
                id="quest-a",
                name="Outreach",
                org_id="org-a",
                team_id="team-a",
                user_id="agent-user",
                quest=SimpleNamespace(status="open", type="closable"),
                items=[
                    SimpleNamespace(
                        id="item-a",
                        quest_id="quest-a",
                        status="pending",
                        description="Also on my own quest",
                    ),
                    SimpleNamespace(
                        id="item-c",
                        quest_id="quest-a",
                        status="pending",
                        description="Owned only",
                    ),
                ],
            )

    agent = SimpleNamespace(
        own_user_id="agent-user",
        config=SimpleNamespace(agent=SimpleNamespace(org_id="org-a")),
        _get_ouro_client=lambda: SimpleNamespace(assets=_Assets(), quests=_Quests()),
    )

    inbox = load_work_inbox(agent)

    assert [item["id"] for item in inbox] == ["item-assigned", "item-a", "item-c"]
    assert inbox[0]["inbox_source"] == "assigned"
    assert inbox[2]["inbox_source"] == "owned"


def test_run_heartbeat_works_inbox_before_planning(tmp_path):
    captured = {}

    class _Assets:
        def search(self, **_kwargs):
            return [
                {
                    "id": "quest-a",
                    "name": "Outreach",
                    "org_id": "org-a",
                    "team_id": "team-a",
                    "user_id": "agent-user",
                }
            ]

    class _Quests:
        def list_assigned_items(self, **_kwargs):
            return []

        def retrieve(self, quest_id):
            assert quest_id == "quest-a"
            return SimpleNamespace(
                id="quest-a",
                name="Outreach",
                org_id="org-a",
                team_id="team-a",
                user_id="agent-user",
                quest=SimpleNamespace(status="open", type="closable"),
                items=[
                    SimpleNamespace(
                        id="item-a",
                        quest_id="quest-a",
                        status="pending",
                        description="Contact a researcher",
                    )
                ],
            )

    class _FakeAgent:
        own_user_id = "agent-user"

        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(
                    enabled=True,
                    cadence="4h",
                    review_window="1h",
                    auto_approve=False,
                ),
                agent=SimpleNamespace(
                    model="main-model",
                    workspace=tmp_path,
                    name="hermes",
                    org_id="org-a",
                ),
            )
            self.doc_store = SimpleNamespace(read=lambda _key: None)
            self.team_registry = SimpleNamespace(
                team_ids=lambda: {"team-a"},
                get_team=lambda tid: SimpleNamespace(id=tid, agent_can_create=True),
            )

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        def _get_ouro_client(self):
            return SimpleNamespace(assets=_Assets(), quests=_Quests())

        def doc_store_for(self, team_id):
            assert team_id == "team-a"
            return self.doc_store

        async def run(self, task, **kwargs):
            captured["task"] = task
            captured["kwargs"] = kwargs
            return '{"action":"none"}'

    async def _no_planning(*_args, **_kwargs):
        raise AssertionError("planning must not run while the inbox has work")

    with patch("ouro_agents.modes.planning.run_planning_run", new=_no_planning):
        result = asyncio.run(run_heartbeat(_FakeAgent()))

    assert result is None
    assert "working your quest inbox" in captured["task"]
    assert "Contact a researcher" in captured["task"]
    assert captured["kwargs"]["team_id"] == "team-a"
    assert captured["kwargs"]["preload_tools"] == [
        "ouro:get_asset",
        "ouro:list_quest_items",
        "ouro:update_quest_item",
        "ouro:complete_quest_item",
        "ouro:submit_quest_entry",
        "ouro:write_comment",
    ]


def test_run_heartbeat_skips_planning_when_cadence_not_due(tmp_path):
    from ouro_agents.modes.planning import PlanningCursor, save_cursor
    from datetime import timezone

    save_cursor(
        tmp_path,
        "team-a",
        PlanningCursor(last_planned_at=datetime.now(timezone.utc).isoformat()),
    )

    captured = {}

    class _FakeAgent:
        own_user_id = None

        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                    active_hours=None,
                ),
                planning=SimpleNamespace(
                    enabled=True,
                    cadence="1d",
                    review_window="1h",
                    auto_approve=False,
                ),
                agent=SimpleNamespace(
                    model="main-model", workspace=tmp_path, name="hermes"
                ),
            )
            self.doc_store = SimpleNamespace(
                read=lambda key: (
                    "General playbook" if key == "HEARTBEAT:hermes" else None
                )
            )
            self.team_registry = SimpleNamespace(
                team_ids=lambda: {"team-a"},
                get_team=lambda tid: SimpleNamespace(id=tid, agent_can_create=True),
            )

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

        def _get_ouro_client(self):
            return None

        def doc_store_for(self, _team_id):
            return self.doc_store

        async def run(self, task, **kwargs):
            captured["task"] = task
            return '{"action":"none"}'

    async def _no_planning(*_args, **_kwargs):
        raise AssertionError("planning must not run before the cadence is due")

    with patch("ouro_agents.modes.planning.run_planning_run", new=_no_planning):
        result = asyncio.run(run_heartbeat(_FakeAgent()))

    assert result is None
    assert captured["task"].startswith("General playbook")


def test_agent_heartbeat_resets_stale_usage_before_run():
    agent = OuroAgent.__new__(OuroAgent)
    agent._usage_tracker = UsageTracker()
    agent._usage_tracker.record(
        "stale-run",
        {"input_tokens": 70_000_000, "output_tokens": 1_000_000},
    )
    agent._subagent_ledger = [("stale-subagent", SimpleNamespace())]

    class _Memory:
        def __init__(self):
            self.reset_calls = 0

        def reset_usage(self):
            self.reset_calls += 1

    agent.memory = _Memory()
    observed = {}

    async def _fake_run_heartbeat(running_agent):
        observed["input_tokens"] = running_agent._usage_tracker.total_input_tokens
        observed["subagent_ledger"] = list(running_agent._subagent_ledger)
        observed["memory_reset_calls"] = running_agent.memory.reset_calls
        return '{"action":"none"}'

    with patch("ouro_agents.modes.heartbeat.run_heartbeat", new=_fake_run_heartbeat):
        result = asyncio.run(agent.heartbeat())

    assert result == '{"action":"none"}'
    assert observed == {
        "input_tokens": 0,
        "subagent_ledger": [],
        "memory_reset_calls": 1,
    }


def test_force_planning_heartbeat_uses_explicit_team_id(tmp_path):
    captured = {}

    class _Registry:
        def team_ids(self):
            return {"team-a", "team-b"}

    class _FakeAgent:
        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    proactive=SimpleNamespace(enabled=False, servers=[]),
                ),
                agent=SimpleNamespace(
                    model="main-model", workspace=tmp_path, name="hermes"
                ),
            )
            self.team_registry = _Registry()

        def _build_model(self, model_id, heartbeat=False):
            return SimpleNamespace(model_id=model_id, heartbeat=heartbeat)

        def _refresh_platform_context(self):
            return None

    agent = _FakeAgent()

    async def _fake_run_planning_run(_agent, hb_model, team_id, servers, goal=""):
        captured["model_id"] = hb_model.model_id
        captured["team_id"] = team_id
        captured["servers"] = servers
        captured["goal"] = goal
        return "planned"

    with patch(
        "ouro_agents.modes.planning.run_planning_run",
        new=_fake_run_planning_run,
    ):
        result = asyncio.run(
            force_planning_heartbeat(agent, goal="Focus on team B", team_id="team-b")
        )

    assert result == "planned"
    assert captured["model_id"] == "heartbeat-model"
    assert captured["team_id"] == "team-b"
    assert captured["servers"] == ["ouro"]
    assert captured["goal"] == "Focus on team B"


def test_usage_rows_include_model_ids_for_run_and_subagent_rows():
    display = OuroDisplay()
    total = RunUsage(
        model_id="main-model",
        steps=3,
        input_tokens=120,
        current_context_tokens=90,
        output_tokens=30,
        cost_usd=0.12,
    ).finalize()
    preflight = SubAgentUsage(
        model_id="preflight-model",
        steps=1,
        input_tokens=40,
        current_context_tokens=35,
        output_tokens=10,
        llm_calls=1,
        wall_time_ms=250,
        cost_usd=0.02,
    )

    rows = display._usage_rows(
        total, duration_s=1.5, subagent_ledger=[("preflight", preflight)]
    )

    assert rows[0][1] == "main-model"
    assert rows[1][0] == "sub:preflight"
    assert rows[1][1] == "preflight-model"
    assert rows[1][3] == "35"
    assert rows[-1][0] == "total"
    assert rows[-1][1] == "main-model"
    assert rows[-1][3] == "90"


def test_usage_rows_hide_reasoning_by_default():
    display = OuroDisplay()
    total = RunUsage(model_id="main-model", input_tokens=10, output_tokens=5).finalize()

    rows = display._usage_rows(total, duration_s=1.5)

    assert len(rows[0]) == 10
    assert "Context" in display._usage_table_headers()
    assert "Reasoning" not in display._usage_table_headers()


def test_usage_rows_can_show_reasoning_when_enabled():
    display = OuroDisplay(show_reasoning_in_summary=True)
    total = RunUsage(
        model_id="main-model",
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=3,
    ).finalize()

    rows = display._usage_rows(total, duration_s=1.5)

    assert len(rows[0]) == 11
    assert "Reasoning" in display._usage_table_headers()
    assert rows[0][8] == "3"
