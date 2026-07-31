from ouro_agents.config import OuroAgentsConfig
from ouro_agents.soul import build_prompt, build_shared_prompt_sections, current_datetime_section
from ouro_agents.modes.framing import (
    CHAT_FRAMING,
    HEARTBEAT_FRAMING,
    HEARTBEAT_OUTPUT,
    heartbeat_framing_for_kind,
)
from ouro_agents.modes.profiles import CHAT, HEARTBEAT, PLAN
from ouro_agents.skills import load_startup_skills, resolve_skill, resolve_skills
from ouro_agents.subagents.context import SubAgentContext
from ouro_agents.subagents.prompts import DEVELOPER_PROMPT, EXECUTOR_PROMPT
from ouro_agents.subagents.profiles import (
    DEVELOPER,
    EXECUTOR,
    RESEARCH,
    SEARCH,
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


def test_subagent_task_context_docker_uses_mount_not_host_path(tmp_path):
    from ouro_agents.config import SandboxConfig

    host_workspace = tmp_path / "agents" / "hermes"
    host_workspace.mkdir(parents=True)
    ctx = SubAgentContext(
        workspace=host_workspace,
        backend=None,
        agent_id="hermes",
        memory_config=None,
        model=None,
        sandbox_config=SandboxConfig(mode="docker", workspace_mount="/workspace"),
    )

    prompt = _format_task_context("Inspect the workspace.", ctx)

    assert "Workspace root: /workspace" in prompt
    assert str(host_workspace.resolve()) not in prompt


def test_build_shared_prompt_sections_formats_core_sections():
    sections = build_shared_prompt_sections(
        soul="Be precise.",
        notes="Deployment note.",
        platform_context="You are @athena.",
        user_model="Prefers concise updates.",
        working_memory="Recent anchor post: Day 9.",
        plans_index="- PLAN:athena:2026-04-06",
    )

    assert sections["soul"] == "## IDENTITY AND RULES (SOUL)\nBe precise."
    assert sections["notes"] == "## DEPLOYMENT CONTEXT (NOTES)\nDeployment note."
    assert sections["platform_context"] == "## PLATFORM CONTEXT\nYou are @athena."
    assert sections["user_model"] == "## USER CONTEXT\nPrefers concise updates."
    assert sections["working_memory"] == "## WORKING MEMORY\nRecent anchor post: Day 9."
    assert "conversation_state" not in sections
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
    assert "run_python" in EXECUTOR_PROMPT
    assert EXECUTOR.needs_python_tool is True
    assert "load_skill" in EXECUTOR.allowed_tools
    assert "read_context" in EXECUTOR.allowed_tools
    assert "filesystem" in EXECUTOR.skills
    assert EXECUTOR.can_delegate_to == []
    assert "Side-branch" in EXECUTOR.description or "side-branch" in EXECUTOR.description
    assert "complete the workflow end to end" in DEVELOPER_PROMPT
    assert "return a plan in place of execution" in DEVELOPER_PROMPT



def test_heartbeat_framing_points_at_search_delegation():
    assert "`search`" in HEARTBEAT_FRAMING
    assert "`research`" in HEARTBEAT_FRAMING
    assert "writer subagent" not in HEARTBEAT_FRAMING
    assert "You own the whole tick" in HEARTBEAT_FRAMING


def test_heartbeat_output_requires_tick_summary_json():
    assert '"action"' in HEARTBEAT_OUTPUT
    assert "worth_remembering" in HEARTBEAT_OUTPUT
    assert "selected_priority" in HEARTBEAT_OUTPUT
    assert "ONLY valid JSON" in HEARTBEAT_OUTPUT


def test_quest_work_framing_includes_mechanics_open_ended_does_not():
    quest = heartbeat_framing_for_kind("quest_work")
    open_ended = heartbeat_framing_for_kind("open_ended")
    assert "`update_quest_item`" in quest
    assert "`update_quest_item`" not in open_ended


def test_chat_framing_status_questions_require_lookup_not_new_work():
    assert "what are you up to?" in CHAT_FRAMING
    assert "report recent work" in CHAT_FRAMING
    assert "read_context" in CHAT_FRAMING
    assert "Looking things up is not taking initiative" in CHAT_FRAMING
    assert "do not start new research" in CHAT_FRAMING
    assert "Subagents" in CHAT_FRAMING
    assert "explicitly asks for substantial work" in CHAT_FRAMING
    assert "Only perform side-effecting platform actions" in CHAT_FRAMING
    assert "commit-and-continue" in CHAT_FRAMING


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

    for profile in (CHAT, HEARTBEAT, PLAN):
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
    for profile in (CHAT, HEARTBEAT, PLAN):
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


def test_research_profile_writes_local_draft_only():
    assert RESEARCH.delegatable is True
    assert RESEARCH.allowed_servers == ["search"]
    assert "ouro:create_post" not in RESEARCH.preload_tools
    assert RESEARCH.include_asset_placement is False
    assert RESEARCH.needs_python_tool is True
    assert RESEARCH.can_delegate_to == []
    assert "filesystem" in RESEARCH.skills
    assert "ouro" not in RESEARCH.skills
    assert "asset_output" not in RESEARCH.skills
    assert "Never create posts" in RESEARCH.system_prompt


def test_search_profile_is_exported_from_package():
    from ouro_agents import subagents as subagents_pkg

    assert subagents_pkg.SEARCH is SEARCH
    assert "SEARCH" in subagents_pkg.__all__


def test_writer_profile_is_ouro_only_prose_specialist():
    assert WRITER.delegatable is True
    assert WRITER.allowed_servers == ["ouro"]
    assert WRITER.needs_python_tool is False
    assert WRITER.can_delegate_to == []
    assert "memory_recall" in WRITER.allowed_tools
    assert WRITER.preload_tools == [
        "ouro:create_post",
        "ouro:update_post",
        "ouro:get_asset",
    ]
    assert "Not for research" in WRITER.description
    assert "do not research the web" in WRITER.system_prompt.lower()
    assert "local paths via web crawl" in WRITER.system_prompt


def test_platform_subagents_receive_ouro_asset_semantics():
    for profile in (EXECUTOR, WRITER, DEVELOPER):
        assert "ouro" in profile.skills
        bodies = resolve_skills(profile.skills)
        joined = "\n\n".join(bodies)
        assert "# Ouro Platform" in joined
        assert '"close" means set the quest status to `closed` with `update_quest`' in joined


def test_skill_docs_use_load_tool_list_syntax():
    assert 'load_tool(["search:web_search_exa"])' in resolve_skill("web-search")
    assert 'load_tool(["ouro:create_file"])' in resolve_skill("filesystem")
