"""Mode framing text and output format builders.

Each run mode has a framing string (injected as ## MODE in the system prompt)
and an output format section that tells the LLM how to return results.
"""

# ---------------------------------------------------------------------------
# Framing text (one per mode)
# ---------------------------------------------------------------------------

CHAT_FRAMING = (
    "You are in a conversation. Your primary goal is to help the person you're talking to. "
    "Be conversational, clear, and concise. Ask clarifying questions when a request is ambiguous. "
    "Use other tools when the request calls for it."
)

AUTONOMOUS_FRAMING = (
    "You are operating autonomously to complete a task. "
    "Work through the task step by step, using tools as needed. "
    "Report what you accomplished when finished."
)

HEARTBEAT_FRAMING = (
    "You are running an autonomous heartbeat. Review your context and playbook, "
    "then decide what's most valuable to do right now. Be genuine and thoughtful "
    "— quality over quantity. Treat each heartbeat like a bounded work session: "
    "prefer one meaningful slice of progress over trying to finish an entire "
    "multi-step plan in one run. If nothing feels worth doing, it's okay to pass.\n\n"
    "When creating posts, write like a person with something to say — not like an AI "
    "producing content. Prose over bullet lists. Have a point of view. Skip the "
    "preamble and engagement bait. Delegate to the `writer` subagent for drafting."
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

EXTENDED_MARKDOWN_INSTRUCTIONS = """
**Writing Ouro messages** — use extended markdown in your `final_answer`:
- **Mention users**: @username
- **Link to assets**: prefer markdown shorthands `[label](asset:<uuid>)` or typed `[label](post:<uuid>)`, `[label](file:<uuid>)`, `[label](dataset:<uuid>)`, `[label](route:<uuid>)`, `[label](service:<uuid>)` — the server resolves these to canonical URLs. If a tool response includes a `url`, you may paste that exact URL; never invent path segments or use placeholders like `entity` in URLs.
- **Embed assets** (block-level): ```assetComponent
  {"id": "<uuid>", "assetType": "post"|"file"|"dataset"|"route"|"service", "viewMode": "preview"|"card", "visualizationId": "<uuid>|null"}
  ``` — use search_assets() or get_asset() for IDs; prefer viewMode "preview" for files/datasets. For datasets, set visualizationId to render a specific saved dataset view. Use the exact keys `id`, `assetType`, and `viewMode` here; do not use legacy embed keys like `asset_id`, `asset_type`, or `type`.
- **Standard markdown**: headings, **bold**, *italic*, lists, code blocks, tables, links
- **Math**: $inline$ and $$display$$ LaTeX
""".strip()

CHAT_OUTPUT = (
    "## OUTPUT FORMAT\n"
    "This is a local/ad-hoc chat run. Respond with `final_answer` only.\n"
    "Never respond with plain text outside a tool call. "
    "Never emit pseudo-tool syntax such as 'Calling tools:' or handwritten JSON.\n\n"
    f"{EXTENDED_MARKDOWN_INSTRUCTIONS}"
)

AUTONOMOUS_OUTPUT = (
    "## OUTPUT FORMAT\n"
    "For simple replies (greetings, acknowledgments, or when no tools are needed), "
    "call the `final_answer` tool directly with your response. "
    "Never respond with plain text outside a tool call. "
    "Never emit pseudo-tool syntax such as 'Calling tools:' or handwritten JSON.\n\n"
    f"{EXTENDED_MARKDOWN_INSTRUCTIONS}"
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

CHAT_REPLY_OUTPUT = (
    "## OUTPUT FORMAT\n"
    "Your reply is posted to the conversation automatically when you call `final_answer`. "
    "Do NOT call `send_message` — the server persists your response for you.\n"
    "Never respond with plain text outside a tool call. "
    "Never emit pseudo-tool syntax such as 'Calling tools:' or handwritten JSON.\n\n"
    f"{EXTENDED_MARKDOWN_INSTRUCTIONS}"
)


def build_output_format(
    output_format: str,
    mode_name: str,
    preloaded_tool_names: list[str] | None = None,
) -> str:
    """Return the output format section for a mode."""
    if mode_name == "chat-reply":
        return CHAT_REPLY_OUTPUT
    return output_format
