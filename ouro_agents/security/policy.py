from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Capability(str, Enum):
    READ_PLATFORM = "read_platform"
    REPLY = "reply"
    CREATE_ASSET = "create_asset"
    UPDATE_ASSET = "update_asset"
    MANAGE_QUEST = "manage_quest"
    EXECUTE_ROUTE = "execute_route"
    SEND_MESSAGE = "send_message"
    SCHEDULE = "schedule"
    DELEGATE = "delegate"
    RUN_PYTHON = "run_python"
    RUN_SHELL = "run_shell"
    MEMORY_WRITE = "memory_write"
    EXTERNAL_SEARCH = "external_search"
    LOAD_MCP_TOOL = "load_mcp_tool"


class ActorRole(str, Enum):
    CONTROLLER = "controller"
    TRUSTED = "trusted"
    PUBLIC = "public"
    AGENT = "agent"
    UNKNOWN = "unknown"


class EventSurface(str, Enum):
    DIRECT_CHAT = "direct_chat"
    API_RUN = "api_run"
    COMMENT = "comment"
    MENTION = "mention"
    HEARTBEAT = "heartbeat"
    PLAN_REVIEW = "plan_review"
    CLEANUP = "cleanup"
    UNKNOWN = "unknown"


ALL_CAPABILITIES = frozenset(Capability)
READ_REPLY_CAPABILITIES = frozenset(
    {
        Capability.READ_PLATFORM,
        Capability.REPLY,
    }
)
NO_ACTION_CAPABILITIES: frozenset[Capability] = frozenset()
# Mentions are direct summons — often "make me a post/dataset" — so the surface
# allows asset creation. The role intersection still holds public commenters
# and other agents to read + reply; only trusted/controller actors gain
# CREATE_ASSET here. LOAD_MCP_TOOL is required because creation tools
# (create_post, create_dataset, ...) live behind load_tool.
MENTION_CAPABILITIES = READ_REPLY_CAPABILITIES | {
    Capability.CREATE_ASSET,
    Capability.LOAD_MCP_TOOL,
}

ROLE_CAPABILITIES: dict[ActorRole, frozenset[Capability]] = {
    ActorRole.CONTROLLER: ALL_CAPABILITIES,
    ActorRole.TRUSTED: frozenset(
        {
            Capability.READ_PLATFORM,
            Capability.REPLY,
            Capability.CREATE_ASSET,
            Capability.UPDATE_ASSET,
            Capability.MANAGE_QUEST,
            Capability.SEND_MESSAGE,
            Capability.EXTERNAL_SEARCH,
            Capability.LOAD_MCP_TOOL,
        }
    ),
    ActorRole.PUBLIC: READ_REPLY_CAPABILITIES,
    ActorRole.AGENT: READ_REPLY_CAPABILITIES,
    ActorRole.UNKNOWN: READ_REPLY_CAPABILITIES,
}

SURFACE_CAPABILITIES: dict[EventSurface, frozenset[Capability]] = {
    EventSurface.DIRECT_CHAT: ALL_CAPABILITIES,
    EventSurface.API_RUN: ALL_CAPABILITIES,
    EventSurface.COMMENT: READ_REPLY_CAPABILITIES,
    EventSurface.MENTION: MENTION_CAPABILITIES,
    EventSurface.HEARTBEAT: ALL_CAPABILITIES,
    EventSurface.PLAN_REVIEW: frozenset(
        {
            Capability.READ_PLATFORM,
            Capability.REPLY,
            Capability.MANAGE_QUEST,
            Capability.UPDATE_ASSET,
            Capability.LOAD_MCP_TOOL,
            Capability.MEMORY_WRITE,
        }
    ),
    EventSurface.CLEANUP: NO_ACTION_CAPABILITIES,
    EventSurface.UNKNOWN: READ_REPLY_CAPABILITIES,
}


@dataclass(frozen=True)
class CapabilityEnvelope:
    allowed_capabilities: frozenset[Capability]
    role: ActorRole = ActorRole.UNKNOWN
    surface: EventSurface = EventSurface.UNKNOWN
    reason: str = ""

    def allows(self, capability: Capability | str) -> bool:
        return Capability(capability) in self.allowed_capabilities

    @property
    def is_restricted(self) -> bool:
        return self.allowed_capabilities != ALL_CAPABILITIES


