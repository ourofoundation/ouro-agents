"""Tests for preflight structured-output parsing, including tool preloads."""

from ouro_agents.subagents.preflight import (
    MAX_PREFLIGHT_TOOLS,
    format_heartbeat_execution_brief,
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


def test_parses_heartbeat_fields():
    result = parse_preflight_result(
        '{"intent": "research", "complexity": "moderate", "worth_remembering": true,'
        ' "objective": "Ground outreach in latest papers",'
        ' "rationale": "Inbox item needs current citations.",'
        ' "briefing": "quest_id=abc team_id=def",'
        ' "plan": "1. search\\n2. comment",'
        ' "actions": ["search", "comment"],'
        ' "delegates": [{"subagent": "search", "task": "Find 2026 papers on X"}],'
        ' "evidence": ["comment id"],'
        ' "stop_conditions": ["one comment posted"],'
        ' "tools": ["ouro:write_comment"]}'
    )
    assert result.objective.startswith("Ground outreach")
    assert result.delegates == [
        {"subagent": "search", "task": "Find 2026 papers on X"}
    ]
    assert result.evidence == ["comment id"]
    assert result.has_heartbeat_brief is True
    assert result.should_remember() is True


def test_pass_objective_never_worth_remembering():
    result = parse_preflight_result(
        '{"intent": "converse", "complexity": "simple", "worth_remembering": true,'
        ' "objective": "pass", "plan": "", "actions": [], "tools": []}'
    )
    assert result.is_pass_objective is True
    assert result.worth_remembering is True  # model said true…
    assert result.should_remember() is False  # …but pass ticks never reflect


def test_format_heartbeat_execution_brief_includes_objective():
    result = parse_preflight_result(
        '{"objective": "Ship one comment", "actions": ["write_comment"],'
        ' "evidence": ["comment id"], "stop_conditions": ["done"]}'
    )
    brief = format_heartbeat_execution_brief(result)
    assert "Ship one comment" in brief
    assert "write_comment" in brief
    assert "Do not invent a second plan" in brief


def test_format_heartbeat_execution_brief_fallback_when_empty():
    brief = format_heartbeat_execution_brief(parse_preflight_result("{}"))
    assert "did not return a usable plan" in brief


def test_prefetch_assets_keeps_only_valid_uuids_and_caps():
    raw = (
        '{"objective": "x", "prefetch_assets": ['
        '"019F8012-D95B-7316-8475-507C3A3F26A4",'  # valid, mixed case
        ' "not-a-uuid",'
        ' "019f5902-b1eb-7794-b3c9-ada8acfe9d36",'
        ' "019f5902-b1eb-7794-b3c9-ada8acfe9d36",'  # duplicate
        ' "01954d5f-fcea-7970-b8d8-b68879df9d7f",'
        ' "019f8012-d9b0-7eba-a4cd-1f07e7fd91a7"]}'  # over cap
    )
    result = parse_preflight_result(raw)
    assert result.prefetch_assets == [
        "019f8012-d95b-7316-8475-507c3a3f26a4",
        "019f5902-b1eb-7794-b3c9-ada8acfe9d36",
        "01954d5f-fcea-7970-b8d8-b68879df9d7f",
    ]


def test_prefetch_assets_default_empty():
    result = parse_preflight_result('{"objective": "x"}')
    assert result.prefetch_assets == []


def test_memory_notes_parsed_deduped_and_capped():
    notes = [f"note {i}" for i in range(6)]
    raw = (
        '{"objective": "x", "memory_notes": '
        + str(notes + ["note 1"]).replace("'", '"')
        + "}"
    )
    result = parse_preflight_result(raw)
    assert result.memory_notes == notes[:4]


def test_memory_notes_default_empty():
    result = parse_preflight_result('{"objective": "x"}')
    assert result.memory_notes == []


def test_parses_json_wrapped_in_prose():
    raw = (
        "Based on my analysis, here is the plan.\n"
        '{"intent": "create", "complexity": "moderate", "worth_remembering": true,'
        ' "objective": "Ship the quest", "briefing": "team=x",'
        ' "plan": ["create quest", "share with controller"],'
        ' "memory_notes": ["New quest id: <new_quest_id>"],'
        ' "tools": ["ouro:create_quest"]}\n'
        "Hope that helps."
    )
    result = parse_preflight_result(raw)
    assert result.objective == "Ship the quest"
    assert "1. create quest" in result.plan
    assert result.memory_notes == ["New quest id: <new_quest_id>"]
    assert result.tools == ["ouro:create_quest"]
