from ouro_agents.modes.profiles import (
    AUTONOMOUS,
    AUTONOMOUS_ACTION_PRELOADS,
    CHAT,
    DREAM,
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


def test_chat_mode_keeps_subagents_available_for_explicit_work():
    assert CHAT.allow_delegation is True
    assert resolve_mode_profile(RunMode.CHAT).allow_delegation is True


def test_chat_mode_preloads_nothing():
    # Most chat turns are conversational and need zero tools; everything
    # stays one load_tool away.
    assert CHAT.preload_tools == []
    assert resolve_mode_profile(RunMode.CHAT).preload_tools == []


def test_chat_hides_scheduler_tools_but_scheduled_run_mode_keeps_them():
    assert CHAT.include_scheduler_tools is False
    # Scheduled tasks execute in autonomous mode.
    assert AUTONOMOUS.include_scheduler_tools is True


def test_chat_mode_is_conversational_and_work_modes_are_not():
    assert CHAT.conversational is True
    assert AUTONOMOUS.conversational is False


def test_chat_keeps_post_reflection_and_skip_preflight_removed():
    assert CHAT.skip_post_reflection is False
    assert "skip_preflight" not in type(CHAT).model_fields


def test_dream_mode_has_restricted_review_profile():
    profile = resolve_mode_profile(RunMode.DREAM)

    assert profile is DREAM
    assert profile.max_steps == 40
    assert profile.restricted_servers is True
    assert profile.default_servers == ["ouro"]
    assert profile.allow_delegation is False
    assert profile.include_scheduler_tools is False
    assert profile.lightweight is False
    assert profile.skip_post_reflection is True
    assert profile.append_conversation_turns is False
    assert profile.memory_tool_filter is None
    assert "write_dream_report" in profile.framing


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
