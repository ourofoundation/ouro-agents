"""Tests for the deterministic asset.deleted cleanup module."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ouro_agents.cleanup.asset_deleted import (
    discover_files_with_asset,
    rewrite_markdown,
    rewrite_plan_json,
    sweep_workspace_for_deleted_asset,
)


_UUID = "01999999-1111-7222-8333-444444444444"
_OTHER = "01988888-aaaa-7bbb-8ccc-dddddddddddd"


# ---------------------------------------------------------------------------
# rewrite_markdown
# ---------------------------------------------------------------------------


def test_rewrite_markdown_replaces_typed_link_with_label_and_marker():
    body = f"see [my dataset](dataset:{_UUID}) for details\n"
    new, edits = rewrite_markdown(body, _UUID)
    assert edits == 1
    assert new == "see my dataset [deleted] for details\n"


def test_rewrite_markdown_handles_empty_link_label():
    body = f"orphan: [](post:{_UUID})\n"
    new, edits = rewrite_markdown(body, _UUID)
    assert edits == 1
    assert new == "orphan: [deleted]\n"


def test_rewrite_markdown_replaces_bare_uuid_in_list_item():
    body = (
        "- working with asset 01999999-1111-7222-8333-444444444444 today\n"
        "- unrelated note about pasta\n"
    )
    new, edits = rewrite_markdown(body, _UUID)
    assert edits == 1
    assert "[deleted]" in new
    assert "pasta" in new
    assert _UUID not in new


def test_rewrite_markdown_replaces_uuid_inside_table_cell():
    body = (
        "| name | id |\n"
        "|------|----|\n"
        f"| foo  | {_UUID} |\n"
        f"| bar  | {_OTHER} |\n"
    )
    new, edits = rewrite_markdown(body, _UUID)
    assert edits == 1
    assert "[deleted]" in new
    assert _OTHER in new  # untouched
    assert _UUID not in new


def test_rewrite_markdown_replaces_asset_component_block():
    body = (
        "preamble\n"
        "```assetComponent\n"
        '{"id": "' + _UUID + '", "assetType": "post", "viewMode": "preview"}\n'
        "```\n"
        "trailing prose\n"
    )
    new, edits = rewrite_markdown(body, _UUID)
    assert edits == 1
    assert "> [deleted asset]" in new
    assert "```assetComponent" not in new
    assert "trailing prose" in new


def test_rewrite_markdown_leaves_other_uuids_alone():
    body = f"- still alive: [foo](post:{_OTHER})\n- gone: [bar](post:{_UUID})\n"
    new, edits = rewrite_markdown(body, _UUID)
    assert edits == 1
    assert _OTHER in new
    assert "bar [deleted]" in new


def test_rewrite_markdown_zero_edits_when_uuid_absent():
    body = "no relevant ids here\n"
    new, edits = rewrite_markdown(body, _UUID)
    assert edits == 0
    assert new == body


# ---------------------------------------------------------------------------
# rewrite_plan_json
# ---------------------------------------------------------------------------


def test_rewrite_plan_json_filters_array_entries():
    plan = json.dumps(
        {"asset_ids": [_UUID, _OTHER], "title": "demo"}, indent=2
    )
    new, edits, archived = rewrite_plan_json(plan, _UUID)
    assert edits == 1
    assert not archived
    parsed = json.loads(new)
    assert parsed["asset_ids"] == [_OTHER]
    assert parsed["title"] == "demo"


def test_rewrite_plan_json_marks_plan_archived_when_array_empties():
    plan = json.dumps({"asset_ids": [_UUID], "title": "lonely"}, indent=2)
    new, edits, archived = rewrite_plan_json(plan, _UUID)
    assert edits == 1
    assert archived
    parsed = json.loads(new)
    assert parsed["asset_ids"] == []
    assert parsed["archived"] is True


def test_rewrite_plan_json_replaces_string_value_with_marker():
    plan = json.dumps({"primary_id": _UUID, "title": "demo"}, indent=2)
    new, _, _ = rewrite_plan_json(plan, _UUID)
    parsed = json.loads(new)
    assert parsed["primary_id"] == "[deleted]"


def test_rewrite_plan_json_falls_back_to_string_replace_on_invalid_json():
    raw = "not really json but contains " + _UUID + " here"
    new, edits, archived = rewrite_plan_json(raw, _UUID)
    assert edits == 1
    assert "[deleted]" in new
    assert not archived


# ---------------------------------------------------------------------------
# Workspace sweep + discovery
# ---------------------------------------------------------------------------


def _build_workspace(root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}

    memory = root / "MEMORY.md"
    memory.write_text(
        f"# Memory\n\n- ref: [old plan](post:{_UUID})\n- keeper: {_OTHER}\n"
    )
    paths["memory"] = memory

    daily_dir = root / "daily_logs"
    daily_dir.mkdir()
    daily = daily_dir / "2026-05-01.md"
    daily.write_text(f"- 09:00 — published [report](dataset:{_UUID})\n")
    paths["daily"] = daily

    plans_dir = root / "teams" / "team-1" / "plans"
    plans_dir.mkdir(parents=True)
    plan = plans_dir / "p1.json"
    plan.write_text(json.dumps({"asset_ids": [_UUID, _OTHER]}, indent=2))
    paths["plan"] = plan

    untouched = root / "data" / "ignored.md"
    untouched.parent.mkdir()
    untouched.write_text(f"contains {_UUID} but excluded\n")
    paths["excluded"] = untouched

    return paths


def test_sweep_rewrites_markdown_and_plan_only_for_target_uuid():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = _build_workspace(root)

        result = sweep_workspace_for_deleted_asset(root, _UUID)

        assert result.asset_id == _UUID
        assert str(paths["memory"]) in result.files_rewritten
        assert str(paths["daily"]) in result.files_rewritten
        assert str(paths["plan"]) in result.files_rewritten
        # The excluded data/ file was not even discovered.
        assert str(paths["excluded"]) not in result.files_inspected

        # Memory file: typed link replaced; other UUID preserved.
        new_memory = paths["memory"].read_text()
        assert _UUID not in new_memory
        assert _OTHER in new_memory
        assert "old plan [deleted]" in new_memory

        # Daily log: typed link replaced.
        new_daily = paths["daily"].read_text()
        assert _UUID not in new_daily
        assert "report [deleted]" in new_daily

        # Plan: array filtered, no archive flag (still non-empty).
        new_plan = json.loads(paths["plan"].read_text())
        assert new_plan["asset_ids"] == [_OTHER]
        assert "archived" not in new_plan


def test_sweep_bumps_frontmatter_timestamp_on_markdown_rewrites():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        memory = root / "MEMORY.md"
        memory.write_text(f"---\ntitle: Memory\n---\n\nref: [x](post:{_UUID})\n")

        sweep_workspace_for_deleted_asset(root, _UUID)

        text = memory.read_text()
        assert "last_updated:" in text
        assert "title: Memory" in text
        assert _UUID not in text


def test_discover_files_with_asset_filters_by_extension():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "good.md").write_text(f"contains {_UUID}")
        (root / "good.json").write_text(json.dumps({"id": _UUID}))
        (root / "bin.txt").write_text(f"also has {_UUID}")
        (root / "data").mkdir()
        (root / "data" / "ignored.md").write_text(f"in excluded dir {_UUID}")

        matches = {p.name for p in discover_files_with_asset(_UUID, root)}

        assert "good.md" in matches
        assert "good.json" in matches
        # data/ is excluded; bin.txt has wrong suffix and isn't rewritten anyway.
        assert "ignored.md" not in matches
