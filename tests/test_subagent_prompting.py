from ouro_agents.config import OuroAgentsConfig
from ouro_agents.soul import build_prompt, build_shared_prompt_sections, current_datetime_section
from ouro_agents.modes.framing import CHAT_FRAMING, HEARTBEAT_FRAMING
from ouro_agents.modes.profiles import CHAT, HEARTBEAT, PLAN, REVIEW
from ouro_agents.skills import load_startup_skills, resolve_skill, resolve_skills
from ouro_agents.subagents.context import SubAgentContext
from ouro_agents.subagents.strategist import STRATEGIST_PROMPT
from ouro_agents.subagents.prompts import DEVELOPER_PROMPT, EXECUTOR_PROMPT
from ouro_agents.subagents.profiles import (
    DEVELOPER,
    EXECUTOR,
    RESEARCH,
    SEARCH,
    STRATEGIST,
    WRITER,
)
from ouro_agents.subagents.runner import _format_task_context
from ouro_agents.tool_prompt import build_tool_calling_system_prompt


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
    assert sections["plans_index"] == "## PLAN QUEST INDEX\n- PLAN:athena:2026-04-06"


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
    assert "## PLAN QUEST INDEX\n- PLAN:athena:2026-04-06" in prompt
    assert "## WORKING MEMORY\nRecent anchor post: Day 9." in prompt
    assert "## Ouro asset placement" in prompt


def test_strategist_task_context_includes_working_memory(tmp_path):
    ctx = SubAgentContext(
        workspace=tmp_path,
        backend=None,
        agent_id="athena",
        memory_config=None,
        model=None,
        soul="Do the work.",
        notes="Deployment note.",
        platform_context="You are @athena.",
        working_memory="Recent anchor post: Day 9.",
        user_model="Prefers concise updates.",
        plans_index="- PLAN:athena:2026-04-06",
    )

    prompt = _format_task_context(
        "Choose one heartbeat objective.",
        ctx,
        shared_context_sections=STRATEGIST.shared_context_sections,
        include_asset_placement=STRATEGIST.include_asset_placement,
    )

    assert "## CURRENT DATE AND TIME" in prompt
    assert "## PLATFORM CONTEXT\nYou are @athena." in prompt
    assert "## WORKING MEMORY\nRecent anchor post: Day 9." in prompt
    assert "## PLAN QUEST INDEX\n- PLAN:athena:2026-04-06" in prompt
    assert "## Task\nChoose one heartbeat objective." in prompt
    assert "## IDENTITY AND RULES (SOUL)" not in prompt
    assert "## USER CONTEXT" not in prompt
    assert "## DEPLOYMENT CONTEXT" not in prompt
    assert "## Ouro asset placement" not in prompt


def test_strategist_prompts_require_plain_json_final_message():
    assert "ONLY valid JSON" in STRATEGIST_PROMPT
    assert "JSON object alone" in STRATEGIST_PROMPT
    assert "final_answer" not in STRATEGIST_PROMPT


def test_strategist_system_prompt_starts_with_strategist_role():
    prompt = build_tool_calling_system_prompt(
        STRATEGIST.system_prompt,
        include_work_directive=STRATEGIST.include_work_directive,
        include_mechanics=STRATEGIST.include_tool_mechanics,
    )

    assert prompt.startswith("You are the heartbeat strategist.")
    assert "You are a capable work agent" not in prompt
    assert "Prime directive: do the work" not in prompt


def test_strategist_prompts_limit_tool_use():
    assert "read_context" in STRATEGIST_PROMPT
    assert "memory_recall" in STRATEGIST_PROMPT
    assert "selected_priority" in STRATEGIST_PROMPT
    assert "priority_audit" in STRATEGIST_PROMPT
    assert "Do not call side-effecting tools" in STRATEGIST_PROMPT
    assert "normally at most 4 ordered" in STRATEGIST_PROMPT.lower() or "Normally emit at most 4" in STRATEGIST_PROMPT


def test_ouro_skill_describes_quest_lifecycle_semantics():
    ouro_skill = resolve_skill("ouro")

    assert ouro_skill is not None
    assert "`quest`" in ouro_skill
    assert '"close" means set the quest status to `closed` with `update_quest`' in ouro_skill
    assert '"cancel" means set status to `cancelled`' in ouro_skill
    assert "Only delete assets when the user explicitly says delete/remove/purge" in ouro_skill


