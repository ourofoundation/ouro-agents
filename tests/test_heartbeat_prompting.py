import asyncio
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ouro_agents.agent import OuroAgent
from ouro_agents.config import HeartbeatConfig
from ouro_agents.display import OuroDisplay
from ouro_agents.modes.framing import HEARTBEAT_FRAMING, HEARTBEAT_QUEST_MECHANICS, heartbeat_framing_for_kind
from ouro_agents.modes.heartbeat import (
    TickKind,
    _advance_due_recurring_items,
    _load_owned_open_quest_items,
    build_quest_work_playbook,
    force_planning_heartbeat,
    has_future_heartbeat_in_active_window,
    is_curiosity_window,
    load_work_inbox,
    parse_heartbeat_tick_summary,
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
    assert "one meaningful slice" in HEARTBEAT_FRAMING
    assert "concrete platform work" in HEARTBEAT_FRAMING
    assert "You own the whole tick" in HEARTBEAT_FRAMING


def test_heartbeat_framing_allows_direction_proposal_posts():
    assert "direction" in HEARTBEAT_FRAMING
    assert "strategist" not in HEARTBEAT_FRAMING
    assert "delegate" in HEARTBEAT_FRAMING.lower() or "`search`" in HEARTBEAT_FRAMING
    assert "ouro:search_assets" in HEARTBEAT.preload_tools
    assert "ouro:create_post" in HEARTBEAT.preload_tools
    assert HEARTBEAT.restricted_servers is True
    assert HEARTBEAT.allow_delegation is True
    assert HEARTBEAT.skip_post_reflection is False
    assert HEARTBEAT.max_steps == 40
    assert "skip_preflight" not in type(HEARTBEAT).model_fields


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
    # Tool mechanics live in quest_work framing, not the data-shaped playbook.
    assert "`update_quest_item`" not in playbook
    assert "MODE framing" in playbook


def test_heartbeat_framing_carries_quest_execution_mechanics():
    # Quest mechanics are appended only for quest_work ticks.
    quest = heartbeat_framing_for_kind("quest_work")
    assert "`update_quest_item`" in quest
    assert "`complete_quest_item`" in quest
    assert "`write_comment`" in quest
    assert "waiting_check_every" in quest
    assert "`submit_quest_entry`" in quest
    assert "`create_quest_items`" in quest
    assert "Never execute an item you know is stale" in quest
    assert "`update_quest_item`" in HEARTBEAT_QUEST_MECHANICS
    assert "`update_quest_item`" not in HEARTBEAT_FRAMING


def test_parse_heartbeat_tick_summary_pass_and_act():
    pass_summary = parse_heartbeat_tick_summary(
        '{"action": "none", "details": "inbox clear", "selected_priority": null, '
        '"worth_remembering": false, "memory_notes": []}'
    )
    assert pass_summary["is_pass"] is True
    assert pass_summary["worth_remembering"] is False
    assert pass_summary["selected_priority"] is None

    act = parse_heartbeat_tick_summary(
        '{"action": "sent follow-up", "details": "emailed Alice", '
        '"selected_priority": 2, "worth_remembering": true, '
        '"memory_notes": ["Alice replied last week"]}'
    )
    assert act["is_pass"] is False
    assert act["selected_priority"] == 2
    assert act["worth_remembering"] is True
    assert act["memory_notes"] == ["Alice replied last week"]


def test_build_heartbeat_task_context_composes_inbox_with_policy(tmp_path):
    from ouro_agents.modes.heartbeat import build_heartbeat_task_context

    heartbeat_md = tmp_path / "HEARTBEAT.md"
    heartbeat_md.write_text(
        "Priority order\n1. Advance a live conversation.\n2. Send a due follow-up.\n"
    )

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
                heartbeat=SimpleNamespace(active_hours=None),
                agent=SimpleNamespace(
                    model="main-model",
                    workspace=tmp_path,
                    name="hermes",
                    org_id="org-a",
                ),
            )
            self.doc_store = SimpleNamespace(read=lambda _key: None)
            self.memory = None
            self.team_registry = SimpleNamespace(team_ids=lambda: {"team-a"})

        def _get_ouro_client(self):
            return SimpleNamespace(assets=_Assets(), quests=_Quests())

        def doc_store_for(self, _team_id):
            return self.doc_store

    ctx = build_heartbeat_task_context(_FakeAgent(), advance_recurring=False)

    assert ctx.source == "quest-inbox"
    assert ctx.tick_kind == TickKind.QUEST_WORK
    assert ctx.include_plans_index is False
    assert "`update_quest_item`" in ctx.framing_override
    assert "Contact a researcher" in ctx.playbook
    assert "## Heartbeat Policy" in ctx.playbook
    assert "Advance a live conversation" in ctx.playbook


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

    # Assigned/owned nature is a selection signal; entry-vs-complete tool
    # mechanics live in quest_work MODE framing.
    assert "entry submission" not in owned_only
    assert "entry submission" in mixed
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


