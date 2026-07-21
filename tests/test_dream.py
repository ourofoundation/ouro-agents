import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ouro_agents.config import MemoryConfig
from ouro_agents.memory import MemoryResult
from ouro_agents.memory.dream import (
    _REVIEW_PERIOD_KEY,
    _STRENGTH_DECAY_PERIOD_KEY,
    decay_memory_strength,
    distill_skills,
    has_recent_dream_activity,
    promote_log_entries,
    run_dream,
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
                        "entry": "Verify dream promotions land in working memory.",
                    }
                ]
            )
        )


class _UpdateBackend:
    def __init__(self, memories=None):
        self.memories = memories or []
        self.get_all_calls = []
        self.updated = []
        self.deleted = []

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        return self.memories

    def update_metadata(self, memory_id, metadata):
        self.updated.append((memory_id, metadata))

    def delete(self, memory_id):
        self.deleted.append(memory_id)


class _ReviewModel:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def __call__(self, messages):
        system = messages[0]["content"]
        if "reviewing an agent's stored memories" in system:
            return SimpleNamespace(content=json.dumps(self.verdicts))
        if "memory curator" in system and "rewrite" in system:
            return SimpleNamespace(content="# Memory\n\n## Facts\n- Compacted.")
        return _PromotionModel()(messages)


def _memory_config():
    return MemoryConfig(
        extraction_model="test-model",
        embedder="test-embedder",
        decay_after_days=30,
    )


def test_promote_log_entries_handles_final_section_without_trailing_newline(tmp_path: Path):
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    memory_name = store.memory_name("hermes")
    log_name = store.log_name("hermes", period_key_offset("daily", -1))

    store.write(memory_name, "# Memory\n\n## Facts\n- Existing fact.\n\n## Learnings")
    store.write(log_name, "# Daily Log\n\n- Useful durable lesson from the prior period.\n")

    promoted = promote_log_entries(
        tmp_path,
        _PromotionModel(),
        doc_store=store,
        agent_name="hermes",
    )

    assert promoted == 1
    assert (
        "## Learnings\n- Verify dream promotions land in working memory."
        in store.read(memory_name)
    )


def test_promote_log_entries_uses_daily_logs_for_weekly_fallback(tmp_path: Path):
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="weekly")
    memory_name = store.memory_name("hermes")
    previous_week = period_key_offset("weekly", -1)
    year_text, week_text = previous_week.split("-W", 1)
    previous_monday = date.fromisocalendar(int(year_text), int(week_text), 1)
    daily_key = previous_monday.isoformat()
    daily_log_name = store.log_name("hermes", daily_key)

    store.write(memory_name, "# Memory\n\n## Facts\n- Existing fact.\n\n## Learnings")
    store.write(daily_log_name, "# Daily Log\n\n- Useful durable lesson from the prior week.\n")

    promoted = promote_log_entries(
        tmp_path,
        _PromotionModel(),
        doc_store=store,
        agent_name="hermes",
    )

    assert promoted == 1
    assert "Verify dream promotions land in working memory." in store.read(memory_name)
    assert has_recent_dream_activity(store, "hermes", "weekly")


def test_decay_passes_update_shared_memory_snapshot():
    backend = _UpdateBackend()
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    memories = [
        MemoryResult(
            id="mem-1",
            text="Old evolving fact",
            category="fact",
            strength=0.8,
            stability="evolving",
            created_at=old,
        )
    ]

    count = decay_memory_strength(
        backend,
        "hermes",
        _memory_config(),
        team_id="team-42",
        all_memories=memories,
    )

    assert count == 1
    assert memories[0].strength < 0.8
    assert backend.updated[0][1][_STRENGTH_DECAY_PERIOD_KEY] == period_key("daily")


def test_decay_skips_when_period_marker_already_set():
    current_period = period_key("daily")
    backend = _UpdateBackend()
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    memories = [
        MemoryResult(
            id="mem-1",
            text="Already decayed",
                category="fact",
                strength=0.8,
            created_at=old,
                metadata={_STRENGTH_DECAY_PERIOD_KEY: current_period},
        )
    ]

    count = decay_memory_strength(
        backend,
        "hermes",
        _memory_config(),
        team_id="team-42",
        all_memories=memories,
        period=current_period,
    )

    assert count == 0
    assert backend.updated == []


