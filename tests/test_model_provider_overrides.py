import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ouro_agents.agent import OuroAgent


def make_agent(
    *,
    openrouter_provider=None,
    heartbeat_openrouter_provider=None,
    subagent_profiles=None,
    caching_enabled=False,
    caching_ttl="5m",
):
    agent = OuroAgent.__new__(OuroAgent)
    agent.config = SimpleNamespace(
        agent=SimpleNamespace(reasoning=None),
        models=None,
        prompt_caching=SimpleNamespace(enabled=caching_enabled, ttl=caching_ttl),
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


class TestModelProviderOverrides(unittest.TestCase):
    def _make_agent(self, **kwargs):
        return make_agent(**kwargs)

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

    def test_defaults_to_auto_tool_choice_for_glm(self):
        # Z.AI's GLM endpoint advertises no route for tool_choice="required".
        agent = self._make_agent()

        self.assertEqual(agent._default_tool_choice("z-ai/glm-5.2"), "auto")

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

    def test_conversational_runs_always_use_auto_tool_choice(self):
        # Chat must be free to answer a casual message with plain content;
        # smolagents' default tool_choice="required" forces pointless tool
        # calls on greetings.
        agent = self._make_agent()

        self.assertEqual(
            agent._default_tool_choice("openai/gpt-4.1-mini", conversational=True),
            "auto",
        )
        self.assertEqual(
            agent._default_tool_choice("moonshotai/kimi-k3", conversational=True),
            "auto",
        )

    def test_build_model_passes_auto_tool_choice_for_conversational(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("openai/gpt-4.1-mini", conversational=True)

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

    def test_build_model_passes_openrouter_attribution_headers(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("openai/gpt-4.1-mini")

        client_kwargs = tracked_model.call_args.kwargs["client_kwargs"]
        headers = client_kwargs["default_headers"]
        self.assertEqual(headers["HTTP-Referer"], "https://ouro.foundation")
        self.assertEqual(headers["X-OpenRouter-Title"], "Ouro")
        self.assertEqual(
            headers["X-OpenRouter-Categories"], "personal-agent,cloud-agent"
        )

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

    def test_replay_allowlisted_families_are_pinned_first_party(self):
        # Kimi/GLM reasoning_details (format="unknown") are only replayable
        # same-provider, so builds must hard-pin them even when the global
        # config allows fallbacks.
        agent = self._make_agent(
            openrouter_provider={"allow_fallbacks": True},
        )

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("moonshotai/kimi-k3")

        provider = tracked_model.call_args.kwargs["extra_body"]["provider"]
        self.assertEqual(provider["order"], ["moonshotai"])
        self.assertFalse(provider["allow_fallbacks"])

    def test_glm_is_pinned_to_zai(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("z-ai/glm-5.2")

        provider = tracked_model.call_args.kwargs["extra_body"]["provider"]
        self.assertEqual(provider["order"], ["z-ai"])
        self.assertFalse(provider["allow_fallbacks"])

    def test_other_models_are_not_pinned(self):
        agent = self._make_agent()

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("xiaomi/mimo-v2.5")

        extra_body = tracked_model.call_args.kwargs.get("extra_body")
        if extra_body is not None:
            self.assertNotIn("provider", extra_body)

    def test_kimi_reasoning_effort_is_normalized_to_enabled(self):
        # K3 thinking is always on; non-max efforts must not be forwarded.
        from ouro_agents.config import ReasoningConfig

        agent = self._make_agent()
        agent.config.agent.reasoning = ReasoningConfig(effort="medium")

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("moonshotai/kimi-k3")

        reasoning = tracked_model.call_args.kwargs["extra_body"]["reasoning"]
        self.assertEqual(reasoning, {"enabled": True})

    def test_kimi_reasoning_effort_max_is_forwarded(self):
        from ouro_agents.config import ReasoningConfig

        agent = self._make_agent()
        agent.config.agent.reasoning = ReasoningConfig(effort="max")

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("moonshotai/kimi-k3")

        reasoning = tracked_model.call_args.kwargs["extra_body"]["reasoning"]
        self.assertEqual(reasoning, {"enabled": True, "effort": "max"})

    def test_non_kimi_reasoning_effort_is_forwarded(self):
        from ouro_agents.config import ReasoningConfig

        agent = self._make_agent()
        agent.config.agent.reasoning = ReasoningConfig(effort="medium")

        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model("z-ai/glm-5.2")

        reasoning = tracked_model.call_args.kwargs["extra_body"]["reasoning"]
        self.assertEqual(reasoning, {"effort": "medium"})

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


class TestExplicitCacheDetection(unittest.TestCase):
    def setUp(self):
        self.agent = OuroAgent.__new__(OuroAgent)

    def test_commercial_qwen_tiers_supported(self):
        for model_id in (
            "qwen/qwen3.7-plus",
            "qwen/qwen3.7-max",
            "qwen/qwen3.6-flash",
            "qwen/qwen3-max",
            "qwen/qwen3.6-max-preview",
        ):
            self.assertTrue(
                self.agent._supports_explicit_cache(model_id), model_id
            )

    def test_excluded_qwen_and_other_models(self):
        for model_id in (
            "qwen/qwen3-coder-plus",  # middle qualifier
            "qwen/qwen3-vl-max",  # middle qualifier
            "qwen/qwen3.5-plus-02-15",  # dated snapshot
            "qwen/qwen3-235b-a22b",  # open-source
            "anthropic/claude-sonnet-4",  # uses top-level path instead
            "openai/gpt-4.1-mini",
            "deepseek/deepseek-v4-pro",
        ):
            self.assertFalse(
                self.agent._supports_explicit_cache(model_id), model_id
            )


class TestBuildModelCaching(unittest.TestCase):
    def _build(self, model_id, **agent_kwargs):
        agent = make_agent(**agent_kwargs)
        with (
            patch("ouro_agents.agent.TrackedOpenAIModel") as tracked_model,
            patch("ouro_agents.agent.get_display") as get_display,
        ):
            get_display.return_value = SimpleNamespace(reasoning=None)
            agent._build_model(model_id)
        return tracked_model.call_args.kwargs

    def test_qwen_commercial_gets_breakpoints_when_enabled(self):
        kwargs = self._build("qwen/qwen3.7-plus", caching_enabled=True, caching_ttl="1h")
        self.assertTrue(kwargs["cache_breakpoints"])
        self.assertEqual(kwargs["cache_ttl"], "1h")

    def test_no_breakpoints_when_caching_disabled(self):
        kwargs = self._build("qwen/qwen3.7-plus", caching_enabled=False)
        self.assertFalse(kwargs["cache_breakpoints"])

    def test_anthropic_does_not_use_message_breakpoints(self):
        # Anthropic caches via the top-level cache_control field, not injection.
        kwargs = self._build("anthropic/claude-sonnet-4", caching_enabled=True)
        self.assertFalse(kwargs["cache_breakpoints"])


if __name__ == "__main__":
    unittest.main()
