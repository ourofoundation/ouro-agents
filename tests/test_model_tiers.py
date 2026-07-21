import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from ouro_agents.agent import OuroAgent
from ouro_agents.config import (
    MODEL_ROLE_TIERS,
    OuroAgentsConfig,
    tier_spec_for_role,
)


def _legacy_config() -> dict:
    return {
        "agent": {
            "name": "hermes",
            "model": "openai/gpt-4.1-mini",
            "workspace": "./workspace",
            "reasoning": {"effort": "medium"},
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


def _tiered_config() -> dict:
    return {
        "agent": {
            "name": "hermes",
            "workspace": "./workspace",
        },
        "models": {
            "strong": {
                "id": "z-ai/glm-5.2",
                "reasoning": {"effort": "medium"},
            },
            "light": {
                "id": "xiaomi/mimo-v2.5",
                "reasoning": {"effort": "none"},
            },
        },
        "modes": {
            "heartbeat": {"enabled": True, "every": "1h"},
            "planning": {"enabled": True},
        },
        "subagents": {
            "strategist": {"max_steps": 6},
            "research": {"max_steps": 10},
            "writer": {"max_steps": 8},
        },
        "mcp_servers": [],
        "memory": {
            "embedder": "openai/text-embedding-3-small",
        },
    }


def _load(data: dict) -> OuroAgentsConfig:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        path.write_text(json.dumps(data))
        return OuroAgentsConfig.load_from_file(path)


class TestModelTierHydration(unittest.TestCase):
    def test_hydrates_required_fields_from_tiers(self):
        config = _load(_tiered_config())

        self.assertEqual(config.agent.model, "z-ai/glm-5.2")
        self.assertEqual(config.agent.reasoning.effort, "medium")
        self.assertEqual(config.heartbeat.model, "z-ai/glm-5.2")
        self.assertEqual(config.heartbeat.reasoning.effort, "medium")
        self.assertEqual(config.planning.model, "z-ai/glm-5.2")
        self.assertEqual(config.memory.extraction_model, "xiaomi/mimo-v2.5")

    def test_explicit_overrides_win_over_tiers(self):
        data = _tiered_config()
        data["agent"]["model"] = "moonshotai/kimi-k3"
        data["agent"]["reasoning"] = {"effort": "high"}
        data["modes"]["heartbeat"]["model"] = "openai/gpt-4.1-mini"
        data["memory"]["extraction_model"] = "openai/gpt-4.1-nano"
        data["subagents"]["research"]["model"] = "anthropic/claude-sonnet-4"

        config = _load(data)

        self.assertEqual(config.agent.model, "moonshotai/kimi-k3")
        self.assertEqual(config.agent.reasoning.effort, "high")
        self.assertEqual(config.heartbeat.model, "openai/gpt-4.1-mini")
        self.assertEqual(config.memory.extraction_model, "openai/gpt-4.1-nano")
        self.assertEqual(
            config.subagents.profiles["research"].model,
            "anthropic/claude-sonnet-4",
        )

    def test_mid_tier_is_used_for_heartbeat_when_present(self):
        data = _tiered_config()
        data["models"]["mid"] = {
            "id": "moonshotai/kimi-k3",
            "reasoning": {"enabled": True},
        }

        config = _load(data)

        self.assertEqual(config.heartbeat.model, "moonshotai/kimi-k3")
        self.assertTrue(config.heartbeat.reasoning.enabled)

    def test_legacy_config_without_models_still_loads(self):
        config = _load(_legacy_config())
        self.assertIsNone(config.models)
        self.assertEqual(config.agent.model, "openai/gpt-4.1-mini")


class TestModelRoleTiers(unittest.TestCase):
    def test_role_map_matches_hermes_split(self):
        self.assertEqual(MODEL_ROLE_TIERS["agent"], "strong")
        self.assertEqual(MODEL_ROLE_TIERS["writer"], "strong")
        self.assertEqual(MODEL_ROLE_TIERS["planning"], "strong")
        self.assertEqual(MODEL_ROLE_TIERS["strategist"], "strong")
        self.assertEqual(MODEL_ROLE_TIERS["preflight"], "strong")
        self.assertEqual(MODEL_ROLE_TIERS["heartbeat_preflight"], "strong")
        self.assertEqual(MODEL_ROLE_TIERS["search"], "light")
        self.assertEqual(MODEL_ROLE_TIERS["research"], "light")
        self.assertEqual(MODEL_ROLE_TIERS["reflector"], "light")
        self.assertEqual(MODEL_ROLE_TIERS["utility"], "light")
        self.assertEqual(MODEL_ROLE_TIERS["chat"], "mid")
        self.assertEqual(MODEL_ROLE_TIERS["heartbeat"], "mid")

    def test_mid_falls_back_to_strong(self):
        config = _load(_tiered_config())
        spec = tier_spec_for_role(config.models, "heartbeat")
        self.assertEqual(spec.id, "z-ai/glm-5.2")
        chat_spec = tier_spec_for_role(config.models, "chat")
        self.assertEqual(chat_spec.id, "z-ai/glm-5.2")

    def test_legacy_proactive_migrates_to_servers(self):
        data = _tiered_config()
        data["modes"]["heartbeat"]["proactive"] = {
            "enabled": True,
            "servers": ["ouro", "search"],
        }
        config = _load(data)
        self.assertEqual(config.heartbeat.servers, ["ouro"])
        self.assertFalse(hasattr(config.heartbeat, "proactive"))


class TestCompletionCaps(unittest.TestCase):
    def test_build_model_applies_role_completion_cap(self):
        from ouro_agents.config import ROLE_MAX_COMPLETION_TOKENS

        agent = OuroAgent.__new__(OuroAgent)
        agent.config = _load(_tiered_config())
        agent._usage_tracker = object()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("z-ai/glm-5.2", role="heartbeat")

        self.assertEqual(
            tracked_model.call_args.kwargs["max_tokens"],
            ROLE_MAX_COMPLETION_TOKENS["heartbeat"],
        )


class TestAgentModelResolution(unittest.TestCase):
    def _agent(self, data: dict) -> OuroAgent:
        config = _load(data)
        agent = OuroAgent.__new__(OuroAgent)
        agent.config = config
        agent._usage_tracker = object()
        return agent

    def test_subagent_uses_role_tier(self):
        agent = self._agent(_tiered_config())
        profile = SimpleNamespace(name="research", model_override=None)

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._resolve_subagent_model(profile)

        self.assertEqual(
            tracked_model.call_args.kwargs["model_id"],
            "xiaomi/mimo-v2.5",
        )
        reasoning = tracked_model.call_args.kwargs["extra_body"]["reasoning"]
        self.assertEqual(reasoning, {"effort": "none"})

    def test_writer_uses_strong_tier(self):
        agent = self._agent(_tiered_config())
        profile = SimpleNamespace(name="writer", model_override=None)

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._resolve_subagent_model(profile)

        self.assertEqual(
            tracked_model.call_args.kwargs["model_id"],
            "z-ai/glm-5.2",
        )

    def test_utility_model_uses_light(self):
        agent = self._agent(_tiered_config())
        self.assertEqual(agent._utility_model_id(), "xiaomi/mimo-v2.5")

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model(agent._utility_model_id(), role="utility")

        reasoning = tracked_model.call_args.kwargs["extra_body"]["reasoning"]
        self.assertEqual(reasoning, {"effort": "none"})

    def test_explicit_subagent_model_beats_tier(self):
        data = _tiered_config()
        data["subagents"]["research"]["model"] = "openai/gpt-4.1-mini"
        agent = self._agent(data)
        profile = SimpleNamespace(name="research", model_override=None)

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._resolve_subagent_model(profile)

        self.assertEqual(
            tracked_model.call_args.kwargs["model_id"],
            "openai/gpt-4.1-mini",
        )

    def test_legacy_subagent_falls_back_to_agent_model(self):
        agent = self._agent(_legacy_config())
        profile = SimpleNamespace(name="research", model_override=None)

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._resolve_subagent_model(profile)

        self.assertEqual(
            tracked_model.call_args.kwargs["model_id"],
            "openai/gpt-4.1-mini",
        )

    def test_chat_role_uses_mid_tier(self):
        data = _tiered_config()
        data["models"]["mid"] = {
            "id": "moonshotai/kimi-k3",
            "reasoning": {"enabled": True},
        }
        agent = self._agent(data)

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model(
                agent._model_id_for_role("chat"),
                role="chat",
                conversational=True,
            )

        self.assertEqual(
            tracked_model.call_args.kwargs["model_id"],
            "moonshotai/kimi-k3",
        )
        reasoning = tracked_model.call_args.kwargs["extra_body"]["reasoning"]
        self.assertEqual(reasoning, {"enabled": True})

    def test_chat_role_falls_back_to_strong_without_mid(self):
        agent = self._agent(_tiered_config())
        self.assertEqual(agent._model_id_for_role("chat"), "z-ai/glm-5.2")


class TestHermesConfigLoads(unittest.TestCase):
    def test_hermes_json_loads_with_tiers(self):
        path = Path(__file__).resolve().parents[1] / "hermes.json"
        if not path.exists():
            self.skipTest("hermes.json not present")
        config = OuroAgentsConfig.load_from_file(path)
        self.assertIsNotNone(config.models)
        self.assertEqual(config.agent.model, config.models.strong.id)
        self.assertEqual(
            config.memory.extraction_model, config.models.light.id
        )
        strategist = config.subagents.profiles["strategist"]
        self.assertIsNone(strategist.model)
        self.assertIsNone(strategist.reasoning)
        self.assertEqual(strategist.max_steps, 6)


if __name__ == "__main__":
    unittest.main()
