from ouro_agents.memory.tools import _normalize_memory_queries
from ouro_agents.memory.tools import make_memory_tools
from ouro_agents.memory import MemoryResult


def test_normalize_memory_queries_accepts_wrapper_dict():
    assert _normalize_memory_queries(
        {"queries": [{"query": "Ce2Fe17 test", "scope": "team"}]}
    ) == [{"query": "Ce2Fe17 test", "scope": "team"}]


def test_normalize_memory_queries_recovers_nested_wrapper_and_drops_noise():
    malformed = [
        {"queries": [{"query": "Ce2Fe17 test", "scope": "team"}]},
        ["reasoning text that should not be treated as a query"],
        {"not_query": "ignored"},
    ]

    assert _normalize_memory_queries(malformed) == [
        {"query": "Ce2Fe17 test", "scope": "team"}
    ]


def test_normalize_memory_queries_accepts_plain_string():
    assert _normalize_memory_queries("materials-science route failures") == [
        {"query": "materials-science route failures"}
    ]


class _FakeBackend:
    def __init__(self):
        self.get_all_calls = []

    def search(self, **kwargs):
        return [
            MemoryResult(text="Keep this decision", category="decision"),
            MemoryResult(text="Drop this observation", category="observation"),
        ]

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        return []


class _FakeDocStore:
    def memory_name(self, agent_name=None):
        return f"MEMORY:{agent_name}:team"

    def daily_name(self, agent_name, day):
        return f"DAILY:{agent_name}:team:{day}"

    def read(self, name):
        return ""


def _tool_by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_memory_status_scopes_vector_counts_to_team_id():
    backend = _FakeBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        user_id="user-1",
        doc_store=_FakeDocStore(),
        team_id="team-42",
    )

    _tool_by_name(tools, "memory_status").forward()

    assert backend.get_all_calls[0]["team_id"] == "team-42"


def test_memory_recall_applies_profile_memory_categories():
    backend = _FakeBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        doc_store=_FakeDocStore(),
        team_id="team-42",
        memory_categories=["decision"],
    )

    result = _tool_by_name(tools, "memory_recall").forward(
        [{"query": "team strategy"}]
    )

    assert "Keep this decision" in result
    assert "Drop this observation" not in result
