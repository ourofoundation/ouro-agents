import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ouro_agents.scaffold import init_agent_project


def test_init_agent_project_creates_standalone_git_ready_project():
    with TemporaryDirectory() as tmpdir:
        target = init_agent_project("atlas", Path(tmpdir) / "atlas")

        config = json.loads((target / "agent.json").read_text())
        assert config["agent"]["name"] == "atlas"
        assert config["agent"]["workspace"] == "."
        assert config["agent"]["sandbox"]["enable_shell"] is True
        assert "GH_TOKEN" in config["agent"]["sandbox"]["env_allowlist"]
        assert (target / "SOUL.md").exists()
        assert (target / "HEARTBEAT.md").exists()
        assert (target / "MEMORY.md").exists()
        assert "ouro-agents==" in (target / "pyproject.toml").read_text()
        assert "protected/" in (target / ".gitignore").read_text()


def test_init_agent_project_refuses_to_overwrite_files():
    with TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "atlas"
        target.mkdir()
        (target / "SOUL.md").write_text("existing")

        with pytest.raises(FileExistsError, match="SOUL.md"):
            init_agent_project("atlas", target)

        assert (target / "SOUL.md").read_text() == "existing"


@pytest.mark.parametrize("name", ["Atlas", "a", "../atlas", "atlas_agent"])
def test_init_agent_project_validates_name(name: str):
    with TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError):
            init_agent_project(name, Path(tmpdir) / "target")
