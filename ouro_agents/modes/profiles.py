"""Mode profile definitions.

A ModeProfile defines how a run mode behaves: its prompt framing, tool access,
resource limits, and behavioral flags.  This mirrors SubAgentProfile — a single
declarative object that captures everything about a mode, replacing scattered
conditionals throughout agent.py and soul.py.

Built-in profiles cover the five core modes.  User config can override
``max_steps`` and ``preload_tools`` per mode via ``ModeOverride``.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from ..security.policy import Capability, CapabilityEnvelope
from ..security.tool_capabilities import capability_for_tool
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
    # Deferred MCP tools removed from this mode entirely (directory + load_tool)
    excluded_tools: list[str] = Field(default_factory=list)
    allow_delegation: bool = True
    # None means no capability envelope has been applied; otherwise this set
    # is the authoritative upper bound for tools exposed by the profile.
    allowed_capabilities: frozenset[Capability] | None = None
    # None = all memory tools available; list = restrict to these names
    memory_tool_filter: list[str] | None = None

    # Prompt assembly
    # Conversational modes answer a person directly; the MODE framing governs
    # when to act. The shared preamble drops its "produce the result before
    # reporting back" work directive for these so the two don't pull opposite
    # ways on casual messages.
    conversational: bool = False
    lightweight: bool = False
    skip_preflight: bool = False
    skip_post_reflection: bool = False
    load_conversation_state: bool = False
    load_scheduled_tasks: bool = False
    include_chat_conversation_id: bool = False

    # Conversation turn persistence
    append_conversation_turns: bool = True
    update_conversation_state: bool = False

    # Conversation-id annotation style (None = don't add)
    conversation_id_annotation: Optional[str] = None

    def allows_capability(self, capability: Capability | str) -> bool:
        if self.allowed_capabilities is None:
            return True
        return Capability(capability) in self.allowed_capabilities


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

AUTONOMOUS_ACTION_PRELOADS = [
    "ouro:search_assets",
    "ouro:get_asset",
    "ouro:execute_route",
    "ouro:get_action",
]

# The org/team membership the discovery tools return is already in the platform
# context, so preloading them wastes the slot. Preload the genuine hot path
# instead: find things, inspect them, run a route and read its result, and the
# single most common chat side-effect (replying with a comment). Anything heavier
# or rarer (create_post, query_dataset, dataset views) stays one load_tool away.
CHAT_HOTPATH_PRELOADS = [
    "ouro:search_assets",
    "ouro:get_asset",
    "ouro:execute_route",
    "ouro:get_action",
    "ouro:write_comment",
]

# Chat runs ARE the conversation: the host injects history and posts the
# final reply, so platform messaging tools are pure foot-guns there
# (double-posting, reading the conversation it's already in). Removing them
# beats prompting "do not call send_message" in three places.
CHAT_EXCLUDED_TOOLS = [
    "ouro:list_conversations",
    "ouro:get_conversation",
    "ouro:get_conversations",
    "ouro:create_conversation",
    "ouro:send_message",
    "ouro:list_messages",
]


# Chat skips preflight for lower reply latency. Hot-path tools are
# preloaded statically; post-run reflection still curates memory in a
# background thread so it adds no reply latency. The trivial-message
# regex still fast-paths greetings.
CHAT = ModeProfile(
    name="chat",
    framing=CHAT_FRAMING,
    output_format=CHAT_OUTPUT,
    max_steps=20,
    preload_tools=CHAT_HOTPATH_PRELOADS,
    excluded_tools=CHAT_EXCLUDED_TOOLS,
    conversational=True,
    skip_preflight=True,
    load_conversation_state=True,
    include_chat_conversation_id=True,
    append_conversation_turns=False,
    update_conversation_state=True,
    conversation_id_annotation="this conversation's history and memory",
)

AUTONOMOUS = ModeProfile(
    name="autonomous",
    framing=AUTONOMOUS_FRAMING,
    output_format=AUTONOMOUS_OUTPUT,
    max_steps=40,
    preload_tools=AUTONOMOUS_ACTION_PRELOADS,
)

HEARTBEAT = ModeProfile(
    name="heartbeat",
    framing=HEARTBEAT_FRAMING,
    output_format=HEARTBEAT_OUTPUT,
    max_steps=20,
    preload_tools=[
        "ouro:search_assets",
        "ouro:get_asset",
        "ouro:write_comment",
        "ouro:create_post",
    ],
    lightweight=True,
    skip_preflight=True,
    load_scheduled_tasks=False,
    append_conversation_turns=False,
)

PLAN = ModeProfile(
    name="plan",
    framing=PLANNING_FRAMING,
    output_format=PLAN_OUTPUT,
    max_steps=20,
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
    max_steps=12,
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

PROFILES = [CHAT, AUTONOMOUS, HEARTBEAT, PLAN, REVIEW]

MODE_REGISTRY: dict[RunMode, ModeProfile] = {
    RunMode.CHAT: CHAT,
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


def _filter_preload_tools(
    preload_tools: list[str],
    allowed_capabilities: frozenset[Capability],
) -> list[str]:
    filtered: list[str] = []
    for tool_name in preload_tools:
        capability = capability_for_tool(tool_name)
        if capability is not None and capability in allowed_capabilities:
            filtered.append(tool_name)
    return filtered


def apply_capability_envelope(
    profile: ModeProfile,
    envelope: CapabilityEnvelope,
) -> ModeProfile:
    """Return a profile narrowed by a capability envelope.

    The envelope can only subtract. It never expands a profile's explicit
    capability set, tool preloads, delegation, or memory-write behavior.
    """
    if profile.allowed_capabilities is None:
        allowed = envelope.allowed_capabilities
    else:
        allowed = profile.allowed_capabilities & envelope.allowed_capabilities

    updates: dict = {
        "allowed_capabilities": frozenset(allowed),
        "preload_tools": _filter_preload_tools(profile.preload_tools, frozenset(allowed)),
    }

    if Capability.DELEGATE not in allowed:
        updates["allow_delegation"] = False

    if Capability.MEMORY_WRITE not in allowed and profile.memory_tool_filter is not None:
        updates["memory_tool_filter"] = [
            name for name in profile.memory_tool_filter if name != "remember"
        ]

    return profile.model_copy(update=updates)
