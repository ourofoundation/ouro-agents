from pathlib import Path
from types import SimpleNamespace

from ouro_agents.memory.context_loader import (
    _find_entity_files,
    build_memory_index,
    load_entity_files,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_find_entity_files_matches_frontmatter_alias(tmp_path: Path):
    _write(
        tmp_path / "memory" / "entities" / "acme.md",
        "---\ndescription: Acme Corp background\naliases: [acme-corporation]\n---\n\n# Acme\n\nDetails.\n",
    )

    matched = _find_entity_files(tmp_path, ["Acme Corporation"])

    assert len(matched) == 1
    assert matched[0][0] == "Acme Corporation"
    assert matched[0][1].name == "acme.md"


def test_find_entity_files_still_matches_by_stem(tmp_path: Path):
    _write(tmp_path / "memory" / "entities" / "modal-app.md", "# Modal app\n")

    matched = _find_entity_files(tmp_path, ["Modal App"])

    assert [path.name for _, path in matched] == ["modal-app.md"]


def test_load_entity_files_uses_alias_match(tmp_path: Path):
    _write(
        tmp_path / "memory" / "entities" / "acme.md",
        "---\naliases: [acme-corp]\n---\n\n# Acme\n\nKey account.\n",
    )
    state = SimpleNamespace(key_entities=["Acme Corp"])

    text = load_entity_files(tmp_path, state)

    assert "Key account." in text


def test_build_memory_index_lists_entities_and_tasks(tmp_path: Path):
    _write(
        tmp_path / "memory" / "entities" / "acme.md",
        "---\ndescription: Acme Corp background\n---\n\n# Acme\n",
    )
    _write(
        tmp_path / "memory" / "tasks" / "site-redesign.md",
        "# Site redesign\n\nStatus: in progress\n",
    )

    index = build_memory_index(tmp_path)

    assert "Memory files" in index
    assert "`memory/entities/acme.md` — Acme Corp background" in index
    # No frontmatter description: falls back to the first heading line.
    assert "`memory/tasks/site-redesign.md` — Site redesign" in index


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
