import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ouro_agents.cli.conversations import create_conversation


class TestCreateConversation(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(conversations=Mock())

    def test_uses_explicit_chat_name(self):
        create_conversation(
            self.client,
            user_id="user-id",
            agent_id="agent-id",
            name="  Project planning  ",
        )

        self.client.conversations.create.assert_called_once_with(
            member_user_ids=["user-id", "agent-id"],
            name="Project planning",
            org_id=None,
            team_id=None,
        )

    def test_leaves_new_chat_unnamed_for_automatic_naming(self):
        create_conversation(
            self.client,
            user_id="user-id",
            agent_id="agent-id",
            name="   ",
        )

        name = self.client.conversations.create.call_args.kwargs["name"]
        self.assertIsNone(name)


if __name__ == "__main__":
    unittest.main()
