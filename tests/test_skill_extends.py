"""Tests for skill ``extends:`` addenda."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ouro_agents.config import OuroAgentsConfig
from ouro_agents.skills import (
    _ADDENDUM_CHAR_CAP,
    _ADDENDUM_TRUNCATION_MARKER,
    get_skill_directory,
    invalidate_skill_cache,
    list_skill_names,
    load_startup_skills,
    resolve_skill,
)


def _write_skill(skills_dir: Path, name: str, text: str) -> Path:
    skills_dir.mkdir(parents=True, exist_ok=True)
    path = skills_dir / f"{name}.md"
    path.write_text(text)
    return path


def _config(workspace: Path) -> OuroAgentsConfig:
    return OuroAgentsConfig(
        agent={"name": "test", "model": "test-model", "workspace": workspace},
        heartbeat={"model": "test-model"},
        mcp_servers=[],
        memory={
            "extraction_model": "test-model",
            "embedder": "test-embedder",
        },
    )


def test_addendum_to_always_parent_inlined_at_startup(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "outreach",
        "---\ndescription: Human outreach rules\nload: always\n---\n\n"
        "# Outreach\n\nDo not spam.\n",
    )
    _write_skill(
        skills,
        "outreach-addendum",
        "---\ndescription: Learned outreach tweaks\nextends: outreach\n---\n\n"
        "# Tweaks\n\nPrefer coil triage.\n",
    )
    invalidate_skill_cache(tmp_path)

    text = load_startup_skills(_config(tmp_path))
    assert "# Outreach" in text
    assert "Do not spam." in text
    assert "## Agent addendum: outreach-addendum" in text
    assert 'Extends the "outreach" skill' in text
    assert "the skill above wins" in text
    assert "Prefer coil triage." in text
    # Parent body must come before the addendum heading.
    assert text.index("# Outreach") < text.index("## Agent addendum: outreach-addendum")


def test_addendum_to_stub_parent_not_inlined_but_resolves(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "domain",
        "---\ndescription: Stub domain playbook\nload: stub\n---\n\n"
        "# Domain\n\nBase steps.\n",
    )
    _write_skill(
        skills,
        "domain-addendum",
        "---\ndescription: Domain refinements\nextends: domain\n---\n\n"
        "Use run_coil instead.\n",
    )
    invalidate_skill_cache(tmp_path)

    startup = load_startup_skills(_config(tmp_path))
    assert "Base steps." not in startup
    assert "Use run_coil instead." not in startup

    resolved = resolve_skill("domain", workspace=tmp_path)
    assert resolved is not None
    assert "Base steps." in resolved
    assert "## Agent addendum: domain-addendum" in resolved
    assert "Use run_coil instead." in resolved


def test_child_excluded_from_directory_and_list_names(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "outreach",
        "---\ndescription: Outreach\nload: always\n---\n\n# Outreach\n",
    )
    _write_skill(
        skills,
        "outreach-addendum",
        "---\ndescription: Addendum\nextends: outreach\n---\n\nExtra.\n",
    )
    _write_skill(
        skills,
        "domain",
        "---\ndescription: Domain stub\nload: stub\n---\n\n# Domain\n",
    )
    _write_skill(
        skills,
        "domain-addendum",
        "---\ndescription: Domain addendum\nextends: domain\n---\n\nExtra.\n",
    )
    invalidate_skill_cache(tmp_path)

    directory = get_skill_directory(_config(tmp_path))
    assert "outreach-addendum" not in directory
    assert "domain-addendum" not in directory
    assert "- domain:" in directory
    # always-loaded parent excluded from directory by default
    assert "- outreach:" not in directory

    names = list_skill_names(tmp_path)
    assert "outreach-addendum" not in names
    assert "domain-addendum" not in names
    assert "outreach" in names
    assert "domain" in names


def test_unknown_extends_treated_as_standalone(tmp_path, caplog):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "orphan-addendum",
        "---\ndescription: Orphan\nextends: missing-parent\nload: stub\n---\n\n"
        "# Orphan\n",
    )
    invalidate_skill_cache(tmp_path)

    with caplog.at_level(logging.WARNING, logger="ouro_agents.skills"):
        directory = get_skill_directory(_config(tmp_path))
        names = list_skill_names(tmp_path, include_always=False)

    assert "- orphan-addendum:" in directory
    assert "orphan-addendum" in names
    assert any("unknown parent" in r.message for r in caplog.records)


def test_no_chaining_treats_grandchild_as_standalone(tmp_path, caplog):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "base",
        "---\ndescription: Base\nload: stub\n---\n\n# Base\n",
    )
    _write_skill(
        skills,
        "base-addendum",
        "---\ndescription: Mid\nextends: base\n---\n\n# Mid\n",
    )
    _write_skill(
        skills,
        "base-addendum-addendum",
        "---\ndescription: Grandchild\nextends: base-addendum\n---\n\n"
        "# Grandchild\n",
    )
    invalidate_skill_cache(tmp_path)

    with caplog.at_level(logging.WARNING, logger="ouro_agents.skills"):
        resolved = resolve_skill("base", workspace=tmp_path)
        names = list_skill_names(tmp_path)

    assert resolved is not None
    assert "## Agent addendum: base-addendum" in resolved
    assert "Grandchild" not in resolved
    # Grandchild is standalone because parent itself has extends
    assert "base-addendum-addendum" in names
    assert any("no chaining" in r.message for r in caplog.records)


def test_oversized_addendum_truncated(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "big",
        "---\ndescription: Big parent\nload: always\n---\n\n# Big\n",
    )
    huge = "x" * (_ADDENDUM_CHAR_CAP + 500)
    _write_skill(
        skills,
        "big-addendum",
        "---\ndescription: Huge\nextends: big\n---\n\n" + huge + "\n",
    )
    invalidate_skill_cache(tmp_path)

    text = load_startup_skills(_config(tmp_path))
    assert _ADDENDUM_TRUNCATION_MARKER in text
    # Truncated body should not contain the full oversized payload.
    assert huge not in text
    # Cap applies to addendum body only — the rendered section after the
    # addendum header should be at most cap + marker overhead.
    header = '## Agent addendum: big-addendum\n'
    addendum_section = text.split(header, 1)[1]
    body_before_marker = addendum_section.split(_ADDENDUM_TRUNCATION_MARKER, 1)[0]
    # Strip the parent-wins note line + blank line that precede the body.
    note_end = body_before_marker.find("\n\n")
    assert note_end != -1
    capped_body = body_before_marker[note_end + 2 :]
    assert len(capped_body.rstrip("\n")) <= _ADDENDUM_CHAR_CAP


def test_multiple_children_appended_in_name_order(tmp_path, caplog):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "parent",
        "---\ndescription: Parent\nload: always\n---\n\n# Parent\n",
    )
    # Non-canonical names still honored; sorted by name.
    _write_skill(
        skills,
        "zzz-extra",
        "---\ndescription: Later\nextends: parent\n---\n\nZ content.\n",
    )
    _write_skill(
        skills,
        "aaa-extra",
        "---\ndescription: Earlier\nextends: parent\n---\n\nA content.\n",
    )
    invalidate_skill_cache(tmp_path)

    with caplog.at_level(logging.WARNING, logger="ouro_agents.skills"):
        text = load_startup_skills(_config(tmp_path))

    assert text.index("## Agent addendum: aaa-extra") < text.index(
        "## Agent addendum: zzz-extra"
    )
    assert "A content." in text
    assert "Z content." in text
    assert any("Multiple addenda" in r.message for r in caplog.records)


def test_cache_invalidation_picks_up_new_addendum(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "outreach",
        "---\ndescription: Outreach\nload: always\n---\n\n# Outreach\n",
    )
    invalidate_skill_cache(tmp_path)

    before = load_startup_skills(_config(tmp_path))
    assert "Prefer coil" not in before

    _write_skill(
        skills,
        "outreach-addendum",
        "---\ndescription: Addendum\nextends: outreach\n---\n\n"
        "Prefer coil triage.\n",
    )
    # Without invalidation, cached index would miss the new file.
    invalidate_skill_cache(tmp_path)
    after = load_startup_skills(_config(tmp_path))
    assert "## Agent addendum: outreach-addendum" in after
    assert "Prefer coil triage." in after


def test_resolve_skill_child_returns_body_only(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "outreach",
        "---\ndescription: Outreach\nload: always\n---\n\n# Outreach\n",
    )
    _write_skill(
        skills,
        "outreach-addendum",
        "---\ndescription: Addendum\nextends: outreach\n---\n\n"
        "Addendum-only body.\n",
    )
    invalidate_skill_cache(tmp_path)

    child = resolve_skill("outreach-addendum", workspace=tmp_path)
    assert child is not None
    assert "Addendum-only body." in child
    assert "# Outreach" not in child
    assert "Agent addendum" not in child
