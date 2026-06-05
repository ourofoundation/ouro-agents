import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ouro_agents.config import OuroAgentsConfig
from ouro_agents.server import dev_reload_settings


def _minimal_config(workspace: str) -> dict:
    return {
        "agent": {
            "name": "hermes",
            "model": "openai/gpt-4.1-mini",
            "workspace": workspace,
        },
        "modes": {"heartbeat": {"model": "openai/gpt-4.1-mini"}},
        "mcp_servers": [],
        "memory": {
            "path": f"{workspace}/memory",
            "extraction_model": "openai/gpt-4.1-mini",
            "embedder": "openai/text-embedding-3-small",
        },
    }


class TestDevReloadSettings(unittest.TestCase):
    def test_watches_package_and_excludes_workspace_and_chroma(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(_minimal_config(str(workspace)))
            )
            config = OuroAgentsConfig.load_from_file(config_path)

            reload_dirs, reload_excludes = dev_reload_settings(config)

            package_root = Path(__file__).resolve().parents[1] / "ouro_agents"
            self.assertEqual(reload_dirs, [str(package_root.resolve())])
            self.assertIn(str(workspace.resolve()), reload_excludes)
            self.assertIn(str((workspace / "memory" / "chroma").resolve()), reload_excludes)
