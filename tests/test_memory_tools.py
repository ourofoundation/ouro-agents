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
        self.search_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [
            MemoryResult(text="Keep this decision", category="decision"),
            MemoryResult(text="Drop this observation", category="observation"),
        ]

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        return []


class _RankedBackend(_FakeBackend):
    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [
            MemoryResult(
                text="The 2:17 RE-Fe/Co structure family appears in a recent CIF.",
                score=0.99,
                category="observation",
                importance=0.9,
                confidence=0.9,
                subject_type="asset",
                team_ids=["team-42"],
                asset_ids=["asset-cif"],
                source="team-feed",
            ),
            MemoryResult(
                text="Focus on benchmark quality before chasing newly uploaded assets.",
                score=0.2,
                category="direction",
                importance=0.75,
                confidence=0.8,
                subject_type="agent",
                team_ids=["team-42"],
                source="plan-feedback",
            ),
        ]


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


def test_memory_recall_accepts_top_level_scope_defaults():
    backend = _FakeBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        doc_store=_FakeDocStore(),
        team_id="team-42",
    )

    _tool_by_name(tools, "memory_recall").forward(
        [{"query": "Ouro platform features"}],
        scope="global",
        category="fact",
        limit=3,
    )

    assert backend.search_calls[0]["scope"] == "global"
    assert backend.search_calls[0]["category"] == "fact"
    assert backend.search_calls[0]["limit"] == 3


def test_memory_recall_keeps_per_query_scope_over_top_level_default():
    backend = _FakeBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        doc_store=_FakeDocStore(),
        team_id="team-42",
    )

    _tool_by_name(tools, "memory_recall").forward(
        [{"query": "team strategy", "scope": "team"}],
        scope="global",
    )

    assert backend.search_calls[0]["scope"] == "team"


def test_memory_recall_pushes_per_query_category_into_backend_search():
    backend = _FakeBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        doc_store=_FakeDocStore(),
        team_id="team-42",
    )

    _tool_by_name(tools, "memory_recall").forward(
        [{"query": "Ce2Fe17 CIF file", "category": "fact"}],
        scope="global",
    )

    assert backend.search_calls[0]["scope"] == "global"
    assert backend.search_calls[0]["category"] == "fact"


def test_memory_recall_ranks_direction_above_ambient_asset_observation():
    backend = _RankedBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        user_id="user-1",
        doc_store=_FakeDocStore(),
        team_id="team-42",
        mode="chat",
    )

    result = _tool_by_name(tools, "memory_recall").forward(
        [{"query": "what should I focus on next", "limit": 1}]
    )

    assert "Focus on benchmark quality" in result
    assert "2:17 RE-Fe/Co" not in result
