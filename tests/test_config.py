import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
        self.assertEqual(config.modes.profiles["chat"].max_steps, 7)

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

    def test_loads_security_config(self):
        data = _base_config()
        data["security"] = {
            "controllers": ["mmoderwell"],
            "trusted": ["trusted-1"],
            "run_secret": "secret",
        }

        config = self._load_config(data)

        self.assertEqual(config.security.controllers, ["mmoderwell"])
        self.assertEqual(config.security.trusted, ["trusted-1"])
        self.assertEqual(config.security.run_secret, "secret")
        # Runtime-resolved fields start empty; the agent fills them at startup.
        self.assertEqual(config.security.resolved_controller_ids, [])
        self.assertEqual(config.security.resolved_trusted_ids, [])
        self.assertIsNone(config.security.controller_username)

    def test_migrates_legacy_controller_block_into_security(self):
        data = _base_config()
        data["controller"] = {"username": "mmoderwell"}
        data["security"] = {"controllers": ["00000000-0000-0000-0000-000000000001"]}

        config = self._load_config(data)

        self.assertFalse(hasattr(config, "controller"))
        # Controller username leads so it stays the @mention target.
        self.assertEqual(
            config.security.controllers,
            ["mmoderwell", "00000000-0000-0000-0000-000000000001"],
        )

    def test_migrates_legacy_security_key_names(self):
        data = _base_config()
        data["security"] = {
            "controller_user_ids": ["controller-1"],
            "trusted_user_ids": ["trusted-1"],
            "run_shared_secret": "secret",
        }

        config = self._load_config(data)

        self.assertEqual(config.security.controllers, ["controller-1"])
        self.assertEqual(config.security.trusted, ["trusted-1"])
        self.assertEqual(config.security.run_secret, "secret")

    def test_event_pooling_defaults_when_omitted(self):
        config = self._load_config(_base_config())

        self.assertTrue(config.event_pooling.enabled)
        self.assertEqual(
            config.event_pooling.events["new-message"].settle_seconds,
            2.0,
        )
        self.assertEqual(config.event_pooling.events["comment"].settle_seconds, 20.0)
        self.assertEqual(config.event_pooling.events["mention"].max_wait_seconds, 90.0)
        self.assertNotIn("unknown-event", config.event_pooling.events)

    def test_display_serve_progress_defaults_and_overrides(self):
        config = self._load_config(_base_config())

        self.assertTrue(config.display.serve_progress.enabled)
        self.assertEqual(config.display.serve_progress.style, "timeline")
        self.assertTrue(config.display.serve_progress.show_subagents)

        data = _base_config()
        data["display"] = {
            "serve_progress": {
                "enabled": False,
                "style": "compact",
                "show_spinner": False,
                "show_prefetch": False,
                "show_token_updates": False,
                "show_subagents": False,
            }
        }

        config = self._load_config(data)

        self.assertFalse(config.display.serve_progress.enabled)
        self.assertEqual(config.display.serve_progress.style, "compact")
        self.assertFalse(config.display.serve_progress.show_spinner)
        self.assertFalse(config.display.serve_progress.show_prefetch)
        self.assertFalse(config.display.serve_progress.show_token_updates)
        self.assertFalse(config.display.serve_progress.show_subagents)

    def test_loads_event_pooling_per_event_config(self):
        data = _base_config()
        data["event_pooling"] = {
            "enabled": True,
            "events": {
                "new-message": {
                    "enabled": True,
                    "settle_seconds": 1,
                    "jitter_seconds": 2,
                    "max_wait_seconds": 3,
                },
                "comment": {
                    "enabled": False,
                    "settle_seconds": 10,
                    "jitter_seconds": 20,
                    "max_wait_seconds": 30,
                },
            },
        }

        config = self._load_config(data)

        self.assertEqual(
            config.event_pooling.events["new-message"].settle_seconds,
            1.0,
        )
        self.assertEqual(config.event_pooling.events["new-message"].jitter_seconds, 2.0)
        self.assertEqual(config.event_pooling.events["new-message"].max_wait_seconds, 3.0)
        self.assertFalse(config.event_pooling.events["comment"].enabled)
        self.assertEqual(config.event_pooling.events["mention"].settle_seconds, 20.0)

    def test_loads_env_file_declared_in_config(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            env_path = tmp_path / ".env.agent"
            env_path.write_text("TEST_API_KEY=from-config\n")

            data = _base_config()
            data["env_file"] = ".env.agent"
            data["mcp_servers"] = [
                {
                    "name": "test",
                    "transport": "stdio",
                    "command": "echo",
                    "env": {"API_KEY": "${TEST_API_KEY}"},
                }
            ]

            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(data))

            with patch.dict(os.environ, {"TEST_API_KEY": ""}, clear=False):
                config = OuroAgentsConfig.load_from_file(config_path)

            self.assertEqual(config.env_file, env_path)
            self.assertEqual(config.mcp_servers[0].env["API_KEY"], "from-config")

    def test_agent_reasoning_is_loaded(self):
        data = _base_config()
        data["agent"]["reasoning"] = {"effort": "medium"}

        config = self._load_config(data)

        self.assertEqual(config.agent.reasoning.effort, "medium")

    def test_agent_sandbox_defaults_to_local_mode(self):
        config = self._load_config(_base_config())

        self.assertEqual(config.agent.sandbox.mode, "local")
        self.assertEqual(config.agent.sandbox.image, "ouro-agents-sandbox:latest")
        self.assertEqual(config.agent.sandbox.workspace_mount, "/workspace")
        self.assertEqual(config.agent.sandbox.python_packages, [])
        self.assertEqual(
            config.agent.sandbox.env_allowlist,
            ["OURO_API_KEY", "OURO_BASE_URL"],
        )

    def test_agent_sandbox_docker_overrides_load(self):
        data = _base_config()
        data["agent"]["sandbox"] = {
            "mode": "docker",
            "python_packages": ["numpy.*"],
            "image": "custom-sandbox:dev",
            "workspace_mount": "/work",
            "network": "none",
            "memory": "512m",
            "cpus": 0.5,
            "pids_limit": 64,
            "timeout_seconds": 7,
            "max_output_chars": 1234,
            "env_allowlist": ["OURO_API_KEY"],
        }

        config = self._load_config(data)

        self.assertEqual(config.agent.sandbox.mode, "docker")
        self.assertEqual(config.agent.sandbox.python_packages, ["numpy.*"])
        self.assertEqual(config.agent.sandbox.image, "custom-sandbox:dev")
        self.assertEqual(config.agent.sandbox.workspace_mount, "/work")
        self.assertEqual(config.agent.sandbox.network, "none")
        self.assertEqual(config.agent.sandbox.memory, "512m")
        self.assertEqual(config.agent.sandbox.cpus, 0.5)
        self.assertEqual(config.agent.sandbox.pids_limit, 64)
        self.assertEqual(config.agent.sandbox.timeout_seconds, 7)
        self.assertEqual(config.agent.sandbox.max_output_chars, 1234)
        self.assertEqual(config.agent.sandbox.env_allowlist, ["OURO_API_KEY"])

    def test_migrates_legacy_agent_python_packages_into_sandbox(self):
        data = _base_config()
        data["agent"]["python_packages"] = ["numpy.*", "ase.*"]

        config = self._load_config(data)

        self.assertEqual(config.agent.sandbox.python_packages, ["numpy.*", "ase.*"])

    def test_migrates_legacy_top_level_reasoning_into_agent(self):
        data = _base_config()
        data["reasoning"] = {"effort": "low"}

        config = self._load_config(data)

        self.assertEqual(config.agent.reasoning.effort, "low")

    def test_env_file_override_takes_priority_over_config_value(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / ".env.config").write_text("TEST_API_KEY=from-config\n")
            override_env = tmp_path / ".env.override"
            override_env.write_text("TEST_API_KEY=from-override\n")

            data = _base_config()
            data["env_file"] = ".env.config"
            data["mcp_servers"] = [
                {
                    "name": "test",
                    "transport": "stdio",
                    "command": "echo",
                    "env": {"API_KEY": "${TEST_API_KEY}"},
                }
            ]

            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(data))

            with patch.dict(
                os.environ,
                {"ENV_FILE": str(override_env), "TEST_API_KEY": ""},
                clear=False,
            ):
                config = OuroAgentsConfig.load_from_file(config_path)

            self.assertEqual(config.env_file, Path(".env.config"))
            self.assertEqual(config.mcp_servers[0].env["API_KEY"], "from-override")


if __name__ == "__main__":
    unittest.main()
