"""Tests for shared deferred-tool filtering and delegate dispatch helpers."""

from ouro_agents.security.tool_capabilities import (
    filter_deferred_by_servers,
    filter_deferred_excluding,
    resolve_preload_tools,
)
from ouro_agents.subagents.delegate_utils import (
    dispatch_delegate_tasks,
    dumps_delegate_result,
)


def test_filter_deferred_by_servers():
    tools = {"ouro:get_me": object(), "search:web": object()}
    index = [
        {"tool": "ouro:get_me", "server": "ouro", "raw_name": "get_me"},
        {"tool": "search:web", "server": "search", "raw_name": "web"},
    ]
    filtered_tools, filtered_index = filter_deferred_by_servers(
        tools, index, ["ouro"]
    )
    assert list(filtered_tools) == ["ouro:get_me"]
    assert [item["tool"] for item in filtered_index] == ["ouro:get_me"]


def test_filter_deferred_excluding():
    tools = {"ouro:get_me": object(), "ouro:send_money": object()}
    index = [
        {"tool": "ouro:get_me", "server": "ouro", "raw_name": "get_me"},
        {"tool": "ouro:send_money", "server": "ouro", "raw_name": "send_money"},
    ]
    filtered_tools, filtered_index = filter_deferred_excluding(
        tools, index, ["ouro:send_money"]
    )
    assert list(filtered_tools) == ["ouro:get_me"]
    assert [item["tool"] for item in filtered_index] == ["ouro:get_me"]


def test_resolve_preload_tools_prefers_primary_and_skips_missing():
    primary = {"ouro:create_post": "primary"}
    fallback = {"ouro:get_me": "fallback", "ouro:create_post": "fallback-ignored"}
    index = [
        {"tool": "ouro:create_post", "server": "ouro", "raw_name": "create_post"},
        {"tool": "ouro:get_me", "server": "ouro", "raw_name": "get_me"},
    ]
    tools, raw_names, found = resolve_preload_tools(
        ["ouro:create_post", "ouro:missing", "ouro:get_me"],
        primary=primary,
        fallback=fallback,
        index=index,
    )
    assert tools == ["primary", "fallback"]
    assert raw_names == ["create_post", "get_me"]
    assert found == ["ouro:create_post", "ouro:get_me"]


def test_dispatch_delegate_tasks_sequential_and_parallel():
    calls: list[str] = []

    def run_one(spec: dict) -> dict:
        calls.append(spec["subagent"])
        return {"status": "ok", "subagent": spec["subagent"]}

    tasks = [{"subagent": "a"}, {"subagent": "b"}]
    sequential = dispatch_delegate_tasks(tasks, run_one, parallel=False)
    assert [row["subagent"] for row in sequential] == ["a", "b"]
    assert calls == ["a", "b"]

    calls.clear()
    parallel = dispatch_delegate_tasks(tasks, run_one, parallel=True, max_workers=2)
    assert {row["subagent"] for row in parallel} == {"a", "b"}
    assert set(calls) == {"a", "b"}


def test_dispatch_delegate_tasks_captures_errors():
    def run_one(spec: dict) -> dict:
        raise RuntimeError("boom")

    outputs = dispatch_delegate_tasks(
        [{"subagent": "writer", "return_mode": "full_text"}],
        run_one,
        parallel=False,
    )
    assert outputs[0]["status"] == "error"
    assert outputs[0]["subagent"] == "writer"
    assert "boom" in outputs[0]["error"]


def test_dumps_delegate_result_single_vs_multi():
    single = dumps_delegate_result([{"subagent": "a"}], [{"status": "ok"}])
    assert single == '{"status": "ok"}'
    multi = dumps_delegate_result(
        [{"subagent": "a"}, {"subagent": "b"}],
        [{"status": "ok"}, {"status": "ok"}],
    )
    assert multi.startswith("[")
