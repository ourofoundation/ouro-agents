"""Tests for preflight structured-output parsing, including tool preloads."""

from ouro_agents.subagents.preflight import (
    MAX_PREFLIGHT_TOOLS,
    parse_preflight_result,
)


def test_parses_tools_field():
    result = parse_preflight_result(
        '{"intent": "analyze", "complexity": "moderate", "worth_remembering": true,'
        ' "briefing": "", "plan": "1. query dataset",'
        ' "tools": ["ouro:query_dataset", "ouro:get_dataset"]}'
    )
    assert result.tools == ["ouro:query_dataset", "ouro:get_dataset"]


def test_tools_default_to_empty():
    result = parse_preflight_result('{"intent": "converse", "complexity": "simple"}')
    assert result.tools == []


def test_tools_are_deduped_and_capped():
    tools = [f"ouro:tool_{i}" for i in range(10)]
    raw = (
        '{"intent": "create", "complexity": "complex", "tools": '
        + str(tools + tools).replace("'", '"')
        + "}"
    )
    result = parse_preflight_result(raw)
    assert result.tools == tools[:MAX_PREFLIGHT_TOOLS]


def test_non_list_tools_ignored():
    result = parse_preflight_result(
        '{"intent": "question", "complexity": "simple", "tools": "ouro:get_asset"}'
    )
    assert result.tools == []


def test_parse_failure_returns_defaults():
    result = parse_preflight_result("not json at all")
    assert result.tools == []
    assert result.briefing == "not json at all"
