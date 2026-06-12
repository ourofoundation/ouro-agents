import logging
from datetime import datetime, timezone
from typing import Optional

from .constants import CHARS_PER_TOKEN
from .modes.framing import build_output_format
from .modes.profiles import ModeProfile

logger = logging.getLogger(__name__)


MCP_TOOL_RULES = (
    "- Terminology: MCP tools are callable functions available to the agent. "
    "Ouro routes and services are platform endpoints/assets you discover with MCP tools "
    "and execute through `execute_route`.\n"
    '- MCP tools are deferred. Call `load_tool(["ouro:tool_name"])`, then call the tool by its `call_as` name. '
    "Preloaded MCP tools (listed below when present) can be called directly — no `load_tool` needed.\n"
    '- Skills can also be loaded on demand with `load_skill(["skill-name"])` when you need detailed guidance.\n'
    "- Omit optional params you don't need (don't pass null).\n"
    "- Batch where possible: load_tool, load_skill, memory_recall, and delegate all accept arrays.\n"
    "- File paths are always relative to the workspace root (e.g. 'data/file.json', not 'workspace/data/file.json').\n"
    "- Memory writes are automatic: after each run, facts, daily log entries, and asset references are "
    "extracted from your actions. You do not need to call memory-write tools or hand-edit MEMORY.md — "
    "focus on the task. To retrieve prior context, call `memory_recall`; when it returns asset refs, "
    "use get_asset to load them if needed.\n"
    "- For complex multi-step workflows or batch operations, delegate to the `developer` subagent — "
    "it has direct access to the Ouro Python SDK."
)

SUBAGENT_RULES = (
    "Subagents run in their own context. Use `delegate` with a list of task specs "
    "(multiple tasks run in parallel). Each spec: `subagent`, `task`, optional `asset_refs` and `return_mode`.\n\n"
    "**Delegate to accelerate real work, not to avoid it.** A delegation is only useful if you use the returned "
    "asset/action/result to complete the user's task.\n"
    "**MUST delegate:** multi-source research that warrants a written deliverable → `research`, "
    "long-form writing → `writer`, SDK/batch workflows → `developer`, "
    "focused self-contained sub-tasks → `executor`.\n"
    "**Handle yourself:** simple questions, single tool calls, chat replies, quick lookups — "
    "including a quick factual web search (call the search tool directly; don't spin up `research` "
    "for one fact, since it publishes a post).\n\n"
    "Subagents save output as Ouro assets and return JSON with `asset_id`, `name`, `description`, "
    "and a ready-to-use `link`. The asset is already created and published — **do NOT create or "
    "publish another asset for the same work, and do NOT paste the subagent's full body into your reply.** "
    "Surface the result by embedding or linking the returned `asset_id` (use the provided `link`). "
    "Call `get_asset(asset_id)` only when you genuinely need the full content."
)

# Section ordering — lower number = higher priority = appears first in prompt
SECTION_PRIORITY = {
    "mode": 1,
    "current_datetime": 2,
    "soul": 3,
    "platform_context": 4,
    "user_model": 5,
    "output": 6,
    "notes": 7,
    "conversation_state": 8,
    "plans_index": 9,
    "entity_context": 10,
    "conversation": 11,
    "working_memory": 12,
    "subagents": 13,
    "tool_rules": 14,
    "skills": 15,
    "skill_directory": 16,
}


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


SYSTEM_PROMPT_TOKEN_BUDGET = 64000

# Sections that should never be truncated, in order of protection
_PROTECTED_SECTIONS = {"mode", "current_datetime", "soul", "platform_context", "output"}

# Sections that can be truncated, in order of expendability (first = cut first)
_TRIMMABLE_SECTIONS = [
    "skill_directory",
    "skills",
    "entity_context",
    "working_memory",
    "user_model",
    "notes",
    "conversation",
    "conversation_state",
]


