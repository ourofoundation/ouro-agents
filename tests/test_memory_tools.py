from ouro_agents.memory.tools import _normalize_memory_queries


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