def test_executor_and_developer_prompts_require_concrete_work():
    assert "Do the task, not just the reasoning around it" in EXECUTOR_PROMPT
    assert "Return concrete evidence of completion" in EXECUTOR_PROMPT
    assert "complete the workflow end to end" in DEVELOPER_PROMPT
    assert "return a plan in place of execution" in DEVELOPER_PROMPT


def test_strategist_profile_is_read_only():
    assert "memory_recall" in STRATEGIST.allowed_tools
    assert "read_context" in STRATEGIST.allowed_tools
    assert "ouro:query_dataset" in STRATEGIST.preload_tools
    assert STRATEGIST.include_work_directive is False
    assert STRATEGIST.include_tool_mechanics is False
    assert STRATEGIST.include_asset_placement is False
    assert "soul" not in STRATEGIST.shared_context_sections
    assert "working_memory" in STRATEGIST.shared_context_sections


def test_heartbeat_framing_points_at_search_delegation():
    assert "`search`" in HEARTBEAT_FRAMING
    assert "`research`" in HEARTBEAT_FRAMING
    assert "writer subagent" not in HEARTBEAT_FRAMING
    assert "Do not invent a second plan" in HEARTBEAT_FRAMING


def test_chat_framing_treats_status_questions_as_conversation():
    assert "what are you up to?" in CHAT_FRAMING
    assert "not a request to start research" in CHAT_FRAMING
    assert "Subagents are available" in CHAT_FRAMING
    assert "explicitly asks for substantial work" in CHAT_FRAMING
    assert "Only perform side-effecting platform actions" in CHAT_FRAMING


def test_always_loaded_platform_skills_are_injected_for_all_main_modes(tmp_path):
    config = OuroAgentsConfig(
        agent={"name": "hermes", "model": "test-model", "workspace": tmp_path},
        heartbeat={"model": "test-model"},
        mcp_servers=[],
        memory={
            "extraction_model": "test-model",
            "embedder": "test-embedder",
        },
    )
    startup_skills = load_startup_skills(config)
    assert "# Ouro Platform" in startup_skills
    assert "## Ouro Markdown Syntax" in startup_skills

    for profile in (CHAT, HEARTBEAT, PLAN, REVIEW):
        system_prompt, _dynamic_context = build_prompt(
            soul="",
            notes="",
            skills=startup_skills,
            profile=profile,
        )
        assert "## LOADED SKILLS" in system_prompt
        assert "# Ouro Platform" in system_prompt
        assert "## Ouro Markdown Syntax" in system_prompt


def test_current_datetime_is_dynamic_not_in_cacheable_static_prompt():
    """The volatile timestamp must live in dynamic context, not the static
    system prompt — otherwise it busts prompt-prefix caching for everything
    after it (soul, platform context, skills, tool directory)."""
    for profile in (CHAT, HEARTBEAT, PLAN, REVIEW):
        system_prompt, dynamic_context = build_prompt(
            soul="Be precise.",
            notes="",
            skills="# Ouro Platform",
            profile=profile,
        )
        assert "## CURRENT DATE AND TIME" not in system_prompt
        assert "## CURRENT DATE AND TIME" in dynamic_context
        # The stable, cacheable content stays in the static prompt.
        assert "# Ouro Platform" in system_prompt


def test_search_profile_is_cheap_non_publishing():
    assert SEARCH.delegatable is True
    assert SEARCH.allowed_servers == ["search"]
    assert "ouro:create_post" not in SEARCH.preload_tools
    assert SEARCH.include_asset_placement is False
    assert SEARCH.default_return_mode == "full_text"
    assert SEARCH.max_steps <= 4


def test_search_profile_is_exported_from_package():
    from ouro_agents import subagents as subagents_pkg

    assert subagents_pkg.SEARCH is SEARCH
    assert "SEARCH" in subagents_pkg.__all__


def test_platform_subagents_receive_ouro_asset_semantics():
    for profile in (RESEARCH, EXECUTOR, WRITER, DEVELOPER):
        assert "ouro" in profile.skills
        bodies = resolve_skills(profile.skills)
        joined = "\n\n".join(bodies)
        assert "# Ouro Platform" in joined
        assert '"close" means set the quest status to `closed` with `update_quest`' in joined


def test_skill_docs_use_load_tool_list_syntax():
    assert 'load_tool(["search:tavily_search"])' in resolve_skill("web-search")
    assert 'load_tool(["ouro:create_file"])' in resolve_skill("filesystem")
