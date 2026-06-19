from ouro_agents.cli.memory import MemoryFilters, fetch_memories
from ouro_agents.memory import MemoryResult
from ouro_agents.tui.memory_browser import clamp_index, index_after_delete


class _FakeBackend:
    def __init__(self, memories: list[MemoryResult]):
        self.memories = list(memories)
        self.deleted: list[str] = []
        self.updated: list[tuple[str, str]] = []

    def get_all(self, **kwargs):
        out = list(self.memories)
        if kwargs.get("category"):
            out = [m for m in out if m.category == kwargs["category"]]
        if kwargs.get("team_id"):
            team = kwargs["team_id"]
            out = [m for m in out if team in m.team_ids or m.team_id == team]
        out.sort(key=lambda m: m.strength)
        return out[: kwargs.get("limit", 100)]

    def update_text(self, memory_id, text):
        self.updated.append((memory_id, text))
        for m in self.memories:
            if m.id == memory_id:
                m.text = text

    def delete(self, memory_id):
        self.deleted.append(memory_id)
        self.memories = [m for m in self.memories if m.id != memory_id]


def test_fetch_memories_sorts_weakest_first_and_applies_grep():
    backend = _FakeBackend(
        [
            MemoryResult(id="a", text="Strong fact", strength=0.9, category="fact"),
            MemoryResult(id="b", text="Weak preference", strength=0.1, category="preference"),
        ]
    )
    rows = fetch_memories(
        backend,
        "agent",
        MemoryFilters(grep="preference", limit=10),
    )
    assert [m.id for m in rows] == ["b"]


def test_clamp_index():
    assert clamp_index(0, 0) == 0
    assert clamp_index(5, 3) == 2
    assert clamp_index(-1, 3) == 0


def test_index_after_delete():
    assert index_after_delete(2, 0) == 0
    assert index_after_delete(2, 3) == 2
    assert index_after_delete(4, 4) == 3
