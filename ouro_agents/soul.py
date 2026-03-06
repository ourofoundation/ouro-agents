from pathlib import Path

from .config import RunMode


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
    "- If a tool call fails, fix arguments and retry before giving up.\n"
    "- Prefer fully-qualified names like `ouro:create_post` when calling `load_tool`.\n"
    "- For content/topic questions on Ouro (e.g. 'what's new in X?'), usually use `ouro:search_assets` "
    "(and optionally `ouro:get_team_activity`)."
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


def build_prompt(
    soul: str,
    notes: str,
    skills: str,
    memory_context: str,
    mode: RunMode = RunMode.AUTONOMOUS,
    conversation_context: str = "",
    deferred_tool_directory: str = "",
) -> str:
    """Assemble the full system prompt, framed for the given RunMode."""
    sections: list[str] = []

    sections.append(f"## MODE\n{_MODE_FRAMING[mode]}")

    if soul:
        sections.append(f"## IDENTITY AND RULES (SOUL)\n{soul}")

    if notes:
        sections.append(f"## DEPLOYMENT CONTEXT (NOTES)\n{notes}")

    if skills:
        sections.append(f"## SKILLS AND KNOWLEDGE\n{skills}")

    if memory_context:
        sections.append(f"## RELEVANT MEMORIES\n{memory_context}")

    if conversation_context:
        sections.append(
            f"## RECENT CONVERSATION (most recent last)\n{conversation_context}"
        )

    if deferred_tool_directory:
        sections.append(
            f"## MCP TOOL USAGE RULES\n{MCP_TOOL_RULES}\n\n"
            f"## DEFERRED TOOL DIRECTORY (name + short description)\n{deferred_tool_directory}"
        )

    if mode == RunMode.CHAT:
        sections.append(
            "## OUTPUT FORMAT\n"
            "Respond naturally. For simple replies (greetings, follow-ups, opinions), "
            "call the `final_answer` tool directly with your response. "
            "Use other tools first only when the user's request requires them."
        )
    else:
        sections.append(
            "## OUTPUT FORMAT\n"
            "For simple replies (greetings, acknowledgments, or when no tools are needed), "
            "call the `final_answer` tool directly with your response. "
            "Never respond with plain text outside a tool call."
        )

    return "\n\n---\n\n".join(sections)