def _enforce_budget(sections: dict[str, str], ordered_keys: list[str]) -> None:
    """Truncate low-priority sections if the total exceeds the token budget.

    Modifies ``sections`` in place.  Protected sections (mode, soul, state)
    are never touched.  Trimmable sections are cut in reverse priority order.
    """
    total = sum(_estimate_tokens(sections[k]) for k in ordered_keys)
    if total <= SYSTEM_PROMPT_TOKEN_BUDGET:
        return

    overage = total - SYSTEM_PROMPT_TOKEN_BUDGET
    for section_key in _TRIMMABLE_SECTIONS:
        if overage <= 0:
            break
        if section_key not in sections:
            continue
        section_tokens = _estimate_tokens(sections[section_key])
        if section_tokens <= 100:
            continue
        max_chars = max(400, (section_tokens - overage) * CHARS_PER_TOKEN)
        if max_chars < len(sections[section_key]):
            sections[section_key] = (
                sections[section_key][:max_chars] + "\n[...truncated]"
            )
            saved = section_tokens - _estimate_tokens(sections[section_key])
            overage -= saved
            logger.info(
                "Budget enforcement: trimmed '%s' by ~%d tokens", section_key, saved
            )


def current_datetime_section(workspace_root: str = "") -> str:
    """Return a compact current-date section for prompt injection."""
    # Drop sub-second precision: the model never needs microseconds, and a
    # volatile timestamp would otherwise bust prompt-prefix caching on any
    # consumer that still places this section in a cached block.
    local_now = datetime.now().astimezone().replace(microsecond=0)
    utc_now = local_now.astimezone(timezone.utc)
    lines = [
        "## CURRENT DATE AND TIME",
        f"Local datetime: {local_now.isoformat()}",
        f"Current date: {local_now.date().isoformat()}",
        f"Weekday: {local_now.strftime('%A')}",
        f"UTC datetime: {utc_now.isoformat()}",
    ]
    if workspace_root:
        lines.append(f"Workspace root: {workspace_root}")
    return "\n".join(lines)


def build_shared_prompt_sections(
    *,
    soul: str = "",
    notes: str = "",
    platform_context: str = "",
    user_model: str = "",
    working_memory: str = "",
    conversation_state: str = "",
    plans_index: str = "",
    workspace_root: str = "",
) -> dict[str, str]:
    """Build the shared prompt sections used by main and subagent runs."""
    sections: dict[str, str] = {"current_datetime": current_datetime_section(workspace_root)}

    if soul:
        sections["soul"] = f"## IDENTITY AND RULES (SOUL)\n{soul}"

    if platform_context:
        sections["platform_context"] = f"## PLATFORM CONTEXT\n{platform_context}"

    if user_model:
        sections["user_model"] = f"## USER CONTEXT\n{user_model}"

    if notes:
        sections["notes"] = f"## DEPLOYMENT CONTEXT (NOTES)\n{notes}"

    if conversation_state:
        sections["conversation_state"] = (
            f"## CONVERSATION STATE\n{conversation_state}"
        )

    if plans_index:
        sections["plans_index"] = f"## PLAN QUEST INDEX\n{plans_index}"

    if working_memory:
        sections["working_memory"] = f"## WORKING MEMORY\n{working_memory}"

    return sections


# Sections that change every turn and should live in the task message
# (not the system prompt) to enable prefix caching on the static part.
#
# ``current_datetime`` belongs here: it changes on every call, and when it
# sat near the top of the *static* system prompt it invalidated the cached
# prefix for everything after it (soul, platform context, skills, the tool
# directory — several thousand tokens). Subagents already inject it via the
# task message (``runner._format_task_context``); this keeps the main agent
# consistent and makes the whole static prompt cacheable.
_DYNAMIC_SECTIONS = {
    "current_datetime",
    "conversation_state",
    "plans_index",
    "entity_context",
    "working_memory",
    "conversation",
    "user_model",
}


