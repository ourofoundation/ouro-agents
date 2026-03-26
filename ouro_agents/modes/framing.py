"""Mode framing text and output format builders.

Each run mode has a framing string (injected as ## MODE in the system prompt)
and an output format section that tells the LLM how to return results.

Framing strings are static.  Output format is *mostly* static per mode,
except CHAT_REPLY which varies based on whether ``send_message`` is preloaded.
"""

# ---------------------------------------------------------------------------
# Framing text (one per mode)
# ---------------------------------------------------------------------------

CHAT_FRAMING = (
    "You are in a conversation. Your primary goal is to help the person you're talking to. "
    "Be conversational, clear, and concise. Ask clarifying questions when a request is ambiguous. "
    "Use other tools when the request calls for it; when you do, say what you found or did."
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
    "and ongoing work, then create or revise your plan for the upcoming period. "
    "Be thoughtful and realistic. Use checklist format ([] / [x]) for actionable items. "
    "If updating an existing plan, revise the post in place rather than creating a new one. "
    "Do NOT execute any plan items or do actual work — your only job is to write "
    "the plan and publish it as a post."
)

REVIEW_FRAMING = (
    "You have a pending plan that may have received human feedback. "
    "Check for comments on the plan post, incorporate any feedback, "
    "and finalize the plan. "
    "Do NOT execute plan items — only check for feedback, revise if needed, and report."
)

# ---------------------------------------------------------------------------
# Output format text (one per mode, static portion)
# ---------------------------------------------------------------------------

CHAT_OUTPUT = (
    "## OUTPUT FORMAT\n"
    "This is a local/ad-hoc chat run. Respond with `final_answer` only. "
    "Do not call `send_message` unless the task explicitly tells you to post into an Ouro conversation."
)

AUTONOMOUS_OUTPUT = (
    "## OUTPUT FORMAT\n"
    "For simple replies (greetings, acknowledgments, or when no tools are needed), "
    "call the `final_answer` tool directly with your response. "
    "Never respond with plain text outside a tool call. "
    "Never emit pseudo-tool syntax such as 'Calling tools:' or handwritten JSON."
)

HEARTBEAT_OUTPUT = AUTONOMOUS_OUTPUT

PLAN_OUTPUT = (
    "## OUTPUT FORMAT\n"
    "Create or update your plan post, then call `final_answer` with structured JSON. "
    "Do NOT use any other tools — only create_post (or update_post) and final_answer."
)

REVIEW_OUTPUT = (
    "## OUTPUT FORMAT\n"
    "Check for feedback, revise the plan if needed, then call `final_answer` with structured JSON. "
    "Do NOT use any tools besides get_comments, create_comment, update_post, and final_answer."
)

# CHAT_REPLY is dynamic — see build_output_format() below.
_CHAT_REPLY_PRELOADED = (
    "## OUTPUT FORMAT\n"
    "If the task or context includes an Ouro `conversation_id` (or you are clearly in an Ouro chat): "
    "post your reply by calling `send_message` with the real `conversation_id` and reply text "
    "(already loaded). "
    "Then call `final_answer` with the **same** text as `send_message` so streaming and "
    "local logs match the message you posted. If you should not reply, call `final_answer` with exactly "
    "NO_ACTION only (do not call `send_message`).\n"
    "If there is no Ouro conversation_id (e.g. ad-hoc API run), respond with `final_answer` only."
)

_CHAT_REPLY_NOT_PRELOADED = (
    "## OUTPUT FORMAT\n"
    "If the task or context includes an Ouro `conversation_id` (or you are clearly in an Ouro chat): "
    "load `ouro:send_message`, then call `send_message` with the real `conversation_id` and reply text. "
    "Then call `final_answer` with the **same** text as `send_message` so streaming and "
    "local logs match the message you posted. If you should not reply, call `final_answer` with exactly "
    "NO_ACTION only (do not call `send_message`).\n"
    "If there is no Ouro conversation_id (e.g. ad-hoc API run), respond with `final_answer` only."
)


def build_output_format(
    output_format: str,
    mode_name: str,
    preloaded_tool_names: list[str] | None = None,
) -> str:
    """Return the output format section for a mode.

    Most modes use their static ``output_format`` directly.  ``chat-reply``
    varies based on whether ``send_message`` is already preloaded.
    """
    if mode_name == "chat-reply":
        preloaded = set(preloaded_tool_names or [])
        if "send_message" in preloaded:
            return _CHAT_REPLY_PRELOADED
        return _CHAT_REPLY_NOT_PRELOADED
    return output_format