def test_run_dream_dry_run_plans_without_backend_mutation_and_writes_audit(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-1",
                text="Old evolving fact",
                category="fact",
                strength=0.8,
                stability="evolving",
                created_at=old,
            )
        ]
    )
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Existing fact.")

    summary = run_dream(
        tmp_path,
        backend,
        "hermes",
        _memory_config(),
        _ReviewModel([{"id": "mem-1", "status": "uncertain", "reason": "test"}]),
        doc_store=store,
        team_id="team-42",
        dry_run=True,
    )

    assert summary["dry_run"] is True
    assert summary["strength_decayed"] == 1
    assert summary["dream_review"]["reviewed"] == 1
    assert backend.updated == []
    assert backend.deleted == []
    audit_path = Path(summary["audit_log"])
    audit = json.loads(audit_path.read_text())
    assert audit["dry_run"] is True
    assert {op["status"] for op in audit["operations"]} == {"planned"}
    assert any(call["phase"] == "dream_review" for call in audit["llm_calls"])
    review_call = next(c for c in audit["llm_calls"] if c["phase"] == "dream_review")
    assert review_call["response_chars"] > 0
    assert "system" in review_call and "user" in review_call
    assert summary["llm_calls"] >= 1


def test_run_dream_reviews_evolving_memory_in_same_cycle(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-1",
                text="Old evolving fact",
                category="fact",
                strength=0.8,
                stability="evolving",
                created_at=old,
            )
        ]
    )
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Existing fact.")

    summary = run_dream(
        tmp_path,
        backend,
        "hermes",
        _memory_config(),
        _ReviewModel([{"id": "mem-1", "status": "confirmed", "reason": "test"}]),
        doc_store=store,
        team_id="team-42",
        run_id="run-test-review-1",
    )

    assert summary["strength_decayed"] == 1
    assert summary["dream_review"]["confirmed"] == 1
    assert summary["run_id"] == "run-test-review-1"
    audit = json.loads(Path(summary["audit_log"]).read_text())
    assert audit["run_id"] == "run-test-review-1"
    assert any(c["phase"] == "dream_review" for c in audit["llm_calls"])
    assert backend.updated[-1] == (
        "mem-1",
        {
            "last_verified": backend.updated[-1][1]["last_verified"],
            "stability": "stable",
            _REVIEW_PERIOD_KEY: period_key("daily"),
            "last_review_action": "keep",
            "last_review_evidence": "none",
            "last_review_requested_status": "confirmed",
            "last_review_status": "confirmed",
            "last_review_reason": "test",
        },
    )


def test_dream_review_does_not_delete_api_failure_without_explicit_evidence(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-api",
                text="ALIGNN Modal upstream is returning 500 errors",
                category="fact",
                strength=0.8,
                stability="evolving",
                created_at=old,
            )
        ]
    )
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Existing fact.")

    summary = run_dream(
        tmp_path,
        backend,
        "hermes",
        _memory_config(),
        _ReviewModel(
            [
                {
                    "id": "mem-api",
                    "status": "contradicted",
                    "evidence": "none",
                    "action": "delete",
                    "reason": "old outage likely recovered",
                    "replacement": "ALIGNN is available.",
                }
            ]
        ),
        doc_store=store,
        team_id="team-42",
    )

    assert summary["dream_review"]["contradicted"] == 0
    assert summary["dream_review"]["uncertain"] == 1
    assert backend.deleted == []
    assert backend.updated[-1][0] == "mem-api"
    assert backend.updated[-1][1]["last_review_requested_status"] == "contradicted"
    assert backend.updated[-1][1]["last_review_status"] == "uncertain"
    assert backend.updated[-1][1]["last_review_evidence"] == "none"


def test_dream_review_can_delete_with_explicit_evidence(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-api",
                text="ALIGNN Modal upstream is returning 500 errors",
                category="fact",
                strength=0.8,
                stability="evolving",
                created_at=old,
            )
        ]
    )
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Existing fact.")

    summary = run_dream(
        tmp_path,
        backend,
        "hermes",
        _memory_config(),
        _ReviewModel(
            [
                {
                    "id": "mem-api",
                    "status": "contradicted",
                    "evidence": "route_probe",
                    "action": "delete",
                    "reason": "new route probe succeeded",
                    "replacement": "ALIGNN Modal upstream is responding successfully.",
                }
            ]
        ),
        doc_store=store,
        team_id="team-42",
    )

    assert summary["dream_review"]["contradicted"] == 1
    assert summary["dream_review"]["uncertain"] == 0
    assert backend.deleted == ["mem-api"]


