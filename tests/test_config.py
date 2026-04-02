import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ouro_agents.config import OuroAgentsConfig


def _base_config() -> dict:
    return {
        "agent": {
            "name": "hermes",
            "model": "openai/gpt-4.1-mini",
            "workspace": "./workspace",
        },
        "modes": {
            "heartbeat": {
                "model": "openai/gpt-4.1-mini",
            }
        },
        "mcp_servers": [],
        "memory": {
            "extraction_model": "openai/gpt-4.1-mini",
            "embedder": "openai/text-embedding-3-small",
        },
    }


class TestConfigModeOverrides(unittest.TestCase):
    def _load_config(self, data: dict) -> OuroAgentsConfig:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(json.dumps(data))
            return OuroAgentsConfig.load_from_file(path)

    def test_normalizes_friendly_mode_aliases(self):
        data = _base_config()
        data["modes"].update(
            {
                "run": {"max_steps": 11},
                "planning": {"max_steps": 4},
                "chat_reply": {"max_steps": 7},
            }
        )

        config = self._load_config(data)

        self.assertEqual(config.modes.profiles["autonomous"].max_steps, 11)
        self.assertEqual(config.modes.profiles["plan"].max_steps, 4)
        self.assertEqual(config.modes.profiles["chat-reply"].max_steps, 7)

    def test_hydrates_heartbeat_and_planning_sections_from_modes(self):
        data = _base_config()
        data["modes"]["heartbeat"].update(
            {
                "enabled": False,
                "every": "2h",
                "reasoning": {"effort": "low"},
                "max_steps": 8,
            }
        )
        data["modes"]["planning"] = {
            "enabled": True,
            "model": "anthropic/claude-4.6-sonnet",
            "cadence": "4h",
            "min_heartbeats": 5,
            "review_window": "1h",
            "auto_approve": False,
            "max_steps": 6,
        }

        config = self._load_config(data)

        self.assertFalse(config.heartbeat.enabled)
        self.assertEqual(config.heartbeat.every, "2h")
        self.assertEqual(config.heartbeat.reasoning.effort, "low")
        self.assertTrue(config.planning.enabled)
        self.assertEqual(config.planning.model, "anthropic/claude-4.6-sonnet")
        self.assertEqual(config.planning.min_heartbeats, 5)
        self.assertFalse(config.planning.auto_approve)
        self.assertEqual(config.modes.profiles["heartbeat"].max_steps, 8)
        self.assertEqual(config.modes.profiles["plan"].max_steps, 6)

    def test_flattens_subagent_entries(self):
        data = _base_config()
        data["subagents"] = {
            "default_model": "openai/gpt-4.1-mini",
            "research": {"max_steps": 13},
            "writer": {"model": "anthropic/claude-sonnet-4"},
        }

        config = self._load_config(data)

        self.assertEqual(config.subagents.default_model, "openai/gpt-4.1-mini")
        self.assertEqual(config.subagents.profiles["research"].max_steps, 13)
        self.assertEqual(
            config.subagents.profiles["writer"].model, "anthropic/claude-sonnet-4"
        )

    def test_migrates_legacy_agent_max_steps_into_mode_profiles(self):
        data = _base_config()
        data["agent"]["max_steps"] = {
            "chat": 9,
            "run": 13,
            "planning": 5,
            "heartbeat": 3,
        }

        config = self._load_config(data)

        self.assertEqual(config.modes.profiles["chat"].max_steps, 9)
        self.assertEqual(config.modes.profiles["chat-reply"].max_steps, 9)
        self.assertEqual(config.modes.profiles["autonomous"].max_steps, 13)
        self.assertEqual(config.modes.profiles["plan"].max_steps, 5)
        self.assertEqual(config.modes.profiles["heartbeat"].max_steps, 3)

    def test_migrates_legacy_nested_override_blocks(self):
        data = _base_config()
        data["heartbeat"] = {"model": "openai/gpt-4.1-mini"}
        data["modes"] = {"overrides": {"run": {"max_steps": 8}}}
        data["subagents"] = {"overrides": {"research": {"max_steps": 12}}}

        config = self._load_config(data)

        self.assertEqual(config.modes.profiles["autonomous"].max_steps, 8)
        self.assertEqual(config.subagents.profiles["research"].max_steps, 12)

    def test_legacy_top_level_mode_sections_still_load(self):
        data = _base_config()
        data.pop("modes")
        data["heartbeat"] = {
            "model": "openai/gpt-4.1-mini",
            "enabled": True,
        }
        data["planning"] = {
            "enabled": True,
            "cadence": "6h",
        }

        config = self._load_config(data)

        self.assertTrue(config.heartbeat.enabled)
        self.assertTrue(config.planning.enabled)
        self.assertEqual(config.planning.cadence, "6h")


if __name__ == "__main__":
    unittest.main()
