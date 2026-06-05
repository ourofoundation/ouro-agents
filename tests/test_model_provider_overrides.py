import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ouro_agents.agent import OuroAgent


class TestModelProviderOverrides(unittest.TestCase):
    def _make_agent(
        self,
        *,
        openrouter_provider=None,
        heartbeat_openrouter_provider=None,
        subagent_profiles=None,
    ):
        agent = OuroAgent.__new__(OuroAgent)
        agent.config = SimpleNamespace(
            agent=SimpleNamespace(reasoning=None),
            prompt_caching=SimpleNamespace(enabled=False, ttl="5m"),
            heartbeat=SimpleNamespace(
                reasoning=None,
                model="openai/gpt-4.1-mini",
                openrouter_provider=heartbeat_openrouter_provider,
            ),
            subagents=SimpleNamespace(profiles=subagent_profiles or {}),
            openrouter_provider=openrouter_provider,
        )
        agent._usage_tracker = object()
        return agent

    def test_defaults_to_auto_tool_choice_for_minimax(self):
        agent = self._make_agent()

        self.assertEqual(agent._default_tool_choice("minimax/minimax-m2.7"), "auto")
        self.assertIsNone(agent._default_tool_choice("openai/gpt-4.1-mini"))

    def test_defaults_to_auto_tool_choice_for_deepseek(self):
        # DeepSeek's own provider serves reasoning-enabled models as
        # ``deepseek-reasoner``, which rejects ``tool_choice="required"`` with
        # a 400. ``auto`` works for both the reasoning and chat variants.
        agent = self._make_agent()

        self.assertEqual(
            agent._default_tool_choice("deepseek/deepseek-v4-pro"), "auto"
        )
        self.assertEqual(
            agent._default_tool_choice("deepseek/deepseek-chat"), "auto"
        )

    def test_defaults_to_auto_tool_choice_for_qwen(self):
        agent = self._make_agent()

        self.assertEqual(
            agent._default_tool_choice("qwen/qwen3.7-max"), "auto"
        )

    def test_build_model_passes_auto_tool_choice_for_qwen(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("qwen/qwen3.7-max")

        self.assertEqual(tracked_model.call_args.kwargs["tool_choice"], "auto")

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

    def test_global_openrouter_provider_is_passed_in_extra_body(self):
        agent = self._make_agent(
            openrouter_provider={"ignore": ["Parasail"], "allow_fallbacks": True},
        )

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("deepseek/deepseek-v4-pro")

        extra_body = tracked_model.call_args.kwargs.get("extra_body")
        self.assertEqual(
            extra_body["provider"],
            {"ignore": ["Parasail"], "allow_fallbacks": True},
        )

    def test_subagent_provider_overrides_global(self):
        subagent_override = SimpleNamespace(
            reasoning=None,
            openrouter_provider={"order": ["DeepSeek"], "allow_fallbacks": False},
        )
        agent = self._make_agent(
            openrouter_provider={"ignore": ["Parasail"], "allow_fallbacks": True},
            subagent_profiles={"research": subagent_override},
        )

        resolved = agent._resolve_openrouter_provider(subagent_profile="research")

        # Subagent overlay merges over the global default (later wins).
        self.assertEqual(
            resolved,
            {
                "ignore": ["Parasail"],
                "allow_fallbacks": False,
                "order": ["DeepSeek"],
            },
        )

    def test_no_provider_block_when_unset(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("deepseek/deepseek-v4-pro")

        self.assertNotIn("extra_body", tracked_model.call_args.kwargs)

    def test_minimax_routes_request_reasoning_split(self):
        # MiniMax leaks tool-call tokens into content when its thinking shares
        # the content channel; reasoning_split keeps the channels separate.
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("minimax/minimax-m3")

        extra_body = tracked_model.call_args.kwargs.get("extra_body")
        self.assertIsNotNone(extra_body)
        self.assertTrue(extra_body["reasoning_split"])

    def test_non_minimax_routes_omit_reasoning_split(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("openai/gpt-4.1-mini")

        extra_body = tracked_model.call_args.kwargs.get("extra_body")
        if extra_body is not None:
            self.assertNotIn("reasoning_split", extra_body)


if __name__ == "__main__":
    unittest.main()
