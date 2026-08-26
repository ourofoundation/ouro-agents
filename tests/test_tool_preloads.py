"""Context-aware MCP tool preloads."""

from ouro_agents.security.policy import Capability
from ouro_agents.tool_preloads import (
    GET_ASSET,
    QUEST_COMMENT,
    attached_asset_ids,
    attached_asset_task_hint,
    filter_preloads,
    merge_preloads,
    preloads_for_event,
)


def test_plain_chat_message_preloads_nothing():
    assert preloads_for_event("new-message", data={"text": "hello"}) == ()


def test_chat_message_with_asset_fence_preloads_get_asset():
    text = (
        "```assetComponent\n"
        '{"assetType": "post", "id": "asset-123", "viewMode": "card"}\n'
        "```\n"
        "what is this about?"
    )
    assert preloads_for_event("new-message", data={"text": text}) == (GET_ASSET,)


def test_chat_message_with_explicit_attached_assets():
    data = {"text": "look at this", "attached_assets": ["asset-9"]}
    assert preloads_for_event("new-message", data=data) == (GET_ASSET,)
    assert attached_asset_ids(data) == ("asset-9",)


def test_chat_message_with_tiptap_json_embed():
    data = {
        "text": "look",
        "json": {
            "type": "doc",
            "content": [
                {
                    "type": "assetComponent",
                    "attrs": {"id": "file-1", "assetType": "file"},
                }
            ],
        },
    }
    assert attached_asset_ids(data) == ("file-1",)
    assert preloads_for_event("new-message", data=data) == (GET_ASSET,)


def test_asset_component_without_id_does_not_preload():
    text = '```assetComponent\n{"assetType": "route"}\n```\n'
    assert attached_asset_ids({"text": text}) == ()
    assert preloads_for_event("new-message", data={"text": text}) == ()


def test_comment_keeps_registry_preloads():
    names = preloads_for_event(
        "comment",
        root_asset_type="post",
        data={"text": "looks good"},
    )
    assert names == ("ouro:get_asset", "ouro:write_comment", "ouro:get_comments")


def test_quest_comment_uses_manage_set():
    names = preloads_for_event(
        "comment",
        root_asset_type="quest",
        data={"text": "good to send"},
    )
    assert names == QUEST_COMMENT
    assert "ouro:update_quest_item" in names
    assert "ouro:complete_quest_item" in names
    assert "ouro:list_quest_leaderboard" in names


def test_attached_asset_on_comment_does_not_duplicate_get_asset():
    text = (
        "```assetComponent\n"
        '{"id": "post-1", "assetType": "post"}\n'
        "```\n"
    )
    names = preloads_for_event(
        "comment",
        root_asset_type="post",
        data={"text": text},
    )
    assert names.count(GET_ASSET) == 1
    assert names[0] == GET_ASSET


def test_attached_asset_ids_dedupes_across_sources():
    data = {
        "attached_assets": ["asset-1", {"id": "asset-2"}],
        "text": '```assetComponent\n{"id": "asset-1"}\n```\n',
        "json": {
            "type": "doc",
            "content": [{"type": "assetComponent", "attrs": {"id": "asset-2"}}],
        },
    }
    assert attached_asset_ids(data) == ("asset-1", "asset-2")


def test_attached_asset_task_hint():
    hint = attached_asset_task_hint(("asset-1", "asset-2"))
    assert "`asset-1`" in hint
    assert "`asset-2`" in hint
    assert "get_asset" in hint
    assert attached_asset_task_hint(()) == ""


def test_merge_preloads_is_first_seen():
    assert merge_preloads(
        ["ouro:get_asset"],
        ["ouro:get_asset", "ouro:search_assets"],
        None,
    ) == ["ouro:get_asset", "ouro:search_assets"]


def test_filter_preloads_drops_disallowed_and_unmapped():
    allowed = frozenset({Capability.READ_PLATFORM})
    assert filter_preloads(
        ["ouro:get_asset", "ouro:execute_route", "custom:unknown"],
        allowed,
    ) == ["ouro:get_asset"]
    assert filter_preloads(["ouro:execute_route"], None) == ["ouro:execute_route"]
