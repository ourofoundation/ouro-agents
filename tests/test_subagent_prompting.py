from ouro_agents.config import OuroAgentsConfig
from ouro_agents.soul import build_prompt, build_shared_prompt_sections, current_datetime_section
from ouro_agents.modes.framing import CHAT_FRAMING, HEARTBEAT_FRAMING
from ouro_agents.modes.profiles import CHAT, HEARTBEAT, PLAN, REVIEW
from ouro_agents.skills import load_startup_skills, resolve_skill, resolve_skills
from ouro_agents.subagents.context import SubAgentContext
from ouro_agents.subagents.preflight import HEARTBEAT_PREFLIGHT_PROMPT, PREFLIGHT_PROMPT
from ouro_agents.subagents.prompts import DEVELOPER_PROMPT, EXECUTOR_PROMPT
from ouro_agents.subagents.profiles import DEVELOPER, EXECUTOR, HEARTBEAT_PREFLIGHT, PREFLIGHT, RESEARCH, WRITER
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


def test_preflight_prompts_require_final_answer_json():
    for prompt in (PREFLIGHT_PROMPT, HEARTBEAT_PREFLIGHT_PROMPT):
        assert "Finish by calling final_answer exactly once" in prompt
        assert "Return the JSON only inside final_answer" in prompt


def test_preflight_prompts_limit_tool_use_and_recover():
    assert "simple or conversational, do not call tools" in PREFLIGHT_PROMPT
    assert "call memory_recall exactly once" in PREFLIGHT_PROMPT
    assert "memory_recall returns no useful context" in PREFLIGHT_PROMPT
    assert "previous response failed or was not accepted" in PREFLIGHT_PROMPT
    assert "Your job is analysis only" in PREFLIGHT_PROMPT
    assert "Never call side-effecting platform MCP tools" in PREFLIGHT_PROMPT
    assert "create_comment" in PREFLIGHT_PROMPT

    assert "call memory_recall at most once" in HEARTBEAT_PREFLIGHT_PROMPT
    assert "memory_recall returns no useful context" in HEARTBEAT_PREFLIGHT_PROMPT
    assert "previous response failed or was not accepted" in HEARTBEAT_PREFLIGHT_PROMPT
    assert "Your job is analysis only" in HEARTBEAT_PREFLIGHT_PROMPT
    assert "Never call side-effecting platform MCP tools" in HEARTBEAT_PREFLIGHT_PROMPT


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


def test_preflight_profiles_only_allow_memory_recall():
    assert PREFLIGHT.allowed_tools == ["memory_recall"]
    assert HEARTBEAT_PREFLIGHT.allowed_tools == ["memory_recall"]
    assert PREFLIGHT.preload_tools == []
    assert HEARTBEAT_PREFLIGHT.preload_tools == []


def test_heartbeat_framing_does_not_require_unavailable_delegate_tool():
    assert "writer subagent" not in HEARTBEAT_FRAMING
    assert "Delegate to" not in HEARTBEAT_FRAMING


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