def _curiosity_config(**overrides):
    kwargs = {
        "model": "test-model",
        "every": "1h",
        "active_hours": {
            "start": "09:00",
            "end": "22:00",
            "timezone": "America/Chicago",
        },
        "curiosity": {"enabled": True, "last_beats": 3},
    }
    kwargs.update(overrides)
    return HeartbeatConfig(**kwargs)


def test_is_curiosity_window_covers_exactly_the_last_beats():
    cfg = _curiosity_config()
    tz = ZoneInfo("America/Chicago")

    # Hourly beats 09:00–22:00; the last three are 20:00, 21:00, 22:00.
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 18, 0, tzinfo=tz)) is False
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 19, 0, tzinfo=tz)) is False
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 20, 0, tzinfo=tz)) is True
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 21, 0, tzinfo=tz)) is True
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 22, 0, tzinfo=tz)) is True
    # A delayed tick just inside the window still counts.
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 20, 30, tzinfo=tz)) is True


def test_is_curiosity_window_excludes_time_outside_active_hours():
    cfg = _curiosity_config()
    tz = ZoneInfo("America/Chicago")

    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 22, 30, tzinfo=tz)) is False
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 8, 0, tzinfo=tz)) is False


def test_is_curiosity_window_wraps_midnight():
    cfg = _curiosity_config(
        active_hours={
            "start": "22:00",
            "end": "02:00",
            "timezone": "America/Chicago",
        }
    )
    tz = ZoneInfo("America/Chicago")

    # Beats at 22:00–02:00; the last three are 00:00, 01:00, 02:00.
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 1, 23, 0, tzinfo=tz)) is False
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 2, 0, 0, tzinfo=tz)) is True
    assert is_curiosity_window(cfg, now=datetime(2026, 4, 2, 2, 0, tzinfo=tz)) is True


def test_is_curiosity_window_requires_enablement_and_active_hours():
    tz = ZoneInfo("America/Chicago")
    now = datetime(2026, 4, 1, 21, 0, tzinfo=tz)

    assert is_curiosity_window(
        _curiosity_config(curiosity={"enabled": False, "last_beats": 3}), now=now
    ) is False
    assert is_curiosity_window(_curiosity_config(active_hours=None), now=now) is False
    # Default config: curiosity disabled.
    assert is_curiosity_window(
        HeartbeatConfig(model="test-model"), now=now
    ) is False


def test_heartbeat_framing_for_curiosity_kind():
    from ouro_agents.modes.framing import CURIOSITY_FRAMING

    framing = heartbeat_framing_for_kind("curiosity")
    assert framing == CURIOSITY_FRAMING
    assert "genuinely excited about" in framing
    assert "priority ladder" in framing
    # Quest mechanics stay quest-only.
    assert "`update_quest_item`" not in framing


def test_curiosity_window_builds_curiosity_context(tmp_path):
    from ouro_agents.modes.framing import CURIOSITY_FRAMING
    from ouro_agents.modes.heartbeat import build_heartbeat_task_context

    (tmp_path / "CURIOSITY.md").write_text(
        "The workday is done. Follow whatever is pulling at you.\n"
    )
    (tmp_path / "HEARTBEAT.md").write_text("Priority order\n1. Do work.\n")

    class _Memory:
        def search(self, **_kwargs):
            raise AssertionError(
                "work-direction recall must not run during curiosity ticks"
            )

    class _FakeAgent:
        own_user_id = "agent-user"

        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(active_hours=None),
                agent=SimpleNamespace(
                    model="main-model",
                    workspace=tmp_path,
                    name="hermes",
                    org_id="org-a",
                ),
            )
            self.doc_store = SimpleNamespace(read=lambda _key: None)
            self.memory = _Memory()
            self.team_registry = SimpleNamespace(team_ids=lambda: set())

        def _get_ouro_client(self):
            return None

        def doc_store_for(self, _team_id):
            return self.doc_store

    inbox = [
        {
            "id": "item-a",
            "quest_id": "quest-a",
            "status": "pending",
            "description": "Contact a researcher",
            "quest_asset": {"id": "quest-a", "name": "Outreach"},
            "inbox_source": "owned",
        }
    ]

    with patch(
        "ouro_agents.modes.heartbeat.is_curiosity_window", return_value=True
    ):
        ctx = build_heartbeat_task_context(
            _FakeAgent(), inbox=inbox, advance_recurring=False
        )

    assert ctx.tick_kind == TickKind.CURIOSITY
    assert ctx.source == "curiosity"
    assert ctx.framing_override == CURIOSITY_FRAMING
    assert ctx.include_plans_index is False
    # The curiosity playbook drives, not the quest inbox...
    assert "Follow whatever is pulling at you" in ctx.playbook
    assert "choosing and executing this heartbeat's quest work" not in ctx.playbook
    # ...but the inbox is surfaced as an urgency check only.
    assert "Urgency check" in ctx.playbook
    assert "Contact a researcher" in ctx.playbook
    assert "do not work the inbox tonight" in ctx.playbook


