import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from ouro_agents.scaffold import build_sandbox_image, init_agent_project


def test_init_agent_project_creates_standalone_git_ready_project():
    with TemporaryDirectory() as tmpdir:
        target = init_agent_project("atlas", Path(tmpdir) / "atlas")

        config = json.loads((target / "agent.json").read_text())
        assert config["agent"]["name"] == "atlas"
        assert config["agent"]["workspace"] == "."
        assert config["agent"]["data_dir"] == "~/ouro-data/atlas"
        assert config["memory"]["path"] == "~/ouro-data/atlas/memory"
        assert config["agent"]["sandbox"]["image"].startswith(
            "ouro-agents-sandbox:"
        )
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


def test_build_sandbox_image_uses_packaged_dockerfile():
    with (
        patch("ouro_agents.scaffold._runtime_version", return_value="1.2.3"),
        patch("ouro_agents.scaffold.subprocess.run") as run,
    ):
        image = build_sandbox_image()

    assert image == "ouro-agents-sandbox:1.2.3"
    args = run.call_args.args[0]
    assert args[:5] == ["docker", "build", "--file", "-", "--tag"]
    assert args[5] == image
    assert "apt-get install --no-install-recommends --yes git gh" in (
        run.call_args.kwargs["input"]
    )


@pytest.mark.parametrize("name", ["Atlas", "a", "../atlas", "atlas_agent"])
def test_init_agent_project_validates_name(name: str):
    with TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError):
            init_agent_project(name, Path(tmpdir) / "target")
