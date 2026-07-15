"""Tests for outcome evidence digests."""

from types import SimpleNamespace

from ouro_agents.modes.outcomes import (
    build_outcome_evidence_context,
    collect_quest_outcome,
    format_outcome_line,
)


def test_format_outcome_line_includes_external_metrics():
    line = format_outcome_line(
        {
            "name": "Cycle 22",
            "status": "closed",
            "created_at": "2026-07-11",
            "items_done": 4,
            "items_total": 4,
            "produced_asset_ids": ["a1"],
            "metrics": {
                "external_comments": 0,
                "external_reactions": 0,
                "quality_views": 14,
                "downloads": 0,
                "external_entries": 0,
            },
        }
    )
    assert "Cycle 22" in line
    assert "4/4 done" in line
    assert "0 external comments" in line
    assert "14 quality views" in line


def test_collect_quest_outcome_uses_notes_asset_ids_and_counts_fallback():
    class FakeAssets:
        def counts(self, asset_id):
            return {"views": 10, "comments": 2, "reactions": 0, "downloads": 1}

    class FakeComments:
        def list(self, parent_id=None, *args, **kwargs):
            return [
                {"user_id": "owner-1", "id": "c1"},
                {"user_id": "other-2", "id": "c2"},
            ]

    ouro = SimpleNamespace(
        assets=FakeAssets(),
        comments=FakeComments(),
        quests=SimpleNamespace(),
    )
    quest = SimpleNamespace(
        id="quest-1",
        name="PV cycle",
        created_at="2026-07-12T00:00:00+00:00",
        quest=SimpleNamespace(status="open"),
        items=[
            SimpleNamespace(
                id="i1",
                description="Publish analysis post",
                status="done",
                notes="Published post asset:019f4c4e-73f2-7dcb-a0a9-daf9840b712e",
                submission_assets=None,
            )
        ],
    )

    outcome = collect_quest_outcome(ouro, quest, owner_user_id="owner-1")
    assert outcome["items_done"] == 1
    assert "019f4c4e-73f2-7dcb-a0a9-daf9840b712e" in outcome["produced_asset_ids"]
    assert outcome["metrics"]["views"] == 10
    assert outcome["metrics"]["external_comments"] == 1


def test_build_outcome_evidence_context_degrades_gracefully_on_client_failure():
    agent = SimpleNamespace(
        own_user_id="u1",
        config=SimpleNamespace(agent=SimpleNamespace(org_id=None)),
        _get_ouro_client=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert build_outcome_evidence_context(agent) == ""


def test_build_outcome_evidence_context_renders_digest():
    class FakeQuests:
        def retrieve(self, quest_id):
            return SimpleNamespace(
                id=quest_id,
                name="Cycle 25",
                created_at="2026-07-13T00:00:00+00:00",
                quest=SimpleNamespace(status="closed"),
                items=[
                    SimpleNamespace(
                        id="i1",
                        description="Done work",
                        status="done",
                        notes="",
                        submission_assets=None,
                    )
                ],
            )

    class FakeAssets:
        def search(self, **kwargs):
            return [
                {
                    "id": "q1",
                    "name": "Cycle 25",
                    "created_at": "2026-07-13T00:00:00+00:00",
                }
            ]

        def counts(self, asset_id):
            return {}

    agent = SimpleNamespace(
        own_user_id="u1",
        config=SimpleNamespace(agent=SimpleNamespace(org_id=None)),
        _get_ouro_client=lambda: SimpleNamespace(
            quests=FakeQuests(), assets=FakeAssets(), comments=SimpleNamespace()
        ),
    )

    context = build_outcome_evidence_context(agent)
    assert "## Outcome Evidence" in context
    assert "Cycle 25" in context
    assert "completion without engagement is not success" in context
