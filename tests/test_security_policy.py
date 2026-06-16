from ouro_agents.modes.profiles import AUTONOMOUS, apply_capability_envelope
from ouro_agents.security.policy import (
    ActorRole,
    Capability,
    EventSurface,
    actor_role_for,
    describe_capabilities,
    resolve_envelope,
)


def test_controller_comment_drives_real_work():
    # A controller's comment is trusted input, so the surface no longer clamps:
    # the controller gets full role capabilities, same as a direct chat.
    envelope = resolve_envelope(ActorRole.CONTROLLER, EventSurface.COMMENT)

    assert envelope.allows(Capability.CREATE_ASSET)
    assert envelope.allows(Capability.UPDATE_ASSET)
    assert envelope.allows(Capability.EXECUTE_ROUTE)
    assert envelope.allows(Capability.MANAGE_QUEST)
    assert envelope.allows(Capability.LOAD_MCP_TOOL)
    assert not envelope.is_restricted


def test_controller_mention_is_not_clamped_by_surface():
    envelope = resolve_envelope(ActorRole.CONTROLLER, EventSurface.MENTION)

    assert envelope.allows(Capability.CREATE_ASSET)
    assert envelope.allows(Capability.LOAD_MCP_TOOL)
    assert envelope.allows(Capability.EXECUTE_ROUTE)
    assert envelope.allows(Capability.UPDATE_ASSET)


def test_trusted_comment_can_create_and_update():
    envelope = resolve_envelope(ActorRole.TRUSTED, EventSurface.COMMENT)

    assert envelope.allows(Capability.READ_PLATFORM)
    assert envelope.allows(Capability.REPLY)
    assert envelope.allows(Capability.CREATE_ASSET)
    assert envelope.allows(Capability.UPDATE_ASSET)
    assert envelope.allows(Capability.LOAD_MCP_TOOL)
    # Trusted role caps still exclude code/route/delegate even when unclamped.
    assert not envelope.allows(Capability.EXECUTE_ROUTE)
    assert not envelope.allows(Capability.RUN_SHELL)


def test_explicit_event_ceiling_still_caps_controller():
    # An explicit per-event ceiling is a hard cap that applies regardless of
    # role, so it cannot be bypassed by the trusted-actor elevation.
    envelope = resolve_envelope(
        ActorRole.CONTROLLER,
        EventSurface.COMMENT,
        surface_capabilities={Capability.READ_PLATFORM, Capability.REPLY},
    )

    assert envelope.allowed_capabilities == {
        Capability.READ_PLATFORM,
        Capability.REPLY,
    }


def test_public_mention_stays_read_reply_only():
    envelope = resolve_envelope(ActorRole.PUBLIC, EventSurface.MENTION)

    assert envelope.allowed_capabilities == {
        Capability.READ_PLATFORM,
        Capability.REPLY,
    }


def test_describe_capabilities_renders_restrictions():
    envelope = resolve_envelope(ActorRole.PUBLIC, EventSurface.COMMENT)

    note = describe_capabilities(envelope.allowed_capabilities)

    assert "## Run Capabilities" in note
    assert "read platform content" in note
    assert "create new assets" in note  # listed under "cannot"
    assert "cannot" in note


def test_describe_capabilities_empty_when_unrestricted():
    envelope = resolve_envelope(ActorRole.CONTROLLER, EventSurface.DIRECT_CHAT)

    assert describe_capabilities(envelope.allowed_capabilities) == ""


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
