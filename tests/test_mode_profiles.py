from ouro_agents.modes.profiles import (
    AUTONOMOUS,
    AUTONOMOUS_ACTION_PRELOADS,
    CHAT,
    CHAT_DISCOVERY_PRELOADS,
    CHAT_REPLY,
    RunMode,
    resolve_mode_profile,
)


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


def test_chat_modes_preload_read_only_org_discovery_tools():
    assert CHAT.preload_tools == CHAT_DISCOVERY_PRELOADS
    assert CHAT_REPLY.preload_tools == CHAT_DISCOVERY_PRELOADS
    assert resolve_mode_profile(RunMode.CHAT).preload_tools == [
        "ouro:get_organizations",
        "ouro:get_teams",
    ]
