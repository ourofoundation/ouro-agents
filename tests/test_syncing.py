from datetime import datetime, timezone
from types import SimpleNamespace

from ouro_agents.syncing import (
    choose_timestamp_sync_action,
    normalize_status,
    read_field,
)


def test_read_field_handles_dicts_objects_and_dotted_paths():
    payload = {"quest": SimpleNamespace(status="closed")}

    assert read_field(payload, "quest.status") == "closed"
    assert read_field(SimpleNamespace(items=[1, 2]), "items") == [1, 2]
    assert read_field({}, "missing.value", "fallback") == "fallback"


def test_normalize_status_applies_aliases_and_preserves_domain_statuses():
    assert (
        normalize_status(
            "in-progress",
            aliases={"in_progress": "in_progress"},
            default="pending",
        )
        == "in_progress"
    )
    assert normalize_status("closed") == "closed"


def test_choose_timestamp_sync_action_prefers_newer_timestamp():
    local_ts = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    remote_ts = datetime(2026, 4, 14, 11, 0, tzinfo=timezone.utc)

    decision = choose_timestamp_sync_action(
        local_body="local",
        remote_body="remote",
        local_ts=local_ts,
        remote_ts=remote_ts,
    )

    assert decision.action == "push"
    assert decision.reason == "local-newer"


def test_choose_timestamp_sync_action_handles_empty_sides():
    assert (
        choose_timestamp_sync_action(
            local_body="local",
            remote_body="",
            local_ts=None,
            remote_ts=None,
        ).action
        == "push"
    )
    assert (
        choose_timestamp_sync_action(
            local_body="",
            remote_body="remote",
            local_ts=None,
            remote_ts=None,
        ).action
        == "pull"
    )
    assert (
        choose_timestamp_sync_action(
            local_body="",
            remote_body="",
            local_ts=None,
            remote_ts=None,
        ).action
        == "unchanged"
    )
