import json

from ouro_agents.memory.tools import (
    _normalize_forget_specs,
    _normalize_memory_queries,
    _normalize_remember_specs,
)
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


def test_normalize_remember_specs_accepts_single_and_wrapper():
    spec = {
        "text": "Prefer eval-lab",
        "subject_type": "agent",
        "category": "direction",
        "reason": "stated",
    }
    assert _normalize_remember_specs(spec) == [spec]
    assert _normalize_remember_specs({"memories": [spec]}) == [spec]


def test_normalize_forget_specs_accepts_shared_reason_form():
    assert _normalize_forget_specs(
        {"memory_ids": ["a", "b"], "reason": "cleanup"}
    ) == [
        {"memory_id": "a", "reason": "cleanup"},
        {"memory_id": "b", "reason": "cleanup"},
    ]
    assert _normalize_forget_specs(
        {"memory_id": "a", "reason": "gone"}
    ) == [{"memory_id": "a", "reason": "gone"}]


class _FakeBackend:
    def __init__(self):
        self.get_all_calls = []
        self.search_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [
            MemoryResult(text="Keep this direction", category="direction"),
            MemoryResult(text="Drop this episode", category="episode"),
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
                category="episode",
                strength=0.9,
                basis="observed",
                subject_type="asset",
                team_ids=["team-42"],
                asset_ids=["asset-cif"],
                source="team-feed",
            ),
            MemoryResult(
                text="Focus on benchmark quality before chasing newly uploaded assets.",
                score=0.2,
                category="direction",
                strength=0.75,
                basis="stated",
                subject_type="agent",
                team_ids=["team-42"],
                source="plan-feedback",
            ),
        ]


class _FakeDocStore:
    def memory_name(self, agent_name=None):
        return f"MEMORY:{agent_name}:team"

    def log_name(self, agent_name, period):
        return f"LOG:{agent_name}:team:{period}"

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
        memory_categories=["direction"],
    )

    result = _tool_by_name(tools, "memory_recall").forward(
        [{"query": "team strategy"}]
    )

    assert "Keep this direction" in result
    assert "Drop this episode" not in result


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


def test_memory_recall_drops_results_below_signal_floor():
    backend = _RankedBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        user_id="user-1",
        doc_store=_FakeDocStore(),
        team_id="team-42",
        mode="chat",
        min_signal_score=0.5,
    )

    result = _tool_by_name(tools, "memory_recall").forward(
        [{"query": "what should I focus on next"}]
    )

    # The ambient asset episode scores below the floor and is dropped even
    # though the limit would have allowed it.
    assert "Focus on benchmark quality" in result
    assert "2:17 RE-Fe/Co" not in result


def test_memory_recall_keeps_best_hit_when_floor_would_empty_results():
    backend = _RankedBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        user_id="user-1",
        doc_store=_FakeDocStore(),
        team_id="team-42",
        mode="chat",
        min_signal_score=99.0,
    )

    result = _tool_by_name(tools, "memory_recall").forward(
        [{"query": "what should I focus on next"}]
    )

    assert "No relevant memories found" not in result
    assert "Focus on benchmark quality" in result


def test_memory_recall_uses_configured_search_limit_default():
    backend = _FakeBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        doc_store=_FakeDocStore(),
        team_id="team-42",
        search_limit=7,
    )

    _tool_by_name(tools, "memory_recall").forward([{"query": "team strategy"}])

    assert backend.search_calls[0]["limit"] == 7


def test_memory_recall_enforces_global_retrieval_token_budget():
    class _VerboseBackend(_FakeBackend):
        def search(self, **kwargs):
            self.search_calls.append(kwargs)
            return [
                MemoryResult(
                    text=f"Direction memory number {i}: " + "x" * 400,
                    category="direction",
                    basis="stated",
                    strength=0.9,
                    score=0.9,
                )
                for i in range(10)
            ]

    backend = _VerboseBackend()
    tools = make_memory_tools(
        backend,
        agent_id="hermes",
        doc_store=_FakeDocStore(),
        team_id="team-42",
        search_limit=10,
        max_retrieval_tokens=300,  # ~1200 chars; each line is ~430 chars
    )

    result = _tool_by_name(tools, "memory_recall").forward(
        [{"query": "alpha"}, {"query": "beta"}]
    )

    assert "[Recall output truncated" in result
    assert len(result) < 2500


class _MutableBackend(_FakeBackend):
    def __init__(self):
        super().__init__()
        self.deleted = []
        self.updated = []
        self.added = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [MemoryResult(id="mem-1", text="An outdated fact", category="fact")]

    def add(self, text, **kwargs):
        self.added.append({"text": text, **kwargs})

    def delete(self, memory_id):
        self.deleted.append(memory_id)

    def update_text(self, memory_id, text):
        self.updated.append((memory_id, text))