def test_dream_review_second_uncertain_without_evidence_marks_stale(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-churn",
                text="Old decision with low confidence",
                category="decision",
                strength=0.6,
                stability="evolving",
                created_at=old,
                metadata={"last_review_status": "uncertain"},
            )
        ]
    )
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Existing fact.")

    summary = run_dream(
        tmp_path,
        backend,
        "hermes",
        _memory_config(),
        _ReviewModel(
            [
                {
                    "id": "mem-churn",
                    "status": "uncertain",
                    "evidence": "none",
                    "action": "keep",
                    "reason": "still no evidence",
                }
            ]
        ),
        doc_store=store,
        team_id="team-42",
    )

    assert summary["dream_review"]["uncertain"] == 1
    metadata = backend.updated[-1][1]
    assert backend.updated[-1][0] == "mem-churn"
    assert metadata["last_review_action"] == "mark_stale"
    # mark_stale halves strength and flips stability so the memory leaves
    # the review candidate pool next period.
    assert metadata["stability"] == "stable"
    # Strength decay runs earlier in the cycle, so just verify the review
    # halved whatever remained (floored at 0.1) rather than pin an exact value.
    assert 0.1 <= metadata["strength"] < 0.6


def test_dream_review_first_uncertain_does_not_force_stale(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-first",
                text="Old decision with low confidence",
                category="decision",
                strength=0.6,
                stability="evolving",
                created_at=old,
            )
        ]
    )
    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Existing fact.")

    run_dream(
        tmp_path,
        backend,
        "hermes",
        _memory_config(),
        _ReviewModel(
            [
                {
                    "id": "mem-first",
                    "status": "uncertain",
                    "evidence": "none",
                    "action": "keep",
                    "reason": "no evidence yet",
                }
            ]
        ),
        doc_store=store,
        team_id="team-42",
    )

    metadata = backend.updated[-1][1]
    assert metadata["last_review_action"] == "keep"
    assert "stability" not in metadata


class _DistillModel:
    def __init__(self, proposals):
        self.proposals = proposals

    def __call__(self, messages):
        return SimpleNamespace(content=json.dumps(self.proposals))


def _reinforced_direction(memory_id="mem-dir", metadata=None):
    now = datetime.now(timezone.utc).isoformat()
    return MemoryResult(
        id=memory_id,
        text="Always publish benchmark results to the eval-lab team.",
        category="direction",
        strength=0.8,
        created_at=now,
        last_accessed=now,
        metadata=metadata or {},
    )


def test_distill_skills_writes_lesson_file_and_marks_memory(tmp_path: Path):
    backend = _UpdateBackend()
    model = _DistillModel(
        [
            {
                "topic": "publishing",
                "memory_ids": ["mem-dir"],
                "lesson": "Publish benchmark results to the eval-lab team.",
            }
        ]
    )

    written = distill_skills(
        tmp_path, backend, model, all_memories=[_reinforced_direction()]
    )

    assert written == 1
    skill_path = tmp_path / "skills" / "lessons-publishing.md"
    content = skill_path.read_text()
    assert "Publish benchmark results to the eval-lab team." in content
    assert "description: Learned lessons about publishing" in content
    assert backend.updated == [("mem-dir", {"distilled_to_skill": "lessons-publishing"})]


def test_distill_skills_appends_to_existing_topic(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "lessons-publishing.md").write_text(
        "---\ndescription: Learned lessons about publishing (distilled from memory)\n"
        "load: stub\n---\n\n# Lessons: publishing\n\n- Existing lesson.\n"
    )
    backend = _UpdateBackend()
    model = _DistillModel(
        [
            {
                "topic": "publishing",
                "memory_ids": ["mem-dir"],
                "lesson": "New lesson about benchmarks.",
            }
        ]
    )

    written = distill_skills(
        tmp_path, backend, model, all_memories=[_reinforced_direction()]
    )

    assert written == 1
    content = (skills_dir / "lessons-publishing.md").read_text()
    assert "- Existing lesson.\n- New lesson about benchmarks.\n" in content


def test_distill_skills_skips_unreinforced_and_already_distilled(tmp_path: Path):
    backend = _UpdateBackend()
    never_recalled = _reinforced_direction("mem-a")
    never_recalled.last_accessed = ""
    already_distilled = _reinforced_direction(
        "mem-b", metadata={"distilled_to_skill": "lessons-publishing"}
    )

    written = distill_skills(
        tmp_path,
        backend,
        _DistillModel([]),
        all_memories=[never_recalled, already_distilled],
    )

    assert written == 0
    assert not (tmp_path / "skills").exists()


