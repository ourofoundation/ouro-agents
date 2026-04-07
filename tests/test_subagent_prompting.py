from ouro_agents.soul import build_shared_prompt_sections, current_datetime_section
from ouro_agents.subagents.context import SubAgentContext
from ouro_agents.subagents.runner import _format_task_context


def test_current_datetime_section_has_expected_fields():
    section = current_datetime_section()

    assert "## CURRENT DATE AND TIME" in section
    assert "Local datetime:" in section
    assert "Current date:" in section
    assert "Weekday:" in section
    assert "UTC datetime:" in section


def test_subagent_task_context_includes_current_datetime(tmp_path):
    ctx = SubAgentContext(
        workspace=tmp_path,
        backend=None,
        agent_id="athena",
        memory_config=None,
        model=None,
    )

    prompt = _format_task_context("Draft the next briefing.", ctx)

    assert prompt.startswith("## CURRENT DATE AND TIME")
    assert "## Task\nDraft the next briefing." in prompt


def test_build_shared_prompt_sections_formats_core_sections():
    sections = build_shared_prompt_sections(
        soul="Be precise.",
        notes="Deployment note.",
        platform_context="You are @athena.",
        user_model="Prefers concise updates.",
        working_memory="Recent anchor post: Day 9.",
        conversation_state="Current topic: Iran-US conflict",
        plans_index="- PLAN:athena:2026-04-06",
    )

    assert sections["soul"] == "## IDENTITY AND RULES (SOUL)\nBe precise."
    assert sections["notes"] == "## DEPLOYMENT CONTEXT (NOTES)\nDeployment note."
    assert sections["platform_context"] == "## PLATFORM CONTEXT\nYou are @athena."
    assert sections["user_model"] == "## USER CONTEXT\nPrefers concise updates."
    assert sections["working_memory"] == "## WORKING MEMORY\nRecent anchor post: Day 9."
    assert sections["conversation_state"] == "## CONVERSATION STATE\nCurrent topic: Iran-US conflict"
    assert sections["plans_index"] == "## PLAN POST INDEX\n- PLAN:athena:2026-04-06"


def test_subagent_task_context_includes_shared_core_sections(tmp_path):
    ctx = SubAgentContext(
        workspace=tmp_path,
        backend=None,
        agent_id="athena",
        memory_config=None,
        model=None,
        soul="Be precise.",
        notes="Deployment note.",
        platform_context="You are @athena.",
        working_memory="Recent anchor post: Day 9.",
        user_model="Prefers concise updates.",
        plans_index="- PLAN:athena:2026-04-06",
    )

    prompt = _format_task_context("Draft the next briefing.", ctx)

    assert "## IDENTITY AND RULES (SOUL)\nBe precise." in prompt
    assert "## DEPLOYMENT CONTEXT (NOTES)\nDeployment note." in prompt
    assert "## PLATFORM CONTEXT\nYou are @athena." in prompt
    assert "## USER CONTEXT\nPrefers concise updates." in prompt
    assert "## PLAN POST INDEX\n- PLAN:athena:2026-04-06" in prompt
    assert "## WORKING MEMORY\nRecent anchor post: Day 9." in prompt
    assert "## Ouro asset placement" in prompt
