"""Tests for heartbeat strategist structured-output parsing."""

from ouro_agents.subagents.strategist import (
    MAX_STRATEGIST_TOOLS,
    format_heartbeat_execution_brief,
    parse_strategist_result,
)


def test_parses_tools_field():
    result = parse_strategist_result(
        '{"objective": "Query the dataset", "worth_remembering": true,'
        ' "briefing": "", "actions": ["query dataset"],'
        ' "tools": ["ouro:query_dataset", "ouro:get_dataset"]}'
    )
    assert result.tools == ["ouro:query_dataset", "ouro:get_dataset"]


def test_tools_default_to_empty():
    result = parse_strategist_result('{"objective": "pass"}')
    assert result.tools == []


def test_tools_are_deduped_and_capped():
    tools = [f"ouro:tool_{i}" for i in range(10)]
    raw = (
        '{"objective": "x", "tools": '
        + str(tools + tools).replace("'", '"')
        + "}"
    )
    result = parse_strategist_result(raw)
    assert result.tools == tools[:MAX_STRATEGIST_TOOLS]


def test_non_list_tools_ignored():
    result = parse_strategist_result(
        '{"objective": "x", "tools": "ouro:get_asset"}'
    )
    assert result.tools == []


def test_parse_failure_returns_defaults():
    result = parse_strategist_result("not json at all")
    assert result.tools == []
    assert result.briefing == "not json at all"


def test_parses_lean_strategist_fields():
    result = parse_strategist_result(
        '{"objective": "Send one sponsor follow-up",'
        ' "selected_priority": 2,'
        ' "priority_audit": ["No live replies in CRM"],'
        ' "worth_remembering": true,'
        ' "briefing": "quest_id=abc team_id=def",'
        ' "actions": ["delegate search: find contact", "send follow-up"],'
        ' "evidence": ["email id"],'
        ' "stop_conditions": ["one email sent"],'
        ' "tools": ["ouro:update_dataset"]}'
    )
    assert result.objective.startswith("Send one")
    assert result.selected_priority == 2
    assert result.priority_audit == ["No live replies in CRM"]
    assert result.actions[0].startswith("delegate search")
    assert result.evidence == ["email id"]
    assert result.has_heartbeat_brief is True
    assert result.should_remember() is True
    assert result.should_log_episode() is True


def test_legacy_plan_and_delegates_fold_into_actions():
    result = parse_strategist_result(
        '{"objective": "Ground outreach",'
        ' "plan": "1. search\\n2. comment",'
        ' "delegates": [{"subagent": "search", "task": "Find 2026 papers on X"}],'
        ' "tools": ["ouro:write_comment"]}'
    )
    assert any("delegate search" in a.lower() for a in result.actions)
    assert "comment" in " ".join(result.actions).lower() or "2. comment" in result.plan


def test_pass_objective_never_worth_remembering():
    result = parse_strategist_result(
        '{"worth_remembering": true,'
        ' "objective": "pass", "actions": ["invent work"],'
        ' "tools": ["ouro:create_post"],'
        ' "prefetch_assets": ["019f8012-d95b-7316-8475-507c3a3f26a4"],'
        ' "memory_notes": ["remember invented work"],'
        ' "briefing": "do more later", "evidence": ["invented"],'
        ' "stop_conditions": ["eventually"],'
        ' "priority_audit": ["nothing due"]}'
    )
    assert result.is_pass_objective is True
    assert result.worth_remembering is False
    assert result.should_remember() is False
    assert result.should_log_episode() is False
    assert result.selected_priority is None
    assert result.actions == []
    assert result.tools == []
    assert result.prefetch_assets == []
    assert result.memory_notes == []
    assert result.briefing == ""
    assert result.evidence == []
    assert result.stop_conditions == []


