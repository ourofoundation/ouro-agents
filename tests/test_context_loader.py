from pathlib import Path

from ouro_agents.memory.context_loader import (
    _find_entity_files,
    build_cross_team_recent_activity,
    build_cross_team_task_index,
    build_memory_index,
    load_entity_files,
    read_context_paths,
    resolve_readable_context_path,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_find_entity_files_matches_frontmatter_alias(tmp_path: Path):
    _write(
        tmp_path / "protected" / "memory" / "entities" / "acme.md",
        "---\ndescription: Acme Corp background\naliases: [acme-corporation]\n---\n\n# Acme\n\nDetails.\n",
    )

    matched = _find_entity_files(tmp_path, "Looking at Acme Corporation today")

    assert len(matched) == 1
    assert matched[0][0] == "acme"
    assert matched[0][1].name == "acme.md"


def test_find_entity_files_still_matches_by_stem(tmp_path: Path):
    _write(tmp_path / "protected" / "memory" / "entities" / "modal-app.md", "# Modal app\n")

    matched = _find_entity_files(tmp_path, "Check the Modal App notes")

    assert [path.name for _, path in matched] == ["modal-app.md"]


def test_load_entity_files_uses_alias_match(tmp_path: Path):
    _write(
        tmp_path / "protected" / "memory" / "entities" / "acme.md",
        "---\naliases: [acme-corp]\n---\n\n# Acme\n\nKey account.\n",
    )

    text = load_entity_files(tmp_path, haystack="Acme Corp")

    assert "Key account." in text


def test_build_memory_index_lists_entities_and_tasks(tmp_path: Path):
    _write(
        tmp_path / "protected" / "memory" / "entities" / "acme.md",
        "---\ndescription: Acme Corp background\n---\n\n# Acme\n",
    )
    _write(
        tmp_path / "protected" / "memory" / "tasks" / "site-redesign.md",
        "# Site redesign\n\nStatus: in progress\n",
    )

    index = build_memory_index(tmp_path)

    assert "Memory files" in index
    assert "`protected/memory/entities/acme.md` — Acme Corp background" in index
    # No frontmatter description: falls back to the first heading line.
    assert "`protected/memory/tasks/site-redesign.md` — Site redesign" in index


def test_build_memory_index_empty_workspace(tmp_path: Path):
    assert build_memory_index(tmp_path) == ""


def test_build_memory_index_team_scoped(tmp_path: Path):
    _write(
        tmp_path / "teams" / "team-42" / "memory" / "entities" / "alloy.md",
        "---\ndescription: Alloy dataset notes\n---\n\n# Alloy\n",
    )

    index = build_memory_index(tmp_path, team_id="team-42")

    assert "alloy.md` — Alloy dataset notes" in index
    assert build_memory_index(tmp_path) == ""


def test_build_cross_team_task_index_lists_active_tasks(tmp_path: Path):
    team = "01954d5f-fcea-7970-b8d8-b68879df9d7f"
    _write(
        tmp_path / "teams" / team / "memory" / "tasks" / "outreach.md",
        "---\ndescription: Sponsor pipeline\n---\n\n# Outreach\n\nStatus: in progress\n\n## Next steps\n- send email\n",
    )
    _write(
        tmp_path / "teams" / team / "memory" / "tasks" / "done.md",
        "# Done\n\nStatus: complete\n",
    )

    index = build_cross_team_task_index(
        tmp_path, team_labels={team: "permanent-magnets"}
    )

    assert "Cross-team active task files" in index
    assert "outreach.md" in index
    assert "permanent-magnets" in index
    assert "done.md" not in index


def test_build_cross_team_recent_activity_tails_logs(tmp_path: Path):
    team_a = "01954d5f-fcea-7970-b8d8-b68879df9d7f"
    team_b = "019538e2-dd84-7c0b-b340-b2c519fbe730"
    nil = "00000000-0000-0000-0000-000000000000"
    period = "2026-W31"

    def _team(tid: str, slug: str, body: str) -> None:
        import json

        d = tmp_path / "teams" / slug
        d.mkdir(parents=True)
        (d / "state.json").write_text(
            json.dumps({"team": {"id": tid, "name": slug, "slug": slug}, "docs": {}})
        )
        (d / "logs").mkdir()
        (d / "logs" / f"{period}.md").write_text(body)

    _team(
        team_a,
        "materials-science",
        "# W31\n\n## early\n" + ("old line\n" * 80) + "## 2026-07-31\nSent Sandip De cold email.\n",
    )
    _team(team_b, "machine-learning", "# ML W31\n\nFollowed up Arroyave and Kleinke.\n")
    _team(nil, "all", "# All team should be skipped\n")

    digest = build_cross_team_recent_activity(
        tmp_path,
        period=period,
        team_labels={team_a: "materials-science", team_b: "machine-learning"},
        max_tokens=400,
        max_tokens_per_team=120,
    )

    assert "Recent activity across teams" in digest
    assert "materials-science" in digest
    assert "machine-learning" in digest
    assert "Sandip De" in digest
    assert "Arroyave" in digest
    assert "All team should be skipped" not in digest
    assert "teams/materials-science/logs/2026-W31.md" in digest


def test_build_cross_team_recent_activity_empty_workspace(tmp_path: Path):
    assert build_cross_team_recent_activity(tmp_path, period="2026-W31") == ""


def test_resolve_readable_context_path_rejects_traversal(tmp_path: Path):
    _write(tmp_path / "protected" / "memory" / "tasks" / "ok.md", "# ok\n")
    assert (
        resolve_readable_context_path(tmp_path, "protected/memory/tasks/ok.md")
        is not None
    )
    assert resolve_readable_context_path(tmp_path, "memory/tasks/ok.md") is not None
    assert resolve_readable_context_path(tmp_path, "../etc/passwd") is None
    assert resolve_readable_context_path(tmp_path, "secrets/key.txt") is None


def test_resolve_readable_context_path_allows_team_logs(tmp_path: Path):
    team = "team-a"
    rel = f"teams/{team}/logs/2026-W31.md"
    _write(tmp_path / rel, "# Log\n\nSent email today.\n")
    assert resolve_readable_context_path(tmp_path, rel) is not None


def test_read_context_paths_reads_allowed_files(tmp_path: Path):
    team = "team-a"
    rel = f"teams/{team}/memory/tasks/hook.md"
    _write(tmp_path / rel, "# Hook\n\nNext: follow up Khosla.\n")

    text = read_context_paths(
        tmp_path, [rel, "protected/memory/../../etc/passwd"]
    )

    assert "Next: follow up Khosla." in text
    assert "outside allowed memory roots" in text


def test_read_context_paths_reads_team_logs(tmp_path: Path):
    team = "team-a"
    rel = f"teams/{team}/logs/2026-W31.md"
    _write(tmp_path / rel, "# W31\n\nCold email to Sandip De.\n")

    text = read_context_paths(tmp_path, [rel])

    assert "Cold email to Sandip De." in text
