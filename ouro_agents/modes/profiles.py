"""Mode profile definitions.

A ModeProfile defines how a run mode behaves: its prompt framing, tool access,
resource limits, and behavioral flags.  This mirrors SubAgentProfile — a single
declarative object that captures everything about a mode, replacing scattered
conditionals throughout agent.py and soul.py.

Built-in profiles cover the six core modes.  User config can override
``max_steps`` and ``preload_tools`` per mode via ``ModeOverride``.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .framing import (
    AUTONOMOUS_FRAMING,
    AUTONOMOUS_OUTPUT,
    CHAT_FRAMING,
    CHAT_OUTPUT,
    HEARTBEAT_FRAMING,
    HEARTBEAT_OUTPUT,
    PLAN_OUTPUT,
    PLANNING_FRAMING,
    REVIEW_FRAMING,
    REVIEW_OUTPUT,
)


class RunMode(str, Enum):
    CHAT = "chat"
    CHAT_REPLY = "chat-reply"
    AUTONOMOUS = "autonomous"
    HEARTBEAT = "heartbeat"
    PLAN = "plan"
    REVIEW = "review"


class ModeProfile(BaseModel):
    """Declarative definition of a run mode's behavior."""

    name: str
    framing: str = ""
    output_format: str = ""

    # Run config
    max_steps: int = 20
    preload_tools: list[str] = Field(default_factory=list)

    # Tool access: when True, MCP tools are filtered to ``default_servers`` only
    restricted_servers: bool = False
    default_servers: list[str] = Field(default_factory=lambda: ["ouro"])
    # None = all memory tools available; list = restrict to these names
    memory_tool_filter: list[str] | None = None

    # Prompt assembly
    lightweight: bool = False
    skip_preflight: bool = False
    skip_post_reflection: bool = False
    load_conversation_state: bool = False
    load_scheduled_tasks: bool = False
    include_chat_conversation_id: bool = False

    # Conversation turn persistence
    append_conversation_turns: bool = True
    update_conversation_state: bool = False

    # Chat-reply conversation-id annotation style (None = don't add)
    conversation_id_annotation: Optional[str] = None


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

CHAT = ModeProfile(
    name="chat",
    framing=CHAT_FRAMING,
    output_format=CHAT_OUTPUT,
    max_steps=20,
    preload_tools=[],
    load_conversation_state=True,
    include_chat_conversation_id=True,
    skip_preflight=True,
    skip_post_reflection=True,
    append_conversation_turns=True,
    update_conversation_state=True,
    conversation_id_annotation=(
        "conversation memory/history only; respond with `final_answer` "
        "unless explicitly told to post"
    ),
)

CHAT_REPLY = ModeProfile(
    name="chat-reply",
    framing=CHAT_FRAMING,
    output_format="",  # dynamic — see framing.CHAT_REPLY_OUTPUT
    max_steps=20,
    preload_tools=[],
    load_conversation_state=True,
    include_chat_conversation_id=True,
    skip_preflight=True,
    skip_post_reflection=True,
    append_conversation_turns=True,
    update_conversation_state=True,
    conversation_id_annotation=(
        "your reply will be posted automatically — just call `final_answer`"
    ),
)

AUTONOMOUS = ModeProfile(
    name="autonomous",
    framing=AUTONOMOUS_FRAMING,
    output_format=AUTONOMOUS_OUTPUT,
    max_steps=20,
)

HEARTBEAT = ModeProfile(
    name="heartbeat",
    framing=HEARTBEAT_FRAMING,
    output_format=HEARTBEAT_OUTPUT,
    max_steps=20,
    preload_tools=["ouro:get_asset", "ouro:create_comment"],
    restricted_servers=True,
    lightweight=True,
    skip_preflight=True,
    load_scheduled_tasks=False,
    append_conversation_turns=False,
)

PLAN = ModeProfile(
    name="plan",
    framing=PLANNING_FRAMING,
    output_format=PLAN_OUTPUT,
    max_steps=6,
    restricted_servers=True,
    memory_tool_filter=["memory_recall"],
    lightweight=True,
    skip_preflight=True,
    skip_post_reflection=True,
    append_conversation_turns=False,
)

REVIEW = ModeProfile(
    name="review",
    framing=REVIEW_FRAMING,
    output_format=REVIEW_OUTPUT,
    max_steps=6,
    restricted_servers=True,
    memory_tool_filter=["memory_recall"],
    lightweight=True,
    skip_preflight=True,
    skip_post_reflection=True,
    append_conversation_turns=False,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PROFILES = [CHAT, CHAT_REPLY, AUTONOMOUS, HEARTBEAT, PLAN, REVIEW]

MODE_REGISTRY: dict[RunMode, ModeProfile] = {
    RunMode.CHAT: CHAT,
    RunMode.CHAT_REPLY: CHAT_REPLY,
    RunMode.AUTONOMOUS: AUTONOMOUS,
    RunMode.HEARTBEAT: HEARTBEAT,
    RunMode.PLAN: PLAN,
    RunMode.REVIEW: REVIEW,
}


def resolve_mode_profile(mode: RunMode) -> ModeProfile:
    """Look up the built-in profile for a run mode."""
    return MODE_REGISTRY[mode]


def apply_mode_override(profile: ModeProfile, override) -> ModeProfile:
    """Return a copy of *profile* with user config overrides applied.

    *override* should be a ``ModeOverride`` instance (from config).
    """
    updates: dict = {}
    if override.max_steps is not None:
        updates["max_steps"] = override.max_steps
    if override.preload_tools is not None:
        updates["preload_tools"] = override.preload_tools
    if updates:
        return profile.model_copy(update=updates)
    return profile
