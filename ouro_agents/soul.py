import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import RunMode
from .constants import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)


def load_soul(path: Path) -> str:
    """Load SOUL.md if it exists, return empty string otherwise."""
    if path.exists():
        return path.read_text()
    return ""


MCP_TOOL_RULES = (
    '- MCP tools are deferred. Call `load_tool(["ouro:tool_name"])`, then call the tool by its `call_as` name. '
    "Preloaded tools (listed below when present) can be called directly — no `load_tool` needed.\n"
    "- Skills can also be loaded on demand with `load_skill([\"skill-name\"])` when you need detailed guidance.\n"
    "- Emit real tool calls only — no narration, pseudo-JSON, or plain-text pseudo-calls.\n"
    "- Omit optional params you don't need (don't pass null). Retry once on failure, then move on.\n"
    "- Batch where possible: load_tool, load_skill, memory_recall, memory_store, and delegate all accept arrays.\n"
    "- File paths are always relative to the workspace root (e.g. 'data/file.json', not 'workspace/data/file.json').\n"
    "- Link assets in markdown with `[label](asset:<uuid>)` or typed `post:`/`file:`/`dataset:` links.\n"
    "- For complex multi-step workflows or batch operations, delegate to the `developer` subagent — "
    "it has direct access to the Ouro Python SDK."
)

SUBAGENT_RULES = (
    "Subagents run in their own context. Use `delegate` with a list of task specs "
    "(multiple tasks run in parallel). Each spec: `subagent`, `task`, optional `asset_refs` and `return_mode`.\n\n"
    "**MUST delegate:** web search → `research` (never call search tools yourself), "
    "long-form writing → `writer`, SDK/batch workflows → `developer`, "
    "focused sub-tasks → `executor`.\n"
    "**Handle yourself:** simple questions, single tool calls, chat replies, quick lookups.\n\n"
    "Subagents save output as Ouro assets and return JSON with `asset_id`, `name`, `description`. "
    "Use `get_asset(asset_id)` for full content."
)

CHAT_FRAMING = (
    "You are in a conversation. Your primary goal is to help the person you're talking to. "
    "Be conversational, clear, and concise. Ask clarifying questions when a request is ambiguous. "
    "On Ouro, user-visible replies must be posted with MCP (`send_message`); the run's final answer alone "
    "does not appear in the thread. Use other tools when the request calls for it; when you do, say what you found or did."
)

AUTONOMOUS_FRAMING = (
    "You are operating autonomously to complete a task. "
    "Work through the task step by step, using tools as needed. "
    "Report what you accomplished when finished."
)

HEARTBEAT_FRAMING = (
    "You are running an autonomous heartbeat. Review your context and playbook, "
    "then decide what's most valuable to do right now. Be genuine and thoughtful "
    "— quality over quantity. If nothing feels worth doing, it's okay to pass."
)

PLANNING_FRAMING = (
    "You are entering a planning phase. Review recent activity, your memory, "
    "and ongoing work, then create a plan for the upcoming period. "
    "Be thoughtful and realistic. Publish your plan as an Ouro post "
    "so your team can review it before you begin."
)

REVIEW_FRAMING = (
    "You have a pending plan that may have received human feedback. "
    "Check for comments on the plan post, incorporate any feedback, "
    "and finalize the plan."
)

_MODE_FRAMING = {
    RunMode.CHAT: CHAT_FRAMING,
    RunMode.AUTONOMOUS: AUTONOMOUS_FRAMING,
    RunMode.HEARTBEAT: HEARTBEAT_FRAMING,
}

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
    "entity_context": 9,
    "conversation": 10,
    "working_memory": 11,
    "subagents": 12,
    "tool_rules": 13,
    "skills": 14,
    "skill_directory": 15,
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
            # Not worth trimming tiny sections
            continue
        # Trim by removing content from the end, keeping the header
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