def build_prompt(
    soul: str,
    notes: str,
    skills: str,
    profile: ModeProfile,
    skill_directory: str = "",
    working_memory: str = "",
    conversation_context: str = "",
    conversation_state: str = "",
    user_model: str = "",
    entity_context: str = "",
    deferred_tool_directory: str = "",
    subagent_directory: str = "",
    mode_framing_override: str = "",
    platform_context: str = "",
    chat_conversation_id: Optional[str] = None,
    preloaded_tool_names: Optional[list[str]] = None,
    plans_index: str = "",
    workspace_root: str = "",
) -> tuple[str, str]:
    """Assemble the system prompt and dynamic context.

    Returns (system_prompt, dynamic_context) where:
    - system_prompt: stable sections suitable for LLM prefix caching
    - dynamic_context: per-turn sections to prepend to the task message
    """

    sections: dict[str, str] = build_shared_prompt_sections(
        soul=soul,
        notes=notes,
        platform_context=platform_context,
        user_model=user_model,
        working_memory=working_memory,
        conversation_state=conversation_state,
        plans_index=plans_index,
        workspace_root=workspace_root,
    )

    framing = mode_framing_override or profile.framing
    sections["mode"] = f"## MODE\n{framing}"
    if profile.include_chat_conversation_id and chat_conversation_id:
        annotation = profile.conversation_id_annotation
        if annotation:
            sections["mode"] += (
                f"\n\n**Conversation id for this run:** `{chat_conversation_id}` "
                f"({annotation})."
            )
        else:
            sections[
                "mode"
            ] += f"\n\n**Conversation id for this run:** `{chat_conversation_id}`"
    if skills:
        sections["skills"] = f"## LOADED SKILLS\n{skills}"

    if skill_directory:
        sections["skill_directory"] = (
            "## AVAILABLE SKILLS (use `load_skill` to activate)\n"
            "These skills are available on demand but are not loaded yet. "
            "Call `load_skill` with one or more names from this directory when you need "
            "detailed guidance.\n\n"
            f"{skill_directory}"
        )

    if entity_context:
        sections["entity_context"] = f"## ACTIVE CONTEXT\n{entity_context}"

    if conversation_context:
        sections["conversation"] = (
            f"## RECENT CONVERSATION (most recent last)\n{conversation_context}"
        )

    if subagent_directory:
        sections["subagents"] = (
            f"## SUBAGENTS (use `delegate` tool to invoke)\n"
            f"{SUBAGENT_RULES}\n\n"
            f"{subagent_directory}"
        )

    if deferred_tool_directory:
        tool_rules_text = f"## MCP TOOL USAGE RULES\n{MCP_TOOL_RULES}"
        if preloaded_tool_names:
            names = ", ".join(f"`{n}`" for n in preloaded_tool_names)
            tool_rules_text += (
                f"\n\n## PRELOADED TOOLS (ready to call — no load_tool needed)\n"
                f"These MCP tools are already loaded: {names}. "
                f"Call them directly. Use `load_tool` only for additional MCP tools."
            )
        tool_rules_text += (
            f"\n\n## DEFERRED TOOL DIRECTORY\n"
            f"Primary servers list one line per tool. Secondary servers are collapsed to a "
            f"single summary line — call `load_tool([\"<server>\"])` to list that server's "
            f"tools, then `load_tool` the specific ones you need.\n\n"
            f"{deferred_tool_directory}"
        )
        sections["tool_rules"] = tool_rules_text

    sections["output"] = build_output_format(
        profile.output_format, profile.name, preloaded_tool_names
    )

    ordered_keys = sorted(
        sections.keys(),
        key=lambda k: SECTION_PRIORITY.get(k, 99),
    )

    _enforce_budget(sections, ordered_keys)

    # Split into static (cacheable) and dynamic (per-turn) sections
    static_keys = [k for k in ordered_keys if k not in _DYNAMIC_SECTIONS]
    dynamic_keys = [k for k in ordered_keys if k in _DYNAMIC_SECTIONS]

    static_tokens = sum(_estimate_tokens(sections[k]) for k in static_keys)
    dynamic_tokens = sum(_estimate_tokens(sections[k]) for k in dynamic_keys)
    logger.info(
        "System prompt: ~%d static tokens + ~%d dynamic tokens across %d sections",
        static_tokens,
        dynamic_tokens,
        len(ordered_keys),
    )

    system_prompt = "\n\n---\n\n".join(sections[k] for k in static_keys)
    dynamic_context = (
        "\n\n---\n\n".join(sections[k] for k in dynamic_keys) if dynamic_keys else ""
    )

    return system_prompt, dynamic_context
