import os
import unittest
from unittest.mock import patch

from ouro_agents.constants import openrouter_attribution_headers


class TestOpenRouterAttribution(unittest.TestCase):
    def test_defaults(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "OPENROUTER_HTTP_REFERER",
                "OPENROUTER_APP_TITLE",
                "OPENROUTER_APP_CATEGORIES",
            }
        }
        with patch.dict(os.environ, env, clear=True):
            headers = openrouter_attribution_headers()

        self.assertEqual(
            headers,
            {
                "HTTP-Referer": "https://ouro.foundation",
                "X-OpenRouter-Title": "Ouro",
                "X-OpenRouter-Categories": "personal-agent,cloud-agent",
            },
        )

    def test_env_overrides(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_HTTP_REFERER": "https://example.com",
                "OPENROUTER_APP_TITLE": "Example App",
                "OPENROUTER_APP_CATEGORIES": "cli-agent",
            },
            clear=False,
        ):
            headers = openrouter_attribution_headers()

        self.assertEqual(
            headers,
            {
                "HTTP-Referer": "https://example.com",
                "X-OpenRouter-Title": "Example App",
                "X-OpenRouter-Categories": "cli-agent",
            },
        )

    def test_empty_categories_omits_header(self):
        with patch.dict(
            os.environ,
            {"OPENROUTER_APP_CATEGORIES": "  "},
            clear=False,
        ):
            headers = openrouter_attribution_headers()

        self.assertNotIn("X-OpenRouter-Categories", headers)
        self.assertEqual(headers["HTTP-Referer"], "https://ouro.foundation")
        self.assertEqual(headers["X-OpenRouter-Title"], "Ouro")


if __name__ == "__main__":
    unittest.main()
