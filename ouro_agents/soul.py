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
    "- File paths are always relative to the workspace root (e.g. 'scratch/out.json', not 'workspace/scratch/out.json').\n"
    "- Memory writes are usually automatic: after each run, durable facts and daily log entries are "
    "curated from your actions. When the user explicitly states a stable preference, direction, or "
    "important fact that should affect future runs, call `remember` if available. Do not store raw "
    "episodes or task mechanics in vector memory, and do not hand-edit MEMORY.md. To retrieve prior "
    "context, call `memory_recall`; when it returns asset refs, use get_asset to load them if needed.\n"
    "- When a recalled memory is wrong or outdated, fix it immediately: `update_memory(id, ...)` to "
    "revise it in place, or `forget(id, ...)` to delete it. memory_recall returns the id to use.\n"
    "- For complex multi-step workflows or batch operations, prefer the `developer` subagent when "
    "delegation is available — it has direct access to the Ouro Python SDK."
)

HEARTBEAT_SUBAGENT_RULES = (
    "Follow the strategist brief. Use `delegate` for exploration and heavy tool work; "
    "keep quest lifecycle updates and one-shot comments on this heartbeat.\n\n"
    "**MUST delegate:** routine web/current-info lookup → `search`, "
    "multi-source research with a publishable writeup → `research`, "
    "long-form writing → `writer`, focused MCP sub-tasks → `executor`, "
    "SDK/batch workflows → `developer`.\n"
    "**MUST NOT:** call search MCP tools directly, invent a second plan, "
    "or republish an asset a subagent already created.\n"
    "Default `return_mode` is summary_only. Prefer the returned `link` / `asset_id` "
    "over pasting full bodies."
)

WORKSPACE_LAYOUT_RULES = (
    "Your workspace persists across runs. Keep it organized — future runs (and other "
    "subagents) must be able to find things without searching.\n\n"
    "**Never write new files at the workspace root.** The root is reserved for "
    "framework files (SOUL.md, NOTES.md, MEMORY.md, HEARTBEAT.md) and "
    "framework-managed directories. The sandbox enforces this: writes at the "
    "root or under `protected/` raise PermissionError.\n\n"
    "**Do not write under `protected/`.** That directory is framework-only "
    "(platform cache, scheduled tasks, run log, mem0/Chroma). Agent artifacts "
    "there collide with the harness. Legacy top-level `data/` and `memory/` "
    "are also refused.\n\n"
    "Put your own files here:\n"
    "- `projects/<slug>/` — all artifacts for a project or work cycle (analyses, "
    "results, generated structures/files, post drafts). One directory per ongoing "
    "effort; reuse it across runs instead of inventing new top-level names.\n"
    "- `drafts/` — outgoing drafts not tied to a project (emails, follow-ups, posts).\n"
    "- `scratch/` — disposable intermediates and cross-run state. Safe to delete.\n"
    "- `cifs/` — optional structure library when you maintain one (else keep CIFs "
    "under the relevant `projects/<slug>/`).\n"
    "- `coils/<name>/` — saved coil workflows (coil.json + handler.py); see the "
    "`coils` skill.\n\n"
    "Period logs live at `teams/<team_id>/logs/<period>.md` (period follows "
    "`memory.rhythm`). Do not invent parallel log paths under `memory/`.\n\n"
    "Rules:\n"
    "- Before writing, check whether a fitting `projects/` directory already exists.\n"
    "- Overwrite working files in place instead of writing `_v2`/`_fixed`/`_final` "
    "copies; the run log preserves history.\n"
    "- Reuse one canonical filename per artifact (e.g. `crm_updates.json`), not a new "
    "name per run."
)

SUBAGENT_RULES = (
    "Subagents run in their own context. Use `delegate` with a list of task specs "
    "(multiple tasks run in parallel). Each spec: `subagent`, `task`, optional `asset_refs` and `return_mode`.\n\n"
    "**Delegate to accelerate real work, not to avoid it.** A delegation is only useful if you use the returned "
    "asset/action/result to complete the user's task.\n"
    "**MUST delegate:** multi-source research that warrants a written deliverable → `research`, "
    "quick current-info lookup → `search`, "
    "long-form writing → `writer`, SDK/batch workflows → `developer`, "
    "focused self-contained sub-tasks → `executor`.\n"
    "**Handle yourself:** simple questions, single tool calls, chat replies — "
    "but prefer `search` over spinning up `research` for one fact, since research publishes a post.\n\n"
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
    "conversation_id": 2.5,
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
    "coils": 13.5,
    "tool_rules": 14,
    "workspace_layout": 14.5,
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

# Sections ordered most-recent-last: trimming keeps the tail (recent content)
# instead of the head. Everything else keeps the head.
_TRIM_KEEP_TAIL = {"conversation", "conversation_state"}


def _trim_section(text: str, max_chars: int, *, keep_tail: bool) -> str:
    """Cut a section to ~max_chars at a line boundary, marking the cut."""
    if len(text) <= max_chars:
        return text
    if not keep_tail:
        cut = text[:max_chars]
        newline = cut.rfind("\n")
        if newline > 0:
            cut = cut[:newline]
        return cut + "\n[...truncated]"
    # Keep the section heading, then the tail of the body (most recent lines).
    heading, _, body = text.partition("\n")
    cut = body[-max(0, max_chars - len(heading) - 1) :]
    newline = cut.find("\n")
    if newline != -1:
        cut = cut[newline + 1 :]
    return f"{heading}\n[...truncated]\n{cut}"


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
            sections[section_key] = _trim_section(
                sections[section_key],
                max_chars,
                keep_tail=section_key in _TRIM_KEEP_TAIL,
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

    if workspace_root:
        sections["workspace_layout"] = (
            f"## WORKSPACE FILE ORGANIZATION\n{WORKSPACE_LAYOUT_RULES}"
        )

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
    "conversation_id",
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
    coil_directory: str = "",
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
    # The per-conversation id is volatile: keeping it in the static MODE section
    # (the very first block of the system prompt) gave every conversation a
    # unique prefix and defeated cross-conversation cache hits. It lives in the
    # dynamic context (prepended to the task) instead.
    if profile.include_chat_conversation_id and chat_conversation_id:
        annotation = profile.conversation_id_annotation
        if annotation:
            sections["conversation_id"] = (
                f"## CONVERSATION\n**Conversation id for this run:** "
                f"`{chat_conversation_id}` ({annotation})."
            )
        else:
            sections["conversation_id"] = (
                f"## CONVERSATION\n**Conversation id for this run:** "
                f"`{chat_conversation_id}`"
            )
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
        rules = (
            HEARTBEAT_SUBAGENT_RULES
            if profile.name == "heartbeat"
            else SUBAGENT_RULES
        )
        sections["subagents"] = (
            f"## SUBAGENTS (use `delegate` tool to invoke)\n"
            f"{rules}\n\n"
            f"{subagent_directory}"
        )

    if coil_directory:
        sections["coils"] = (
            "## COILS (saved workflows — use `run_coil` to invoke)\n"
            "Coils are your saved multi-step workflows. Before hand-composing "
            "a sequence of Ouro calls you have done before, check this index "
            "and prefer `run_coil(name, params)`. Author new coils under "
            "coils/<name>/ (load the `coils` skill); publish with "
            "`publish_route` to serve one as a live Ouro route.\n\n"
            f"{coil_directory}"
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
