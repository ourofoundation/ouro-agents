"""Tests for slug-based team workspace paths and migrations."""

from __future__ import annotations

import json
from pathlib import Path

from ouro_agents.memory.context_loader import (
    build_cross_team_recent_activity,
    read_context_paths,
    resolve_readable_context_path,
)
from ouro_agents.memory.team_paths import (
    ensure_team_dir,
    find_team_dir_by_slug_or_id,
    migrate_workspace_team_dirs,
    preferred_team_dir_name,
    public_team_relpath,
    read_team_identity,
    team_workspace_dir,
)


NIL = "00000000-0000-0000-0000-000000000000"
TEAM_A = "019538e2-dd84-7c0b-b340-b2c519fbe730"
TEAM_B = "01954d5f-fcea-7970-b8d8-b68879df9d7f"


def _write_team(workspace: Path, team_id: str, slug: str, *, dir_name: str | None = None) -> Path:
    leaf = dir_name or team_id
    team_dir = workspace / "teams" / leaf
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "state.json").write_text(
        json.dumps(
            {
                "team": {
                    "id": team_id,
                    "name": slug,
                    "slug": slug,
                    "org_id": NIL,
                },
                "docs": {},
            },
            indent=2,
        )
    )
    return team_dir


def test_preferred_team_dir_name_catch_all_and_slug():
    assert preferred_team_dir_name(NIL) == "all"
    assert preferred_team_dir_name(TEAM_A, team_slug="machine-learning") == "machine-learning"
    assert preferred_team_dir_name(TEAM_A) == TEAM_A


def test_migrate_uuid_dirs_to_slug(tmp_path: Path):
    _write_team(tmp_path, TEAM_A, "machine-learning")
    _write_team(tmp_path, NIL, "all")
    logs = tmp_path / "teams" / TEAM_A / "logs"
    logs.mkdir()
    (logs / "2026-W31.md").write_text("# ML log\n")

    moves = migrate_workspace_team_dirs(tmp_path)

    assert any("machine-learning" in m for m in moves)
    assert (tmp_path / "teams" / "machine-learning" / "logs" / "2026-W31.md").is_file()
    assert not (tmp_path / "teams" / TEAM_A).exists()
    assert (tmp_path / "teams" / "all").is_dir()
    assert migrate_workspace_team_dirs(tmp_path) == []


def test_ensure_team_dir_renames_on_slug_change(tmp_path: Path):
    _write_team(tmp_path, TEAM_A, "old-slug", dir_name="old-slug")
    (tmp_path / "teams" / "old-slug" / "MEMORY.md").write_text("# mem\n")

    path = ensure_team_dir(tmp_path, TEAM_A, team_slug="new-slug")

    assert path.name == "new-slug"
    assert (path / "MEMORY.md").read_text() == "# mem\n"
    assert not (tmp_path / "teams" / "old-slug").exists()
    tid, slug = read_team_identity(path)
    assert tid == TEAM_A
    assert slug == "old-slug"  # state.json not rewritten by rename; platform refresh updates it


def test_find_by_slug_or_legacy_uuid(tmp_path: Path):
    _write_team(tmp_path, TEAM_A, "machine-learning", dir_name="machine-learning")

    assert find_team_dir_by_slug_or_id(tmp_path, "machine-learning").name == "machine-learning"
    assert find_team_dir_by_slug_or_id(tmp_path, TEAM_A).name == "machine-learning"
    assert team_workspace_dir(tmp_path, TEAM_A, team_slug="machine-learning").name == (
        "machine-learning"
    )


def test_read_context_accepts_slug_and_uuid_paths(tmp_path: Path):
    team_dir = _write_team(
        tmp_path, TEAM_A, "machine-learning", dir_name="machine-learning"
    )
    log = team_dir / "logs" / "2026-W31.md"
    log.parent.mkdir(parents=True)
    log.write_text("# Week\n\nSent email.\n")

    by_slug = read_context_paths(
        tmp_path, ["teams/machine-learning/logs/2026-W31.md"]
    )
    by_uuid = read_context_paths(
        tmp_path, [f"teams/{TEAM_A}/logs/2026-W31.md"]
    )
    missing = read_context_paths(
        tmp_path, ["teams/machine-learning/logs/2026-W99.md"]
    )
    forbidden = read_context_paths(tmp_path, ["secrets/key.txt"])

    assert "Sent email." in by_slug
    assert "Sent email." in by_uuid
    assert "(not found)" in missing
    assert "(outside allowed memory roots)" in forbidden
    assert resolve_readable_context_path(
        tmp_path, "teams/machine-learning/logs/2026-W31.md"
    ).is_file()


def test_recent_activity_digest_uses_slug_paths(tmp_path: Path):
    team_dir = _write_team(
        tmp_path, TEAM_B, "permanent-magnets", dir_name="permanent-magnets"
    )
    log = team_dir / "logs" / "2026-W31.md"
    log.parent.mkdir(parents=True)
    log.write_text("# W31\n\nFollow-up to Arroyave.\n")

    digest = build_cross_team_recent_activity(
        tmp_path,
        period="2026-W31",
        team_labels={TEAM_B: "permanent-magnets"},
    )

    assert "`teams/permanent-magnets/logs/2026-W31.md`" in digest
    assert "Arroyave" in digest
    assert TEAM_B not in digest
    assert public_team_relpath(tmp_path, log) == (
        "teams/permanent-magnets/logs/2026-W31.md"
    )


def test_slug_collision_falls_back_to_qualified_name(tmp_path: Path):
    _write_team(tmp_path, TEAM_A, "shared-slug", dir_name="shared-slug")
    other = _write_team(tmp_path, TEAM_B, "shared-slug", dir_name=TEAM_B)

    path = ensure_team_dir(tmp_path, TEAM_B, team_slug="shared-slug")

    assert path.name == f"shared-slug-{TEAM_B[:8]}"
    assert (tmp_path / "teams" / "shared-slug").is_dir()
    assert other.exists() or path.exists()
