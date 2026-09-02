import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ouro_agents.config import MemoryConfig
from ouro_agents.memory import MemoryResult
from ouro_agents.memory.dream import (
    _STRENGTH_DECAY_PERIOD_KEY,
    DreamPlan,
    compact_memory_md,
    decay_memory_strength,
    dream_health_note,
    promote_log_entries,
    read_dream_status,
    run_refinement_phase,
    write_dream_status,
)
from ouro_agents.memory.naming import period_key, period_key_offset
from ouro_agents.memory.ouro_docs import LocalDocStore


class _PromotionModel:
    def __call__(self, messages):
        return SimpleNamespace(
            content=json.dumps(
                [
                    {
                        "section": "Learnings",
                        "entry": "Verify promotions land in working memory.",
                    }
                ]
            )
        )


class _CompactionModel:
    def __call__(self, messages):
        return SimpleNamespace(
            content="# Memory\n\n## Facts\n- One compact fact.\n"
        )


class _Backend:
    def __init__(self):
        self.updated = []
        self.deleted = []

    def update_metadata(self, memory_id, metadata):
        self.updated.append((memory_id, metadata))

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def _memory_config(**overrides):
    return MemoryConfig(
        extraction_model="test-model",
        embedder="test-embedder",
        decay_after_days=30,
        **overrides,
    )


def _old_memory(
    *,
    memory_id: str = "mem-1",
    category: str = "fact",
    strength: float = 0.8,
) -> MemoryResult:
    created_at = (
        datetime.now(timezone.utc) - timedelta(days=60)
    ).isoformat()
    return MemoryResult(
        id=memory_id,
        text="Durable but unused memory",
        category=category,
        strength=strength,
        created_at=created_at,
    )


def _plan(*, dry_run: bool = False) -> DreamPlan:
    return DreamPlan(
        scope="team-42",
        agent_id="hermes",
        team_id="team-42",
        rhythm="daily",
        period=period_key("daily"),
        dry_run=dry_run,
    )


def test_promote_log_entries_handles_final_section_without_newline(
    tmp_path: Path,
):
    store = LocalDocStore(
        tmp_path,
        agent_name="hermes",
        team_id="team-42",
        rhythm="daily",
    )
    memory_name = store.memory_name("hermes")
    log_name = store.log_name(
        "hermes",
        period_key_offset("daily", -1),
    )
    store.write(
        memory_name,
        "# Memory\n\n## Facts\n- Existing fact.\n\n## Learnings",
    )
    store.write(
        log_name,
        "# Daily Log\n\n- Useful durable lesson from the prior period.\n",
    )
    plan = _plan()

    promoted = promote_log_entries(
        tmp_path,
        _PromotionModel(),
        doc_store=store,
        agent_name="hermes",
        plan=plan,
    )

    assert promoted == 1
    assert (
        "## Learnings\n- Verify promotions land in working memory."
        in store.read(memory_name)
    )
    assert plan.llm_calls[0]["phase"] == "promotion"
    assert plan.operations[0].detail["entries"][0]["section"] == "Learnings"


def test_promote_log_entries_uses_daily_logs_for_weekly_fallback(
    tmp_path: Path,
):
    store = LocalDocStore(
        tmp_path,
        agent_name="hermes",
        team_id="team-42",
        rhythm="weekly",
    )
    previous_week = period_key_offset("weekly", -1)
    year_text, week_text = previous_week.split("-W", 1)
    previous_monday = date.fromisocalendar(
        int(year_text),
        int(week_text),
        1,
    )
    store.write(
        store.memory_name("hermes"),
        "# Memory\n\n## Learnings\n",
    )
    store.write(
        store.log_name("hermes", previous_monday.isoformat()),
        "# Daily Log\n\n- Useful durable lesson from the prior week.\n",
    )

    promoted = promote_log_entries(
        tmp_path,
        _PromotionModel(),
        doc_store=store,
        agent_name="hermes",
    )

    assert promoted == 1
    assert (
        "Verify promotions land in working memory."
        in store.read(store.memory_name("hermes"))
    )


def test_compact_memory_dry_run_records_plan_without_writing(
    tmp_path: Path,
):
    store = LocalDocStore(
        tmp_path,
        agent_name="hermes",
        team_id="team-42",
        rhythm="daily",
    )
    memory_name = store.memory_name("hermes")
    original = "# Memory\n\n## Facts\n" + "\n".join(
        f"- Repeated fact {index}" for index in range(10)
    )
    store.write(memory_name, original)
    plan = _plan(dry_run=True)

    compacted = compact_memory_md(
        tmp_path,
        _memory_config(memory_md_max_tokens=10),
        _CompactionModel(),
        doc_store=store,
        agent_name="hermes",
        dry_run=True,
        plan=plan,
    )

    assert compacted is True
    assert store.read(memory_name) == original
    assert plan.operations[0].kind == "compaction"
    assert plan.operations[0].status == "planned"


def test_decay_updates_memory_and_period_marker():
    backend = _Backend()
    memory = _old_memory()

    count = decay_memory_strength(
        backend,
        "hermes",
        _memory_config(),
        team_id="team-42",
        all_memories=[memory],
    )

    assert count == 1
    assert memory.strength < 0.8
    assert backend.updated[0][0] == "mem-1"
    assert (
        backend.updated[0][1][_STRENGTH_DECAY_PERIOD_KEY]
        == period_key("daily")
    )


def test_decay_dry_run_plans_without_backend_mutation():
    backend = _Backend()
    memory = _old_memory()
    plan = _plan(dry_run=True)

    count = decay_memory_strength(
        backend,
        "hermes",
        _memory_config(),
        team_id="team-42",
        all_memories=[memory],
        dry_run=True,
        plan=plan,
    )

    assert count == 1
    assert backend.updated == []
    assert backend.deleted == []
    assert plan.operations[0].kind == "strength_decay"
    assert plan.operations[0].status == "planned"


def test_decay_preserves_direction_memories():
    backend = _Backend()

    count = decay_memory_strength(
        backend,
        "hermes",
        _memory_config(),
        team_id="team-42",
        all_memories=[_old_memory(category="direction")],
    )

    assert count == 0
    assert backend.updated == []
    assert backend.deleted == []


def test_refinement_dry_run_is_non_mutating():
    plan = _plan(dry_run=True)
    agent = SimpleNamespace()

    summary = run_refinement_phase(
        agent,
        dry_run=True,
        plan=plan,
    )

    assert summary == {
        "pending": 0,
        "edits": 0,
        "memory_deletes": 0,
        "queue_applied": 0,
    }
    assert plan.skipped == [
        {"kind": "refinement", "reason": "dry_run"}
    ]


def test_dream_status_surfaces_and_clears_failures(tmp_path: Path):
    assert dream_health_note(tmp_path) == ""

    write_dream_status(
        tmp_path,
        "2026-W29",
        {
            "shared": {"promoted": 0},
            "team-42": {
                "failures": [
                    "Log promotion failed: Error code: 402"
                ],
            },
        },
    )

    status = read_dream_status(tmp_path)
    assert status is not None
    assert status["scopes_run"] == 2
    assert status["scopes_with_failures"] == 1
    note = dream_health_note(tmp_path)
    assert "2026-W29" in note
    assert "1 of 2 scopes" in note
    assert "402" in note

    write_dream_status(
        tmp_path,
        "2026-W30",
        {"shared": {"promoted": 1}},
    )
    assert dream_health_note(tmp_path) == ""