def test_pass_synonyms():
    for obj in ("pass", "noop", "nothing", "skip", "none"):
        result = parse_strategist_result(
            f'{{"objective": "{obj}", "worth_remembering": true}}'
        )
        assert result.is_pass_objective is True
        assert result.should_remember() is False


def test_format_heartbeat_execution_brief_includes_objective():
    result = parse_strategist_result(
        '{"objective": "Ship one comment", "actions": ["write_comment"],'
        ' "selected_priority": 4,'
        ' "evidence": ["comment id"], "stop_conditions": ["done"]}'
    )
    brief = format_heartbeat_execution_brief(result)
    assert "Ship one comment" in brief
    assert "write_comment" in brief
    assert "Do not invent a second plan" in brief
    assert "### Selected Priority\n4" in brief


def test_priority_audit_is_capped_to_skipped_tiers():
    result = parse_strategist_result(
        '{"objective": "Reach out", "selected_priority": 3,'
        ' "priority_audit": ["Tier 1 clear", "Tier 2 clear",'
        ' "Selected tier rationale", "extra"]}'
    )
    assert result.priority_audit == ["Tier 1 clear", "Tier 2 clear"]


def test_format_pass_brief():
    brief = format_heartbeat_execution_brief(
        parse_strategist_result('{"objective": "pass"}')
    )
    assert "pass" in brief.lower()
    assert "Do not invent work" in brief


def test_format_heartbeat_execution_brief_fallback_when_empty():
    brief = format_heartbeat_execution_brief(parse_strategist_result("{}"))
    assert "did not return a usable plan" in brief


def test_prefetch_assets_keeps_only_valid_uuids_and_caps():
    raw = (
        '{"objective": "x", "prefetch_assets": ['
        '"019F8012-D95B-7316-8475-507C3A3F26A4",'
        ' "not-a-uuid",'
        ' "019f5902-b1eb-7794-b3c9-ada8acfe9d36",'
        ' "019f5902-b1eb-7794-b3c9-ada8acfe9d36",'
        ' "01954d5f-fcea-7970-b8d8-b68879df9d7f",'
        ' "019f8012-d9b0-7eba-a4cd-1f07e7fd91a7"]}'
    )
    result = parse_strategist_result(raw)
    assert result.prefetch_assets == [
        "019f8012-d95b-7316-8475-507c3a3f26a4",
        "019f5902-b1eb-7794-b3c9-ada8acfe9d36",
        "01954d5f-fcea-7970-b8d8-b68879df9d7f",
    ]


def test_prefetch_assets_default_empty():
    result = parse_strategist_result('{"objective": "x"}')
    assert result.prefetch_assets == []


def test_memory_notes_parsed_deduped_and_capped():
    notes = [f"note {i}" for i in range(6)]
    raw = (
        '{"objective": "x", "memory_notes": '
        + str(notes + ["note 1"]).replace("'", '"')
        + "}"
    )
    result = parse_strategist_result(raw)
    assert result.memory_notes == notes[:4]


def test_memory_notes_default_empty():
    result = parse_strategist_result('{"objective": "x"}')
    assert result.memory_notes == []


def test_parses_json_wrapped_in_prose():
    raw = (
        "Based on my analysis, here is the plan.\n"
        '{"objective": "Ship the quest", "briefing": "team=x",'
        ' "actions": ["create quest", "share with controller"],'
        ' "selected_priority": 3,'
        ' "memory_notes": ["New quest id: <new_quest_id>"],'
        ' "tools": ["ouro:create_quest"]}\n'
        "Hope that helps."
    )
    result = parse_strategist_result(raw)
    assert result.objective == "Ship the quest"
    assert result.actions[0] == "create quest"
    assert result.memory_notes == ["New quest id: <new_quest_id>"]
    assert result.tools == ["ouro:create_quest"]


def test_worth_remembering_string_coercion():
    result = parse_strategist_result(
        '{"objective": "x", "worth_remembering": "false"}'
    )
    assert result.worth_remembering is False
