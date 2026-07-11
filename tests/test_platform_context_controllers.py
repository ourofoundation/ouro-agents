from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from ouro_agents.agent import OuroAgent
from ouro_agents.platform_context_prompt import (
    _MAX_TEAM_DESCRIPTION_CHARS,
    format_platform_context_for_prompt,
)


class TestPlatformContextControllers(unittest.TestCase):
    def test_format_truncates_long_team_descriptions(self) -> None:
        long_desc = (
            "General computational materials science research.\n\n"
            "Getting started\n\n"
            + ("More onboarding detail. " * 40)
            + "\n\n```assetComponent\n{\"assetType\": \"route\"}\n```"
        )
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = workspace / "data"
            data_dir.mkdir()
            (data_dir / "platform_context.json").write_text(
                json.dumps(
                    {
                        "profile": {
                            "id": "agent-1",
                            "username": "hermes",
                            "display_name": "Hermes",
                        },
                        "organizations": [],
                        "teams": [
                            {
                                "id": "team-1",
                                "name": "materials-science",
                                "org_id": "org-1",
                                "organization_name": "all",
                                "role": "write",
                                "agent_can_create": True,
                                "description": long_desc,
                            },
                            {
                                "id": "team-2",
                                "name": "life",
                                "org_id": "org-1",
                                "organization_name": "all",
                                "role": "write",
                                "description": "Like Instagram, but better",
                            },
                        ],
                        "controllers": [],
                    }
                )
            )

            text = format_platform_context_for_prompt(workspace)
            materials_line = next(
                line for line in text.splitlines() if line.startswith("- materials-science")
            )
            life_line = next(
                line for line in text.splitlines() if line.startswith("- life")
            )

            self.assertNotIn("assetComponent", materials_line)
            self.assertNotIn("\n", materials_line)
            self.assertTrue(materials_line.endswith("…"))
            desc = materials_line.split(" — ", 1)[1]
            self.assertLessEqual(len(desc), _MAX_TEAM_DESCRIPTION_CHARS + 1)
            self.assertLess(
                desc.count("More onboarding detail."),
                long_desc.count("More onboarding detail."),
            )
            self.assertIn("Like Instagram, but better", life_line)
            self.assertFalse(life_line.endswith("…"))

    def test_format_includes_controllers_and_share_hint(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = workspace / "data"
            data_dir.mkdir()
            (data_dir / "platform_context.json").write_text(
                json.dumps(
                    {
                        "base_url": "https://ouro.foundation",
                        "profile": {
                            "id": "agent-1",
                            "username": "hermes",
                            "display_name": "Hermes",
                        },
                        "organizations": [],
                        "teams": [],
                        "controllers": [
                            {
                                "username": "mmoderwell",
                                "user_id": "847f4445-78ee-41b1-913b-5bd155c71b13",
                            },
                            {"user_id": "00000000-0000-0000-0000-000000000099"},
                        ],
                    }
                )
            )

            text = format_platform_context_for_prompt(workspace)

            self.assertIn("You are: Hermes (@hermes)", text)
            self.assertIn("Your controllers (privileged humans who operate you):", text)
            self.assertIn(
                "@mmoderwell (user_id: 847f4445-78ee-41b1-913b-5bd155c71b13)",
                text,
            )
            self.assertIn("user_id: 00000000-0000-0000-0000-000000000099", text)
            self.assertIn("share_asset", text)
            self.assertIn("Mentions and links do not grant access", text)

    def test_controller_context_entries_pairs_usernames_and_ids(self) -> None:
        agent = OuroAgent.__new__(OuroAgent)
        agent.config = SimpleNamespace(
            security=SimpleNamespace(
                controllers=[
                    "mmoderwell",
                    "00000000-0000-0000-0000-000000000001",
                    "unresolved",
                ],
                resolved_controller_ids=[
                    "847f4445-78ee-41b1-913b-5bd155c71b13",
                    "00000000-0000-0000-0000-000000000001",
                    "00000000-0000-0000-0000-000000000002",
                ],
            )
        )
        agent._load_security_id_cache = lambda: {  # type: ignore[method-assign]
            "mmoderwell": "847f4445-78ee-41b1-913b-5bd155c71b13"
        }

        entries = OuroAgent._controller_context_entries(agent)

        self.assertEqual(
            entries,
            [
                {
                    "username": "mmoderwell",
                    "user_id": "847f4445-78ee-41b1-913b-5bd155c71b13",
                },
                {"user_id": "00000000-0000-0000-0000-000000000001"},
                {"user_id": "00000000-0000-0000-0000-000000000002"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
