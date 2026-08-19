"""Scheduled-task learnings must not collide with the refinement package."""

from ouro_agents.task_learnings import (
    MAX_LEARNINGS,
    RefinementResult,
    apply_learnings,
    format_learnings_for_prompt,
)


def test_format_learnings_empty():
    assert format_learnings_for_prompt([]) == ""


def test_format_learnings_injects_section():
    text = format_learnings_for_prompt(["use type='post' filter"])
    assert "## Learnings from Previous Runs" in text
    assert "use type='post' filter" in text


def test_apply_learnings_drop_and_append():
    existing = ["old-0", "old-1", "old-2"]
    result = RefinementResult(
        new_learnings=["new-a", "old-1", "new-b"],
        drop_learnings=["old-0"],
    )
    assert apply_learnings(existing, result) == ["old-1", "old-2", "new-a", "new-b"]


def test_apply_learnings_caps_to_most_recent():
    existing = [f"old-{i}" for i in range(MAX_LEARNINGS)]
    result = RefinementResult(new_learnings=["new"])
    updated = apply_learnings(existing, result)
    assert len(updated) == MAX_LEARNINGS
    assert updated[-1] == "new"
    assert "old-0" not in updated


def test_scheduler_imports_task_learnings_not_package():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "ouro_agents" / "scheduler.py"
    tree = ast.parse(source.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "task_learnings"
        for alias in node.names
    }
    assert imported == {"format_learnings_for_prompt", "apply_learnings", "refine"}
    stale = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "refinement"
        and {a.name for a in node.names}
        & {"format_learnings_for_prompt", "apply_learnings", "refine"}
    ]
    assert stale == []