def test_distill_skills_dry_run_plans_without_writing(tmp_path: Path):
    from ouro_agents.memory.dream import DreamPlan

    backend = _UpdateBackend()
    plan = DreamPlan(
        scope="team-42", agent_id="hermes", team_id="team-42",
        rhythm="daily", period=period_key("daily"), dry_run=True,
    )
    model = _DistillModel(
        [
            {
                "topic": "publishing",
                "memory_ids": ["mem-dir"],
                "lesson": "Publish benchmark results to the eval-lab team.",
            }
        ]
    )

    written = distill_skills(
        tmp_path,
        backend,
        model,
        all_memories=[_reinforced_direction()],
        dry_run=True,
        plan=plan,
    )

    assert written == 1
    assert not (tmp_path / "skills").exists()
    assert backend.updated == []
    assert plan.operations[0].kind == "skill_distillation"
    assert plan.operations[0].status == "planned"


def test_scope_has_dream_work_skips_empty_scope(tmp_path: Path):
    from ouro_agents.memory.dream import scope_has_dream_work

    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    backend = _UpdateBackend([])

    # Trivial log, empty memory doc, zero vector memories -> skip
    store.write(store.log_name("hermes", period_key("daily")), "# Log\n")
    assert not scope_has_dream_work(store, backend, "hermes", "daily", team_id="team-42")

    # Meaningful log content -> run
    store.write(
        store.log_name("hermes", period_key("daily")),
        "# Log\n\n- Durable lesson worth promoting later.\n",
    )
    assert scope_has_dream_work(store, backend, "hermes", "daily", team_id="team-42")


def test_scope_has_dream_work_runs_when_memories_exist(tmp_path: Path):
    from ouro_agents.memory.dream import scope_has_dream_work

    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    backend = _UpdateBackend([_reinforced_direction()])
    assert scope_has_dream_work(store, backend, "hermes", "daily", team_id="team-42")

    # Non-empty memory doc alone is also enough
    empty_backend = _UpdateBackend([])
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Something.")
    assert scope_has_dream_work(store, empty_backend, "hermes", "daily", team_id="team-42")


def test_dream_status_surfaces_failures(tmp_path: Path):
    from ouro_agents.memory.dream import (
        dream_health_note,
        read_dream_status,
        write_dream_status,
    )

    assert dream_health_note(tmp_path) == ""

    write_dream_status(
        tmp_path,
        "2026-W29",
        {
            "shared": {"promoted": 0},
            "team-42": {
                "promoted": 0,
                "failures": ["Log promotion failed: Error code: 402"],
            },
        },
    )

    status = read_dream_status(tmp_path)
    assert status["scopes_run"] == 2
    assert status["scopes_with_failures"] == 1

    note = dream_health_note(tmp_path)
    assert "2026-W29" in note
    assert "1 of 2 scopes" in note
    assert "402" in note

    # Healthy cycle clears the note
    write_dream_status(tmp_path, "2026-W30", {"shared": {"promoted": 1}})
    assert dream_health_note(tmp_path) == ""


def test_run_dream_summary_includes_failures(tmp_path: Path):
    class _FailingModel:
        def __call__(self, messages):
            raise RuntimeError("Error code: 402 - insufficient credits")

    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    store.write(store.memory_name("hermes"), "# Memory\n\n## Facts\n- Existing fact.")
    store.write(
        store.log_name("hermes", period_key_offset("daily", -1)),
        "# Log\n\n- Durable lesson worth promoting.\n",
    )

    summary = run_dream(
        tmp_path,
        _UpdateBackend([]),
        "hermes",
        _memory_config(),
        _FailingModel(),
        doc_store=store,
        team_id="team-42",
    )

    assert any("402" in f for f in summary.get("failures", []))


