import sqlite3

from ouro_agents.memory.mem0 import (
    Mem0Backend,
    _decode_linked_memory_ids,
    _encode_linked_memory_ids,
    _repair_chroma_blob_seq_ids,
    _restore_chroma_entity_payload,
    _sanitize_chroma_entity_payload,
)


class _FakeMem0:
    def __init__(self, team_id="team-42"):
        self.search_calls = []
        self.get_all_calls = []
        self.add_calls = []
        self.get_calls = []
        self.update_calls = []
        self.team_id = team_id

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [
            {
                "id": "memory-1",
                "memory": "Ce2Fe17 structural data from CIF",
                "score": 0.9,
                "created_at": "2026-04-15T04:11:41.404721-07:00",
                "run_id": "run-1",
                "metadata": {
                    "category": "fact",
                    "team_id": self.team_id,
                },
            }
        ]

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        return []

    def get(self, memory_id):
        self.get_calls.append(memory_id)
        return {
            "id": memory_id,
            "memory": "Existing memory text",
            "created_at": "2026-04-15T04:11:41.404721-07:00",
            "metadata": {
                "category": "fact",
                "strength": 0.7,
                "team_id": self.team_id,
            },
        }

    def update(self, memory_id, data, metadata=None):
        self.update_calls.append((memory_id, data, metadata))

    def add(self, content, **kwargs):
        self.add_calls.append((content, kwargs))


class _LimitOnlyMem0(_FakeMem0):
    def get_all(self, **kwargs):
        if "top_k" in kwargs:
            raise TypeError("Memory.get_all() got an unexpected keyword argument 'top_k'")
        if not kwargs.get("agent_id"):
            raise ValueError("At least one of 'user_id', 'agent_id', or 'run_id' must be provided.")
        self.get_all_calls.append(kwargs)
        return [
            {
                "id": "memory-1",
                "memory": "Ce2Fe17 structural data from CIF",
                "metadata": {
                    "category": "fact",
                    "team_id": self.team_id,
                },
            }
        ]


def _backend_with_fake_mem(fake):
    backend = object.__new__(Mem0Backend)
    backend._mem = fake
    return backend


def test_repair_chroma_blob_seq_ids_converts_legacy_embeddings_only(tmp_path):
    db_path = tmp_path / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE embeddings (seq_id)")
        conn.execute("CREATE TABLE max_seq_id (seq_id)")
        conn.execute(
            "INSERT INTO embeddings (seq_id) VALUES (?)",
            (sqlite3.Binary((123).to_bytes(8, "big")),),
        )
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (456,))
        conn.execute(
            "INSERT INTO max_seq_id (seq_id) VALUES (?)",
            (sqlite3.Binary((333).to_bytes(8, "big")),),
        )
        conn.execute(
            "INSERT INTO max_seq_id (seq_id) VALUES (?)",
            (sqlite3.Binary(b"\x11\x11000001"),),
        )
        conn.commit()

    repaired = _repair_chroma_blob_seq_ids(tmp_path)

    assert repaired == 2
    assert (tmp_path / ".seq_id_blob_fix_v2").exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT seq_id, typeof(seq_id) FROM embeddings ORDER BY seq_id"
        ).fetchall() == [(123, "integer"), (456, "integer")]
        assert conn.execute(
            "SELECT typeof(seq_id) FROM max_seq_id"
        ).fetchall() == [("integer",), ("blob",)]


def test_global_search_uses_explicit_user_namespace_without_team_filter():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    backend.search(
        "Ce2Fe17 CIF file",
        agent_id="hermes",
        user_id="user-1",
        team_id="team-42",
        scope="global",
        category="fact",
    )

    assert fake.search_calls[0]["top_k"] == 10
    assert fake.search_calls[0]["filters"] == {
        "agent_id": "hermes",
        "user_id": "user-1",
        "category": "fact",
    }


def test_team_search_post_filters_team_without_invalid_chroma_operator():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    results = backend.search(
        "Ce2Fe17 CIF file",
        agent_id="hermes",
        user_id="user-1",
        team_id="team-42",
        scope="team",
        category="fact",
    )

    assert fake.search_calls[0]["top_k"] > 10
    assert fake.search_calls[0]["filters"] == {
        "agent_id": "hermes",
        "user_id": "user-1",
        "category": "fact",
    }
    assert "team_ids_idx" not in fake.search_calls[0]["filters"]
    assert len(results) == 1


def test_personal_search_keeps_user_and_team_filters():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    backend.search(
        "Ce2Fe17 CIF file",
        agent_id="hermes",
        user_id="user-1",
        team_id="team-42",
        scope="personal",
        category="fact",
    )

    assert fake.search_calls[0]["filters"] == {
        "agent_id": "hermes",
        "user_id": "user-1",
        "category": "fact",
    }


