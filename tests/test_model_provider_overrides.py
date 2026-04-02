import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ouro_agents.agent import OuroAgent


class TestModelProviderOverrides(unittest.TestCase):
    def _make_agent(self):
        agent = OuroAgent.__new__(OuroAgent)
        agent.config = SimpleNamespace(
            reasoning=None,
            prompt_caching=SimpleNamespace(enabled=False, ttl="5m"),
            heartbeat=SimpleNamespace(reasoning=None, model="openai/gpt-4.1-mini"),
            subagents=SimpleNamespace(profiles={}),
        )
        agent._usage_tracker = object()
        return agent

    def test_defaults_to_auto_tool_choice_for_minimax(self):
        agent = self._make_agent()

        self.assertEqual(agent._default_tool_choice("minimax/minimax-m2.7"), "auto")
        self.assertIsNone(agent._default_tool_choice("openai/gpt-4.1-mini"))

    def test_build_model_passes_auto_tool_choice_for_minimax(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)

            agent._build_model("minimax/minimax-m2.7")

        self.assertEqual(tracked_model.call_args.kwargs["tool_choice"], "auto")

    def test_build_model_leaves_other_models_on_default_tool_choice(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)

            agent._build_model("openai/gpt-4.1-mini")

        self.assertNotIn("tool_choice", tracked_model.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