def test_curiosity_window_without_playbook_falls_back_to_normal(tmp_path):
    from ouro_agents.modes.heartbeat import build_heartbeat_task_context

    (tmp_path / "HEARTBEAT.md").write_text("Priority order\n1. Do work.\n")

    class _FakeAgent:
        own_user_id = "agent-user"

        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(active_hours=None),
                agent=SimpleNamespace(
                    model="main-model",
                    workspace=tmp_path,
                    name="hermes",
                    org_id="org-a",
                ),
            )
            self.doc_store = SimpleNamespace(read=lambda _key: None)
            self.memory = None
            self.team_registry = SimpleNamespace(team_ids=lambda: set())

        def _get_ouro_client(self):
            return None

        def doc_store_for(self, _team_id):
            return self.doc_store

    with patch(
        "ouro_agents.modes.heartbeat.is_curiosity_window", return_value=True
    ):
        ctx = build_heartbeat_task_context(
            _FakeAgent(), inbox=[], advance_recurring=False
        )

    assert ctx.tick_kind == TickKind.OPEN_ENDED
    assert ctx.source == "playbook"
    assert "Do work" in ctx.playbook


def test_run_heartbeat_skips_planning_during_curiosity_window(tmp_path):
    captured = {}

    (tmp_path / "CURIOSITY.md").write_text("Wander and follow your nose.\n")

    class _Registry:
        def team_ids(self):
            return {"team-a"}

        def get_team(self, team_id):
            return SimpleNamespace(id=team_id, agent_can_create=True)

    class _FakeAgent:
        own_user_id = None  # no inbox — planning would normally get the tick

        def __init__(self):
            self.config = SimpleNamespace(
                heartbeat=SimpleNamespace(
                    model="heartbeat-model",
                    every="1h",
                    servers=["ouro"],
                    active_hours=None,
                ),
                planning=SimpleNamespace(
                    enabled=True,
                    cadence="1h",
                    review_window="1h",
                    auto_approve=False,
                ),
                agent=SimpleNamespace(
                    model="main-model",
                    workspace=tmp_path,
                    name="hermes",
                ),
            )
            self.doc_store = SimpleNamespace(read=lambda _key: None)
            self.team_registry = _Registry()
            self.memory = None

        def _build_model(self, model_id, heartbeat=False, **kwargs):
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

    async def _no_planning(*_args, **_kwargs):
        raise AssertionError("planning must not run during the curiosity window")

    with patch(
        "ouro_agents.modes.heartbeat.is_curiosity_window", return_value=True
    ), patch("ouro_agents.modes.planning.run_planning_run", new=_no_planning):
        result = asyncio.run(run_heartbeat(_FakeAgent()))

    assert result is None
    assert "Wander and follow your nose" in captured["task"]
    assert captured["kwargs"]["heartbeat_tick_kind"] == "curiosity"


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

        def _build_model(self, model_id, heartbeat=False, **kwargs):
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