def test_write_tools_only_exposed_when_remember_enabled(tmp_path):
    backend = _MutableBackend()
    read_only = {t.name for t in make_memory_tools(backend, agent_id="hermes")}
    assert read_only == {"memory_recall", "memory_status"}

    writable = {
        t.name
        for t in make_memory_tools(
            backend,
            agent_id="hermes",
            workspace=tmp_path,
            enable_remember=True,
        )
    }
    assert {"remember", "update_memory", "forget", "note_friction"} <= writable


def test_note_friction_uses_run_context_and_queue(tmp_path):
    from ouro_agents.memory.friction import FrictionQueue

    tool = _tool_by_name(
        make_memory_tools(
            _MutableBackend(),
            agent_id="hermes",
            workspace=tmp_path,
            team_id="team-42",
            run_id="run-42",
            mode="autonomous",
            enable_remember=True,
        ),
        "note_friction",
    )

    response = json.loads(
        tool.forward(
            "tool_failure",
            "The route failed twice before succeeding.",
            "med",
            None,
        )
    )

    assert response["status"] == "ok"
    row = FrictionQueue.for_workspace(tmp_path).pending()[0]
    assert row.run_id == "run-42"
    assert row.mode == "autonomous"
    assert row.team_id == "team-42"


def test_recall_surfaces_ids_only_when_writes_enabled():
    backend = _MutableBackend()
    read_only = _tool_by_name(
        make_memory_tools(backend, agent_id="hermes"), "memory_recall"
    ).forward([{"query": "facts"}])
    assert "id=mem-1" not in read_only

    writable = _tool_by_name(
        make_memory_tools(backend, agent_id="hermes", enable_remember=True),
        "memory_recall",
    ).forward([{"query": "facts"}])
    assert "id=mem-1" in writable


def test_forget_batches_deletes_and_requires_reason():
    backend = _MutableBackend()
    forget = _tool_by_name(
        make_memory_tools(backend, agent_id="hermes", enable_remember=True), "forget"
    )

    ok = json.loads(
        forget.forward(
            [
                {"memory_id": "mem-1", "reason": "superseded"},
                {"memory_id": "mem-2", "reason": "outdated"},
            ]
        )
    )
    assert ok["status"] == "ok"
    assert [r["deleted"] for r in ok["results"]] == ["mem-1", "mem-2"]
    assert backend.deleted == ["mem-1", "mem-2"]

    shared = json.loads(
        forget.forward({"memory_ids": ["mem-3", "mem-4"], "reason": "cleanup"})
    )
    assert shared["status"] == "ok"
    assert backend.deleted[-2:] == ["mem-3", "mem-4"]

    err = json.loads(forget.forward([{"memory_id": "mem-1", "reason": ""}]))
    assert err["status"] == "error"
    assert err["results"][0]["error"] == "reason is required"


def test_remember_batches_stores():
    backend = _MutableBackend()
    remember = _tool_by_name(
        make_memory_tools(
            backend,
            agent_id="hermes",
            user_id="user-1",
            enable_remember=True,
        ),
        "remember",
    )

    ok = json.loads(
        remember.forward(
            [
                {
                    "text": "User prefers markdown replies.",
                    "subject_type": "user",
                    "category": "preference",
                    "reason": "explicitly stated",
                },
                {
                    "text": "Publish evals to eval-lab team.",
                    "subject_type": "agent",
                    "category": "direction",
                    "subject_id": "self",
                    "reason": "standing instruction",
                },
            ]
        )
    )
    assert ok["status"] == "ok"
    assert len(ok["results"]) == 2
    assert all(r["status"] == "ok" for r in ok["results"])
    assert len(backend.added) == 2
    assert backend.added[0]["text"] == "User prefers markdown replies."

    partial = json.loads(
        remember.forward(
            [
                {
                    "text": "A valid fact.",
                    "subject_type": "general",
                    "category": "fact",
                    "reason": "observed",
                },
                {
                    "text": "Missing reason.",
                    "subject_type": "general",
                    "category": "fact",
                    "reason": "",
                },
            ]
        )
    )
    assert partial["status"] == "partial"
    assert partial["results"][0]["status"] == "ok"
    assert partial["results"][1]["status"] == "error"


def test_update_memory_rewrites_text_in_place():
    backend = _MutableBackend()
    update = _tool_by_name(
        make_memory_tools(backend, agent_id="hermes", enable_remember=True),
        "update_memory",
    )

    ok = json.loads(update.forward("mem-1", "A corrected fact", "evolved"))
    assert ok == {"status": "ok", "updated": "mem-1"}
    assert backend.updated == [("mem-1", "A corrected fact")]

    err = json.loads(update.forward("mem-1", "", "evolved"))
    assert err["status"] == "error"
