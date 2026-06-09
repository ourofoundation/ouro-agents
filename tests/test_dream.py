import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ouro_agents.config import MemoryConfig
from ouro_agents.memory import MemoryResult
from ouro_agents.memory.dream import (
    _CONFIDENCE_DECAY_PERIOD_KEY,
    _IMPORTANCE_DECAY_PERIOD_KEY,
    _REVIEW_PERIOD_KEY,
    decay_old_memories,
    decay_stale_confidence,
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
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    memories = [
        MemoryResult(
            id="mem-1",
            text="Old volatile observation",
            category="observation",
            importance=0.8,
            confidence=0.8,
            volatility=0.8,
            created_at=old,
        )
    ]

    importance_count = decay_old_memories(
        backend,
        "hermes",
        _memory_config(),
        team_id="team-42",
        all_memories=memories,
    )
    confidence_count = decay_stale_confidence(
        backend,
        "hermes",
        _memory_config(),
        team_id="team-42",
        all_memories=memories,
    )

    assert importance_count == 1
    assert confidence_count == 1
    assert memories[0].importance == 0.4
    assert memories[0].confidence < 0.5
    assert backend.updated[0][1][_IMPORTANCE_DECAY_PERIOD_KEY] == period_key("daily")
    assert backend.updated[1][1][_CONFIDENCE_DECAY_PERIOD_KEY] == period_key("daily")


def test_decay_skips_when_period_marker_already_set():
    current_period = period_key("daily")
    backend = _UpdateBackend()
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    memories = [
        MemoryResult(
            id="mem-1",
            text="Already decayed",
            category="observation",
            importance=0.8,
            created_at=old,
            metadata={_IMPORTANCE_DECAY_PERIOD_KEY: current_period},
        )
    ]

    count = decay_old_memories(
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
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-1",
                text="Old volatile observation",
                category="observation",
                importance=0.8,
                confidence=0.8,
                volatility=0.8,
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
    assert summary["importance_decayed"] == 1
    assert summary["confidence_decayed"] == 1
    assert summary["dream_review"]["reviewed"] == 1
    assert backend.updated == []
    assert backend.deleted == []
    audit_path = Path(summary["audit_log"])
    audit = json.loads(audit_path.read_text())
    assert audit["dry_run"] is True
    assert {op["status"] for op in audit["operations"]} == {"planned"}


def test_run_dream_reviews_confidence_decay_in_same_cycle(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-1",
                text="Old volatile observation",
                category="observation",
                importance=0.8,
                confidence=0.8,
                volatility=0.8,
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
    )

    assert summary["confidence_decayed"] == 1
    assert summary["dream_review"]["confirmed"] == 1
    assert backend.updated[-1] == (
        "mem-1",
        {
            "confidence": 0.8,
            "last_verified": backend.updated[-1][1]["last_verified"],
            _REVIEW_PERIOD_KEY: period_key("daily"),
            "last_review_action": "keep",
            "last_review_evidence": "none",
            "last_review_requested_status": "confirmed",
            "last_review_status": "confirmed",
            "last_review_reason": "test",
        },
    )


def test_dream_review_does_not_delete_api_failure_without_explicit_evidence(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-api",
                text="ALIGNN Modal upstream is returning 500 errors",
                category="observation",
                importance=0.8,
                confidence=0.2,
                volatility=0.8,
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
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    backend = _UpdateBackend(
        [
            MemoryResult(
                id="mem-api",
                text="ALIGNN Modal upstream is returning 500 errors",
                category="observation",
                importance=0.8,
                confidence=0.2,
                volatility=0.8,
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
