import os
import subprocess
from pathlib import Path

from ouro_agents.memory.dream_git import diff_snapshots, snapshot


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dream Test",
        "GIT_AUTHOR_EMAIL": "dream@example.test",
        "GIT_COMMITTER_NAME": "Dream Test",
        "GIT_COMMITTER_EMAIL": "dream@example.test",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def test_dream_git_snapshots_only_workspace_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Dream Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "dream@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Dream Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "dream@example.test")
    _git(tmp_path, "init")
    workspace = tmp_path / "agents" / "hermes"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "NOTES.md").write_text("before\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside before\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")

    before = snapshot(workspace, "before", agent_name="hermes")
    (workspace / "NOTES.md").write_text("after\n")
    outside.write_text("outside after\n")
    _git(tmp_path, "add", "outside.txt")
    after = snapshot(workspace, "after", agent_name="hermes")

    assert before.kind == "git"
    assert after.changed is True
    assert "NOTES.md" in diff_snapshots(workspace, before, after)
    assert "outside.txt" not in _git(tmp_path, "show", "--name-only", "--format=", "HEAD")
    assert "outside.txt" in _git(tmp_path, "diff", "--cached", "--name-only")


def test_dream_snapshot_falls_back_to_files(tmp_path: Path):
    workspace = tmp_path / "agent"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "SOUL.md").write_text("soul\n")
    (workspace / "skills" / "lesson.md").write_text("lesson\n")

    result = snapshot(workspace, "baseline", agent_name="hermes")

    assert result.kind == "files"
    copied = Path(result.ref)
    assert (copied / "SOUL.md").read_text() == "soul\n"
    assert (copied / "skills" / "lesson.md").read_text() == "lesson\n"
