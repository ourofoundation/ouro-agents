from datetime import datetime, timedelta, timezone

from ouro_agents.config import MemoryConfig
from ouro_agents.memory import MemoryResult
from ouro_agents.memory.dream import _IMPORTANCE_DECAY_PERIOD_KEY, decay_old_memories
from ouro_agents.memory.naming import period_key


class _FakeBackend:
    def __init__(self):
        self.get_all_calls = []
        self.updated = []

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        created = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        return [
            MemoryResult(
                id="mem-1",
                text="Old team-specific learning",
                importance=0.8,
                created_at=created,
            )
        ]

    def update_metadata(self, memory_id, metadata):
        self.updated.append((memory_id, metadata))


def _config():
    return MemoryConfig(
        extraction_model="test-model",
        embedder="test-embedder",
        decay_after_days=30,
    )


def test_decay_skips_unscoped_shared_pass():
    backend = _FakeBackend()

    count = decay_old_memories(backend, "hermes", _config())

    assert count == 0
    assert backend.get_all_calls == []
    assert backend.updated == []


def test_decay_filters_by_team_and_updates_memory_id():
    backend = _FakeBackend()

    count = decay_old_memories(backend, "hermes", _config(), team_id="team-42")

    assert count == 1
    assert backend.get_all_calls[0]["team_id"] == "team-42"
    assert backend.updated == [
        (
            "mem-1",
            {
                "importance": 0.4,
                _IMPORTANCE_DECAY_PERIOD_KEY: period_key("daily"),
            },
        )
    ]


def test_decay_preserves_direction_memories():
    class DirectionBackend(_FakeBackend):
        def get_all(self, **kwargs):
            self.get_all_calls.append(kwargs)
            created = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
            return [
                MemoryResult(
                    id="mem-direction",
                    text="Always prioritize benchmark quality.",
                    category="direction",
                    importance=0.8,
                    created_at=created,
                )
            ]

    backend = DirectionBackend()

    count = decay_old_memories(backend, "hermes", _config(), team_id="team-42")

    assert count == 0
    assert backend.updated == []