def test_run_heartbeat_appends_global_direction_when_unscoped(tmp_path):
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
            # Unscoped heartbeats only do a global recall (no per-team fan-out).
            if kwargs.get("team_id") is None:
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
                    servers=["ouro"],
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

        def _build_model(self, model_id, heartbeat=False, **kwargs):
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
    assert captured["kwargs"]["allowed_servers"] == ["ouro"]


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

        def _build_model(self, model_id, heartbeat=False, **kwargs):
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

        def _build_model(self, model_id, heartbeat=False, **kwargs):
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
    assert "choosing and executing this heartbeat's quest work" in captured["task"]
    assert "Contact a researcher" in captured["task"]
    assert captured["kwargs"]["team_id"] == "team-a"
    assert captured["kwargs"]["preload_tools"] == [
        "ouro:get_asset",
        "ouro:list_quest_items",
        "ouro:update_quest_item",
        "ouro:create_quest_items",
        "ouro:delete_quest_item",
        "ouro:complete_quest_item",
        "ouro:submit_quest_entry",
        "ouro:write_comment",
        "ouro:update_quest",
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

        def _build_model(self, model_id, heartbeat=False, **kwargs):
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

        def _build_model(self, model_id, heartbeat=False, **kwargs):
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
    reflector = SubAgentUsage(
        model_id="reflector-model",
        steps=1,
        input_tokens=40,
        current_context_tokens=35,
        output_tokens=10,
        llm_calls=1,
        wall_time_ms=250,
        cost_usd=0.02,
    )

    rows = display._usage_rows(
        total, duration_s=1.5, subagent_ledger=[("reflector", reflector)]
    )

    assert rows[0][1] == "main-model"
    assert rows[1][0] == "sub:reflector"
    assert rows[1][1] == "reflector-model"
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


def test_quest_work_playbook_allows_mid_quest_revision_on_owned_quests():
    playbook = build_quest_work_playbook(
        [
            {
                "id": "item-a",
                "quest_id": "quest-a",
                "status": "pending",
                "description": "Checkpoint: revise remaining items",
                "quest_asset": {"id": "quest-a", "name": "Adaptive"},
                "inbox_source": "owned",
            }
        ]
    )

    # Adaptive-selection signal stays in the playbook; revision tool mechanics
    # live in quest_work MODE framing.
    assert "stale or improvable" in playbook
    assert "prefer revising that item" in playbook
    assert "`create_quest_items`" not in playbook


def test_planning_budget_blocks_on_max_plans_per_day(tmp_path):
    from datetime import timedelta, timezone

    from ouro_agents.modes.heartbeat import planning_budget_blocks
    from ouro_agents.modes.planning import PlanningCursor, save_cursor

    now = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
    save_cursor(
        tmp_path,
        "team-a",
        PlanningCursor(
            last_planned_at=(now - timedelta(hours=2)).isoformat(),
            last_quest_id="q1",
        ),
    )
    save_cursor(
        tmp_path,
        "team-b",
        PlanningCursor(
            last_planned_at=(now - timedelta(hours=5)).isoformat(),
            last_quest_id="q2",
        ),
    )

    agent = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(workspace=tmp_path),
            planning=SimpleNamespace(max_plans_per_day=2, backlog_limit=8),
        ),
        own_user_id="user-1",
        _get_ouro_client=lambda: SimpleNamespace(
            assets=SimpleNamespace(search=lambda **kwargs: []),
            quests=SimpleNamespace(),
        ),
    )

    reason = planning_budget_blocks(agent, ["team-a", "team-b"], now=now)
    assert reason is not None
    assert "plan budget exhausted" in reason


def test_planning_budget_blocks_on_backlog_including_waiting(tmp_path):
    from ouro_agents.modes.heartbeat import planning_budget_blocks

    class FakeQuests:
        def retrieve(self, quest_id):
            return SimpleNamespace(
                id=quest_id,
                quest=SimpleNamespace(status="open"),
                items=[
                    SimpleNamespace(
                        id="i1",
                        description="Parked follow-up",
                        status="in_progress",
                        waiting_on="reply",
                        waiting_until="2099-01-01T00:00:00+00:00",
                    ),
                    SimpleNamespace(
                        id="i2",
                        description="Another pending",
                        status="pending",
                    ),
                ],
            )

    agent = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(workspace=tmp_path),
            planning=SimpleNamespace(max_plans_per_day=10, backlog_limit=2),
        ),
        own_user_id="user-1",
        _get_ouro_client=lambda: SimpleNamespace(
            quests=FakeQuests(),
            assets=SimpleNamespace(
                search=lambda **kwargs: [
                    {"id": "quest-1", "name": "Sponsor sprint"}
                ]
            ),
        ),
    )

    reason = planning_budget_blocks(agent, ["team-1"])
    assert reason is not None
    assert "open backlog too large" in reason
