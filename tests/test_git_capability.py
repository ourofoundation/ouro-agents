import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ouro_agents.config import SandboxConfig
from ouro_agents.git_capability import detect_git_capability, refused_git_command
from ouro_agents.skills import (
    get_skill_directory,
    list_skill_names,
    resolve_skill,
    set_skill_capabilities,
)
from ouro_agents.tools.docker_sandbox import DockerSandboxSession


def _sandbox(**overrides) -> SandboxConfig:
    fields = {
        "mode": "docker",
        "enable_shell": True,
        "env_allowlist": ["OURO_API_KEY", "GH_TOKEN"],
    }
    fields.update(overrides)
    return SandboxConfig(**fields)


def _repo(path: Path, origin: str | None = None) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
        )
    return path


def test_no_shell_means_no_git(tmp_path):
    cap = detect_git_capability(_repo(tmp_path), _sandbox(enable_shell=False), {})
    assert cap.level == "none"
    assert cap.skill_capabilities == frozenset()


def test_not_a_repo_means_no_git(tmp_path):
    assert detect_git_capability(tmp_path, _sandbox(), {}).level == "none"


@pytest.mark.parametrize(
    ("origin", "env", "reason_fragment"),
    [
        (None, {"GH_TOKEN": "t"}, "no `origin`"),
        ("git@github.com:org/agent.git", {"GH_TOKEN": "t"}, "SSH"),
        ("ssh://git@github.com/org/agent.git", {"GH_TOKEN": "t"}, "SSH"),
        ("https://x-access-token:t@github.com/org/agent.git", {"GH_TOKEN": "t"}, "credentials"),
        ("https://gitlab.com/org/agent.git", {"GH_TOKEN": "t"}, "not an HTTPS GitHub"),
        ("https://github.com/org/agent.git", {"GH_TOKEN": ""}, "GH_TOKEN is empty"),
        ("https://github.com/org/agent.git", {}, "GH_TOKEN is empty"),
    ],
)
def test_local_only_reasons(tmp_path, origin, env, reason_fragment):
    cap = detect_git_capability(_repo(tmp_path, origin), _sandbox(), env)
    assert cap.level == "local"
    assert reason_fragment in cap.reason
    assert cap.skill_capabilities == frozenset({"git-local"})


def test_token_not_forwarded_is_local(tmp_path):
    repo = _repo(tmp_path, "https://github.com/org/agent.git")
    cap = detect_git_capability(
        repo, _sandbox(env_allowlist=["OURO_API_KEY"]), {"GH_TOKEN": "t"}
    )
    assert cap.level == "local"
    assert "env_allowlist" in cap.reason


def test_https_github_with_token_is_remote(tmp_path):
    repo = _repo(tmp_path, "https://github.com/org/agent.git")
    cap = detect_git_capability(repo, _sandbox(), {"GH_TOKEN": "t"})
    assert cap.level == "remote"
    assert cap.skill_capabilities == frozenset({"git-remote"})


@pytest.mark.parametrize(
    ("capabilities", "present", "absent"),
    [
        (set(), set(), {"git", "git-local"}),
        ({"git-local"}, {"git-local"}, {"git"}),
        ({"git-remote"}, {"git"}, {"git-local"}),
    ],
)
def test_git_skills_follow_capabilities(tmp_path, capabilities, present, absent):
    set_skill_capabilities(tmp_path, capabilities)
    names = set(list_skill_names(tmp_path))
    assert present <= names
    assert not (absent & names)
    for name in absent:
        assert resolve_skill(name, tmp_path) is None


def test_gated_workspace_skill_hidden_from_directory(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "deploy.md").write_text(
        "---\ndescription: Deploy things\nrequires: [git-remote, modal]\n---\n\nbody\n"
    )
    set_skill_capabilities(tmp_path, {"git-remote"})
    assert "deploy" not in list_skill_names(tmp_path)
    set_skill_capabilities(tmp_path, {"git-remote", "modal"})
    assert "- deploy: Deploy things" in get_skill_directory_for(tmp_path)


def get_skill_directory_for(workspace: Path) -> str:
    from types import SimpleNamespace

    return get_skill_directory(SimpleNamespace(agent=SimpleNamespace(workspace=workspace)))


@pytest.mark.parametrize(
    "command",
    [
        'git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/o/a.git"',
        'git push -u "https://x-access-token:${GH_TOKEN}@github.com/o/a.git" HEAD',
        "cd /workspace && git remote add backup https://github.com/o/a.git",
        "git -C /workspace remote set-url origin https://github.com/o/a.git",
        "git config credential.helper store",
        "git config --global url.https://github.com/.insteadOf git@github.com:",
        "git push --force origin HEAD",
        "git push -f origin HEAD",
        "git status && git push origin +HEAD:main",
    ],
)
def test_refuses_credential_and_history_bypasses(command):
    assert refused_git_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "cd /workspace && git status --short --branch | head -25",
        "git push -u origin HEAD 2>&1",
        "git push --follow-tags origin HEAD",
        "git clone --depth 1 https://github.com/nayoung10/MOFFlow-2.git scratch/m",
        "git remote -v",
        "git log --oneline -5 | tail -f",
        "psql postgresql://user:pass@localhost/db -c 'select 1'",
        "git config user.name",
    ],
)
def test_allows_ordinary_git(command):
    assert refused_git_command(command) is None


def _run_args(tmp_path, env) -> list[str]:
    session = DockerSandboxSession(
        config=_sandbox(image="img"),
        workspace=tmp_path,
        agent_name="a",
        run_id="r",
    )
    with patch.dict("os.environ", env, clear=True):
        return session._docker_run_args()


def test_sandbox_configures_gh_credential_helper_only_with_token(tmp_path):
    with_token = _run_args(tmp_path, {"GH_TOKEN": "t"})
    assert "GIT_TERMINAL_PROMPT=0" in with_token
    assert "GIT_CONFIG_VALUE_0=!gh auth git-credential" in with_token

    without = _run_args(tmp_path, {"GH_TOKEN": ""})
    assert "GIT_TERMINAL_PROMPT=0" in without
    assert not any(a.startswith("GIT_CONFIG_") for a in without)
