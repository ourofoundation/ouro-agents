import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import RunMode

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4


def load_soul(path: Path) -> str:
    """Load SOUL.md if it exists, return empty string otherwise."""
    if path.exists():
        return path.read_text()
    return ""


MCP_TOOL_RULES = (
    "- MCP tools are deferred. Pick one from the deferred tool directory below.\n"
    "- To use a tool: call `load_tool` with the tool name, then call the tool directly by its `call_as` name.\n"
    "  Example: load_tool('ouro:search_assets') → search_assets(query='...')\n"
    "- Never pass placeholder values (e.g. `<path_to_file>`). Use real values only.\n"
    "- If a tool call fails, fix arguments and retry one time before giving up.\n"
    "- Prefer fully-qualified names like `ouro:create_post` when calling `load_tool`.\n"
    "- For content/topic questions on Ouro (e.g. 'what's new in X?'), usually use `ouro:search_assets` "
    "(and optionally `ouro:get_team_activity`).\n"
    "- PATHS: All file paths (filesystem tools AND run_python helpers) are relative to the workspace root.\n"
    "  Use 'report.md' or 'data/file.json' — NOT 'workspace/report.md' or './workspace/data/file.json'.\n"
    "- OPTIONAL PARAMS: Omit optional parameters you don't need. Do NOT pass 'null' or None — just leave them out."
)

CHAT_FRAMING = (
    "You are in a conversation. Your primary goal is to help the person you're talking to. "
    "Be conversational, clear, and concise. Ask clarifying questions when a request is ambiguous. "
    "Use your tools when the user's request calls for it, but don't reach for tools when a plain answer works. "
    "When you do use tools, explain what you found or did."
)

AUTONOMOUS_FRAMING = (
    "You are operating autonomously to complete a task. "
    "Work through the task step by step, using tools as needed. "
    "Report what you accomplished when finished."
)

HEARTBEAT_FRAMING = (
    "You are running a scheduled check-in. Evaluate whether any action is needed "
    "based on the checklist below. Only act if something genuinely needs attention."
)

_MODE_FRAMING = {
    RunMode.CHAT: CHAT_FRAMING,
    RunMode.AUTONOMOUS: AUTONOMOUS_FRAMING,
    RunMode.HEARTBEAT: HEARTBEAT_FRAMING,
}

# Section ordering — lower number = higher priority = appears first in prompt
SECTION_PRIORITY = {
    "mode":             1,
    "current_datetime": 2,
    "soul":             3,
    "output":           4,
    "notes":            5,
    "conversation":     6,
    "working_memory":   7,
    "tool_rules":       8,
    "skills":           9,
}


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


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


def build_prompt(
    soul: str,
    notes: str,
    skills: str,
    working_memory: str = "",
    mode: RunMode = RunMode.AUTONOMOUS,
    conversation_context: str = "",
    deferred_tool_directory: str = "",
) -> str:
    """Assemble the full system prompt, ordered by section priority."""

    sections: dict[str, str] = {}

    sections["mode"] = f"## MODE\n{_MODE_FRAMING[mode]}"
    sections["current_datetime"] = _current_datetime_section()

    if soul:
        sections["soul"] = f"## IDENTITY AND RULES (SOUL)\n{soul}"

    if notes:
        sections["notes"] = f"## DEPLOYMENT CONTEXT (NOTES)\n{notes}"

    if skills:
        sections["skills"] = f"## SKILLS AND KNOWLEDGE\n{skills}"

    if working_memory:
        sections["working_memory"] = f"## WORKING MEMORY\n{working_memory}"

    if conversation_context:
        sections["conversation"] = (
            f"## RECENT CONVERSATION (most recent last)\n{conversation_context}"
        )

    if deferred_tool_directory:
        sections["tool_rules"] = (
            f"## MCP TOOL USAGE RULES\n{MCP_TOOL_RULES}\n\n"
            f"## DEFERRED TOOL DIRECTORY (name + short description)\n{deferred_tool_directory}"
        )

    if mode == RunMode.CHAT:
        sections["output"] = (
            "## OUTPUT FORMAT\n"
            "Respond naturally. For simple replies (greetings, follow-ups, opinions), "
            "call the `final_answer` tool directly with your response. "
            "Use other tools first only when the user's request requires them."
        )
    else:
        sections["output"] = (
            "## OUTPUT FORMAT\n"
            "For simple replies (greetings, acknowledgments, or when no tools are needed), "
            "call the `final_answer` tool directly with your response. "
            "Never respond with plain text outside a tool call."
        )

    ordered_keys = sorted(
        sections.keys(),
        key=lambda k: SECTION_PRIORITY.get(k, 99),
    )

    total_tokens = sum(_estimate_tokens(sections[k]) for k in ordered_keys)
    logger.info(
        "System prompt: ~%d tokens across %d sections", total_tokens, len(ordered_keys)
    )

    return "\n\n---\n\n".join(sections[k] for k in ordered_keys)