def test_run_dream_scope_writes_run_log_with_usage(tmp_path: Path, monkeypatch):
    """Dream scopes must land in runs.db like other modes (tokens/cost included)."""
    from ouro_agents.agent import OuroAgent
    from ouro_agents.config import MemoryConfig, RunLogConfig
    from ouro_agents.run_log import RunLogStore
    from ouro_agents.usage import UsageTracker

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = RunLogStore(workspace / "runs.db")
    trackers: list[UsageTracker] = []

    def fake_run_dream(**kwargs):
        assert kwargs.get("run_id"), "dream ledgering must pass run_id into run_dream"
        tracker = trackers[-1]
        tracker.record(
            "dream-gen-1",
            {
                "input_tokens": 120,
                "output_tokens": 40,
                "cost_usd": 0.0025,
            },
        )
        return {
            "compacted": False,
            "promoted": 1,
            "outcome_lessons": 0,
            "skills_distilled": 0,
            "strength_decayed": 0,
            "memories_deleted": 0,
            "refinement": {
                "pending": 0,
                "edits": 0,
                "memory_deletes": 0,
                "queue_applied": 0,
            },
            "dream_review": {
                "reviewed": 0,
                "confirmed": 0,
                "contradicted": 0,
                "uncertain": 0,
            },
            "comments_merged": 0,
            "run_id": kwargs["run_id"],
            "llm_calls": 0,
        }

    monkeypatch.setattr("ouro_agents.memory.dream.run_dream", fake_run_dream)

    agent = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(name="hermes", workspace=workspace),
            memory=MemoryConfig(
                extraction_model="test-model",
                embedder="test-embedder",
            ),
            run_log=RunLogConfig(enabled=True, path=workspace / "runs.db"),
        ),
        _current_run_id=None,
        _current_tick_id=None,
        _run_log=store,
        memory=SimpleNamespace(usage_ledger=lambda: None),
        doc_store=None,
    )

    def _build_model(model_id, **kwargs):
        tracker = kwargs.get("usage_tracker") or UsageTracker()
        trackers.append(tracker)
        return SimpleNamespace(model_id=model_id)

    agent._build_model = _build_model
    agent._utility_model_id = lambda: "test/utility"
    agent._finalize_run_record = lambda record: store.write(record)

    summary = OuroAgent._run_dream_scope(
        agent,
        team_id="team-42",
        mode="manual",
        tick_id="tick-dream-1",
        doc_store=SimpleNamespace(),
    )

    assert summary["promoted"] == 1
    rows = store.query_runs(mode="dream", tick_id="tick-dream-1")
    assert len(rows) == 1
    row = rows[0]
    assert summary["run_id"] == row["run_id"]
    assert row["mode"] == "dream"
    assert row["status"] == "success"
    assert row["team_id"] == "team-42"
    assert row["tick_id"] == "tick-dream-1"
    assert row["model"] == "test/utility"
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 40
    assert row["total_tokens"] == 160
    assert abs(float(row["cost_usd"]) - 0.0025) < 1e-9
    assert "dream [manual] scope=team-42" in row["task"]
    store.close()


def test_promote_log_entries_records_llm_call_and_entry_detail(tmp_path: Path):
    from ouro_agents.memory.dream import DreamPlan

    store = LocalDocStore(tmp_path, agent_name="hermes", team_id="team-42", rhythm="daily")
    memory_name = store.memory_name("hermes")
    log_name = store.log_name("hermes", period_key_offset("daily", -1))
    store.write(memory_name, "# Memory\n\n## Facts\n- Existing fact.\n\n## Learnings\n")
    store.write(log_name, "# Daily Log\n\n- Useful durable lesson from the prior period.\n")

    plan = DreamPlan(
        scope="team-42",
        agent_id="hermes",
        team_id="team-42",
        rhythm="daily",
        period=period_key("daily"),
        mode="manual",
    )
    promoted = promote_log_entries(
        tmp_path,
        _PromotionModel(),
        doc_store=store,
        agent_name="hermes",
        plan=plan,
    )

    assert promoted == 1
    assert len(plan.llm_calls) == 1
    assert plan.llm_calls[0]["phase"] == "promotion"
    assert plan.llm_calls[0]["response_chars"] > 0
    promo_ops = [op for op in plan.operations if op.kind == "promotion"]
    assert len(promo_ops) == 1
    assert promo_ops[0].detail["entries"]
    assert promo_ops[0].detail["entries"][0]["section"] == "Learnings"


def test_outcome_lessons_records_operation(tmp_path: Path, monkeypatch):
    from ouro_agents.memory.dream import DreamPlan, _store_outcome_lessons

    stored = []

    def fake_remember(backend, agent_id, text, **kwargs):
        stored.append(text)
        return True

    monkeypatch.setattr(
        "ouro_agents.memory.focus.remember_work_direction",
        fake_remember,
    )
    monkeypatch.setattr(
        "ouro_agents.modes.outcomes.build_outcome_evidence_context",
        lambda agent, limit=8: (
            "Quest A: 0 external comments, 0 quality views\n"
            "Quest B: 0 external comments, 0 quality views"
        ),
    )

    plan = DreamPlan(
        scope="team-42",
        agent_id="hermes",
        team_id="team-42",
        rhythm="daily",
        period=period_key("daily"),
    )
    count = _store_outcome_lessons(
        SimpleNamespace(config=SimpleNamespace()),
        agent_id="hermes",
        backend=_UpdateBackend([]),
        team_id="team-42",
        dry_run=False,
        plan=plan,
    )

    assert count == 1
    assert stored
    ops = [op for op in plan.operations if op.kind == "outcome_lessons"]
    assert len(ops) == 1
    assert ops[0].reason == "low_engagement_constraint"
    assert ops[0].detail["zeroish_signals"] >= 2
    assert "external engagement" in ops[0].excerpt.lower()