def _current_datetime_section() -> str:
    """Return a compact current-date section for the system prompt."""
    local_now = datetime.now().astimezone()
    utc_now = local_now.astimezone(timezone.utc)
    return (
        "## CURRENT DATE AND TIME\n"
        f"Local datetime: {local_now.isoformat()}\n"
        f"Current date: {local_now.date().isoformat()}\n"
        f"Weekday: {local_now.strftime('%A')}\n"
        f"UTC datetime: {utc_now.isoformat()}"
    )


# Sections that change every turn and should live in the task message
# (not the system prompt) to enable prefix caching on the static part.
_DYNAMIC_SECTIONS = {
    "conversation_state",
    "entity_context",
    "working_memory",
    "conversation",
    "user_model",
}


def build_prompt(
    soul: str,
    notes: str,
    skills: str,
    skill_directory: str = "",
    working_memory: str = "",
    mode: RunMode = RunMode.AUTONOMOUS,
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
) -> tuple[str, str]:
    """Assemble the system prompt and dynamic context.

    Returns (system_prompt, dynamic_context) where:
    - system_prompt: stable sections suitable for LLM prefix caching
    - dynamic_context: per-turn sections to prepend to the task message
    """

    sections: dict[str, str] = {}

    framing = mode_framing_override or _MODE_FRAMING[mode]
    sections["mode"] = f"## MODE\n{framing}"
    if mode == RunMode.CHAT and chat_conversation_id:
        sections["mode"] += (
            f"\n\n**Conversation id for this run:** `{chat_conversation_id}` "
            "(use as `conversation_id` in `send_message` when posting)."
        )
    sections["current_datetime"] = _current_datetime_section()

    if soul:
        sections["soul"] = f"## IDENTITY AND RULES (SOUL)\n{soul}"

    if platform_context:
        sections["platform_context"] = f"## PLATFORM CONTEXT\n{platform_context}"

    if user_model:
        sections["user_model"] = f"## USER CONTEXT\n{user_model}"

    if notes:
        sections["notes"] = f"## DEPLOYMENT CONTEXT (NOTES)\n{notes}"

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

    if working_memory:
        sections["working_memory"] = f"## WORKING MEMORY\n{working_memory}"

    if conversation_state:
        sections["conversation_state"] = f"## CONVERSATION STATE\n{conversation_state}"

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
                f"These tools are already loaded: {names}. "
                f"Call them directly. Use `load_tool` only for additional tools."
            )
        tool_rules_text += (
            f"\n\n## DEFERRED TOOL DIRECTORY (name + short description)\n"
            f"{deferred_tool_directory}"
        )
        sections["tool_rules"] = tool_rules_text

    if mode == RunMode.CHAT:
        _preloaded = set(preloaded_tool_names or [])
        if "send_message" in _preloaded:
            send_instr = (
                "post your reply by calling `send_message` with the real `conversation_id` and reply text "
                "(already loaded)."
            )
        else:
            send_instr = "load `ouro:send_message`, then call `send_message` with the real `conversation_id` and reply text."
        sections["output"] = (
            "## OUTPUT FORMAT\n"
            f"If the task or context includes an Ouro `conversation_id` (or you are clearly in an Ouro chat): "
            f"{send_instr} "
            "Then call `final_answer` with the **same** text as `send_message` so streaming and "
            "local logs match the message you posted. If you should not reply, call `final_answer` with exactly "
            "NO_ACTION only (do not call `send_message`).\n"
            "If there is no Ouro conversation_id (e.g. ad-hoc API run), respond with `final_answer` only."
        )
    else:
        sections["output"] = (
            "## OUTPUT FORMAT\n"
            "For simple replies (greetings, acknowledgments, or when no tools are needed), "
            "call the `final_answer` tool directly with your response. "
            "Never respond with plain text outside a tool call. "
            "Never emit pseudo-tool syntax such as 'Calling tools:' or handwritten JSON."
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
