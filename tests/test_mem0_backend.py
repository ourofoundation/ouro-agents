from ouro_agents.memory.mem0 import Mem0Backend


class _FakeMem0:
    def __init__(self, team_id="team-42"):
        self.search_calls = []
        self.get_all_calls = []
        self.add_calls = []
        self.team_id = team_id

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [
            {
                "id": "memory-1",
                "memory": "Ce2Fe17 structural data from CIF",
                "score": 0.9,
                "metadata": {
                    "category": "fact",
                    "team_id": self.team_id,
                },
            }
        ]

    def get_all(self, **kwargs):
        self.get_all_calls.append(kwargs)
        return []

    def add(self, content, **kwargs):
        self.add_calls.append((content, kwargs))


def _backend_with_fake_mem(fake):
    backend = object.__new__(Mem0Backend)
    backend._mem = fake
    return backend


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

    assert fake.search_calls[0]["user_id"] == "user-1"
    assert fake.search_calls[0]["filters"] == {"category": "fact"}


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

    assert fake.search_calls[0]["user_id"] == "user-1"
    assert fake.search_calls[0]["filters"] == {"category": "fact"}
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

    assert fake.search_calls[0]["user_id"] == "user-1"
    assert fake.search_calls[0]["filters"] == {"category": "fact"}


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


def test_add_writes_schema_v2_metadata_and_legacy_fields():
    fake = _FakeMem0()
    backend = _backend_with_fake_mem(fake)

    backend.add(
        "Reviewed asset abc for team.",
        agent_id="hermes",
        user_id="user-1",
        run_id="run-1",
        team_id="team-42",
        metadata={"category": "observation", "asset_refs": "asset-abc"},
    )

    _, kwargs = fake.add_calls[0]
    metadata = kwargs["metadata"]
    assert metadata["schema_version"] == 2
    assert metadata["team_ids"] == "team-42"
    assert metadata["team_ids_idx"] == ",team-42,"
    assert metadata["asset_ids"] == "asset-abc"
    assert metadata["asset_ids_idx"] == ",asset-abc,"
    assert metadata["team_id"] == "team-42"
    assert metadata["asset_refs"] == "asset-abc"
    assert metadata["run_id"] == "run-1"
