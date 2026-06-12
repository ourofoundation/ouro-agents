from ouro_agents.modes.profiles import (
    AUTONOMOUS,
    AUTONOMOUS_ACTION_PRELOADS,
    CHAT,
    CHAT_HOTPATH_PRELOADS,
    CHAT_REPLY,
    RunMode,
    apply_capability_envelope,
    resolve_mode_profile,
)
from ouro_agents.security.policy import ActorRole, EventSurface, resolve_envelope


def test_autonomous_mode_preloads_core_ouro_action_tools():
    assert AUTONOMOUS.preload_tools == AUTONOMOUS_ACTION_PRELOADS
    assert resolve_mode_profile(RunMode.AUTONOMOUS).preload_tools == [
        "ouro:search_assets",
        "ouro:get_asset",
        "ouro:execute_route",
        "ouro:get_action",
    ]


def test_chat_modes_keep_subagents_available_for_explicit_work():
    assert CHAT.allow_delegation is True
    assert CHAT_REPLY.allow_delegation is True
    assert resolve_mode_profile(RunMode.CHAT).allow_delegation is True
    assert resolve_mode_profile(RunMode.CHAT_REPLY).allow_delegation is True


def test_chat_modes_preload_hotpath_tools():
    assert CHAT.preload_tools == CHAT_HOTPATH_PRELOADS
    assert CHAT_REPLY.preload_tools == CHAT_HOTPATH_PRELOADS
    assert resolve_mode_profile(RunMode.CHAT).preload_tools == [
        "ouro:search_assets",
        "ouro:get_asset",
        "ouro:execute_route",
        "ouro:get_action",
        "ouro:create_comment",
    ]


def test_chat_modes_are_conversational_and_work_modes_are_not():
    assert CHAT.conversational is True
    assert CHAT_REPLY.conversational is True
    assert AUTONOMOUS.conversational is False


def test_public_comment_envelope_filters_autonomous_preloads():
    profile = apply_capability_envelope(
        AUTONOMOUS,
        resolve_envelope(ActorRole.PUBLIC, EventSurface.COMMENT),
    )

    assert profile.preload_tools == [
        "ouro:search_assets",
        "ouro:get_asset",
        "ouro:get_action",
    ]
    assert profile.allow_delegation is False