def test_team_search_drops_nonmatching_post_filtered_team():
    fake = _FakeMem0(team_id="other-team")
    backend = _backend_with_fake_mem(fake)

    results = backend.search(
        "Ce2Fe17 CIF file",
        agent_id="hermes",
        team_id="team-42",
        scope="team",
        category="fact",
    )

    assert results == []


def test_search_preserves_top_level_mem0_fields():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    results = backend.search("Ce2Fe17 CIF file", agent_id="hermes")

    assert results[0].created_at == "2026-04-15T04:11:41.404721-07:00"
    assert results[0].metadata["run_id"] == "run-1"


def test_get_all_uses_mem0_filters_and_top_k():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    backend.get_all(
        agent_id="hermes",
        user_id="user-1",
        limit=10,
        team_id="team-42",
        category="fact",
    )

    assert fake.get_all_calls[0]["top_k"] > 10
    assert fake.get_all_calls[0]["filters"] == {
        "agent_id": "hermes",
        "user_id": "user-1",
        "category": "fact",
    }
    assert "team_ids_idx" not in fake.get_all_calls[0]["filters"]


def test_get_all_falls_back_to_limit_for_newer_mem0_api():
    fake = _LimitOnlyMem0()
    backend = _backend_with_fake_mem(fake)

    results = backend.get_all(
        agent_id="hermes",
        limit=10,
        team_id="team-42",
        category="fact",
    )

    assert fake.get_all_calls[0]["limit"] > 10
    assert "top_k" not in fake.get_all_calls[0]
    assert fake.get_all_calls[0]["agent_id"] == "hermes"
    assert "agent_id" not in fake.get_all_calls[0]["filters"]
    assert results[0].text == "Ce2Fe17 structural data from CIF"


def test_update_metadata_preserves_existing_memory_text_and_metadata():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    backend.update_metadata("memory-1", {"strength": 0.35})

    assert fake.get_calls == ["memory-1"]
    assert fake.update_calls[0][0] == "memory-1"
    assert fake.update_calls[0][1] == "Existing memory text"
    assert fake.update_calls[0][2]["category"] == "fact"
    assert fake.update_calls[0][2]["strength"] == 0.35
    assert fake.update_calls[0][2]["created_at"] == "2026-04-15T04:11:41.404721-07:00"


def test_add_writes_schema_v3_metadata():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    backend.add(
        "Reviewed asset abc for team.",
        agent_id="hermes",
        user_id="user-1",
        run_id="run-1",
        team_id="team-42",
        metadata={"category": "fact", "asset_ids": "asset-abc"},
    )

    _, kwargs = fake.add_calls[0]
    metadata = kwargs["metadata"]
    assert metadata["schema_version"] == 3
    assert metadata["team_ids"] == "team-42"
    assert metadata["team_ids_idx"] == ",team-42,"
    assert metadata["asset_ids"] == "asset-abc"
    assert metadata["asset_ids_idx"] == ",asset-abc,"
    assert metadata["strength"] == 0.5
    assert metadata["basis"] == "inferred"
    assert metadata["stability"] == "stable"
    assert metadata["run_id"] == "run-1"
    assert kwargs["infer"] is True
    assert fake.get_all_calls[0]["top_k"] == 1
    assert fake.get_all_calls[0]["filters"]["agent_id"] == "hermes"
    assert fake.get_all_calls[0]["filters"]["user_id"] == "user-1"
    assert "content_hash" in fake.get_all_calls[0]["filters"]


def test_add_can_skip_mem0_fact_inference_for_curated_memory():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    backend.add(
        "Reviewed asset abc for team.",
        agent_id="hermes",
        user_id="user-1",
        run_id="run-1",
        metadata={"category": "fact"},
        infer=False,
    )

    _, kwargs = fake.add_calls[0]
    assert kwargs["infer"] is False


def test_linked_memory_ids_roundtrip_for_chroma_metadata():
    ids = ["81edc49a-a952-43df-bada-e33c9feb4ddf", "other-id"]
    encoded = _encode_linked_memory_ids(ids)
    assert encoded == ",81edc49a-a952-43df-bada-e33c9feb4ddf,other-id,"
    assert _decode_linked_memory_ids(encoded) == ids


def test_sanitize_chroma_entity_payload_converts_lists():
    payload = {
        "data": "Iran War",
        "linked_memory_ids": ["81edc49a-a952-43df-bada-e33c9feb4ddf"],
    }
    sanitized = _sanitize_chroma_entity_payload(payload)
    assert isinstance(sanitized["linked_memory_ids"], str)
    restored = _restore_chroma_entity_payload(sanitized)
    assert restored["linked_memory_ids"] == payload["linked_memory_ids"]
