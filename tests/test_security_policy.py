from ouro_agents.modes.profiles import AUTONOMOUS, apply_capability_envelope
from ouro_agents.security.policy import (
    ActorRole,
    Capability,
    EventSurface,
    actor_role_for,
    resolve_envelope,
)


def test_public_comment_is_read_reply_only():
    envelope = resolve_envelope(ActorRole.PUBLIC, EventSurface.COMMENT)

    assert envelope.allowed_capabilities == {
        Capability.READ_PLATFORM,
        Capability.REPLY,
    }
    assert not envelope.allows(Capability.EXECUTE_ROUTE)
    assert not envelope.allows(Capability.MEMORY_WRITE)


def test_controller_direct_chat_keeps_broad_capabilities():
    envelope = resolve_envelope(ActorRole.CONTROLLER, EventSurface.DIRECT_CHAT)

    assert envelope.allows(Capability.EXECUTE_ROUTE)
    assert envelope.allows(Capability.DELEGATE)
    assert envelope.allows(Capability.RUN_PYTHON)
    assert envelope.allows(Capability.MEMORY_WRITE)


def test_actor_role_uses_stable_ids_before_agent_flag():
    assert (
        actor_role_for(
            actor_user_id="controller-1",
            actor_is_agent=True,
            controller_user_ids=["controller-1"],
        )
        is ActorRole.CONTROLLER
    )
    assert (
        actor_role_for(
            actor_user_id="agent-1",
            actor_is_agent=True,
            controller_user_ids=[],
            trusted_user_ids=[],
        )
        is ActorRole.AGENT
    )


def test_capability_envelope_only_narrows_mode_profile():
    envelope = resolve_envelope(ActorRole.PUBLIC, EventSurface.COMMENT)

    profile = apply_capability_envelope(AUTONOMOUS, envelope)

    assert profile.allowed_capabilities == envelope.allowed_capabilities
    assert profile.allow_delegation is False
    assert "ouro:get_asset" in profile.preload_tools
    assert "ouro:execute_route" not in profile.preload_tools
    assert profile.memory_tool_filter is None


def test_trusted_direct_chat_does_not_become_memory_only():
    envelope = resolve_envelope(ActorRole.TRUSTED, EventSurface.DIRECT_CHAT)

    profile = apply_capability_envelope(AUTONOMOUS, envelope)

    assert profile.allows_capability(Capability.LOAD_MCP_TOOL)
    assert not profile.allows_capability(Capability.MEMORY_WRITE)
    assert profile.memory_tool_filter is None
    assert "ouro:execute_route" not in profile.preload_tools