_CAPABILITY_PHRASES: dict[Capability, str] = {
    Capability.READ_PLATFORM: "read platform content",
    Capability.REPLY: "reply with comments",
    Capability.CREATE_ASSET: "create new assets (posts, files, datasets)",
    Capability.UPDATE_ASSET: "update existing assets",
    Capability.MANAGE_QUEST: "manage quests",
    Capability.EXECUTE_ROUTE: "execute routes",
    Capability.SEND_MESSAGE: "send conversation messages",
    Capability.SCHEDULE: "schedule tasks",
    Capability.DELEGATE: "delegate to subagents",
    Capability.RUN_PYTHON: "run Python code",
    Capability.RUN_SHELL: "run shell commands",
    Capability.MEMORY_WRITE: "write memory",
    Capability.EXTERNAL_SEARCH: "search the web",
    Capability.LOAD_MCP_TOOL: "load additional MCP tools",
}


def describe_capabilities(allowed: frozenset[Capability]) -> str:
    """Render a capability set as a prompt section so the agent knows its
    boundaries instead of inferring them from missing tools."""
    if allowed == ALL_CAPABILITIES:
        return ""
    can = [_CAPABILITY_PHRASES[c] for c in Capability if c in allowed]
    cannot = [_CAPABILITY_PHRASES[c] for c in Capability if c not in allowed]
    lines = ["## Run Capabilities"]
    lines.append(f"This run can: {'; '.join(can)}.")
    if cannot:
        lines.append(f"This run cannot: {'; '.join(cannot)}.")
    lines.append(
        "If the request needs something this run cannot do, say so plainly in "
        "your reply instead of improvising a workaround or presenting a "
        "partial substitute as the requested deliverable."
    )
    return "\n".join(lines)


def _id_set(values: Iterable[str] | None) -> set[str]:
    return {str(value).strip() for value in values or [] if str(value).strip()}


def actor_role_for(
    *,
    actor_user_id: str | None,
    actor_is_agent: bool | None = None,
    controller_user_ids: Iterable[str] | None = None,
    trusted_user_ids: Iterable[str] | None = None,
) -> ActorRole:
    actor_id = (actor_user_id or "").strip()
    if actor_id:
        if actor_id in _id_set(controller_user_ids):
            return ActorRole.CONTROLLER
        if actor_id in _id_set(trusted_user_ids):
            return ActorRole.TRUSTED
        if actor_is_agent:
            return ActorRole.AGENT
        return ActorRole.PUBLIC
    if actor_is_agent:
        return ActorRole.AGENT
    return ActorRole.UNKNOWN


def capabilities_for_surface(
    surface: EventSurface | str,
    override: Iterable[Capability | str] | None = None,
) -> frozenset[Capability]:
    if override is not None:
        return frozenset(Capability(capability) for capability in override)
    try:
        event_surface = EventSurface(surface)
    except ValueError:
        event_surface = EventSurface.UNKNOWN
    return SURFACE_CAPABILITIES.get(event_surface, READ_REPLY_CAPABILITIES)


def resolve_envelope(
    role: ActorRole | str,
    surface: EventSurface | str,
    *,
    surface_capabilities: Iterable[Capability | str] | None = None,
) -> CapabilityEnvelope:
    try:
        actor_role = ActorRole(role)
    except ValueError:
        actor_role = ActorRole.UNKNOWN
    try:
        event_surface = EventSurface(surface)
    except ValueError:
        event_surface = EventSurface.UNKNOWN
    role_caps = ROLE_CAPABILITIES.get(actor_role, READ_REPLY_CAPABILITIES)
    surface_caps = capabilities_for_surface(event_surface, surface_capabilities)
    allowed = role_caps & surface_caps
    return CapabilityEnvelope(
        allowed_capabilities=frozenset(allowed),
        role=actor_role,
        surface=event_surface,
        reason=f"{actor_role.value}:{event_surface.value}",
    )
