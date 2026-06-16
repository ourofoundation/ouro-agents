from ouro_agents.memory.model import EMPTY_CONTENT_HASH, content_hash, memory_item_from_raw, to_metadata
from ouro_agents.memory.validator import MemoryRunContext, validate_memory_candidate


def test_memory_metadata_round_trips_legacy_team_and_asset_refs():
    item = memory_item_from_raw(
        "Touched asset abc for team",
        {
            "category": "observation",
            "team_id": "team-42",
            "asset_refs": "asset-1,asset-2",
        },
    )

    metadata = to_metadata(item)

    assert item.team_ids == ["team-42"]
    assert item.asset_ids == ["asset-1", "asset-2"]
    assert item.category == "episode"
    assert metadata["team_ids_idx"] == ",team-42,"
    assert metadata["asset_ids_idx"] == ",asset-1,asset-2,"
    assert metadata["schema_version"] == 3


def test_memory_item_recomputes_empty_hash_for_nonempty_text():
    item = memory_item_from_raw(
        "Nonempty durable memory",
        {"content_hash": EMPTY_CONTENT_HASH},
    )

    assert item.content_hash == content_hash("Nonempty durable memory")


def test_validator_filters_unknown_team_ids_per_candidate():
    ctx = MemoryRunContext(
        agent_id="hermes",
        user_id="user-1",
        run_id="run-1",
        mode="heartbeat",
        team_id="team-1",
        available_team_ids={"team-1", "team-2"},
    )

    item = validate_memory_candidate(
        {
            "text": "Team 1 decided to focus on benchmarks.",
            "subject_type": "team",
            "category": "direction",
            "team_ids": ["unknown", "team-2"],
            "basis": "stated",
        },
        ctx,
        source="test",
    )

    assert item.team_ids == ["team-2"]
    assert item.subject_id == "team-2"
    assert item.category == "direction"


def test_validator_rejects_episode_memory_for_vector_store():
    ctx = MemoryRunContext(agent_id="hermes", user_id="user-1")

    try:
        validate_memory_candidate(
            {
                "text": "The agent reviewed asset abc.",
                "category": "episode",
            },
            ctx,
            source="test",
        )
    except ValueError as exc:
        assert "period logs" in str(exc)
    else:
        raise AssertionError("episode candidate should be rejected")


def test_validator_allows_chat_user_memory_without_team():
    ctx = MemoryRunContext(
        agent_id="hermes",
        user_id="user-1",
        run_id="conv-1",
        conversation_id="conv-1",
        mode="chat",
    )

    item = validate_memory_candidate(
        {
            "text": "User prefers concise implementation updates.",
            "subject_type": "user",
            "category": "preference",
        },
        ctx,
        source="test",
    )

    assert item.subject_id == "user-1"
    assert item.team_ids == []
