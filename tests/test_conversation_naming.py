import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ouro_agents.conversation_naming import (
    generate_conversation_name,
    name_conversation_if_needed,
)


class TestConversationNaming(unittest.TestCase):
    def test_generates_and_cleans_title(self):
        model = Mock(return_value=SimpleNamespace(content='"Title: Diagnose email threading"'))

        title = generate_conversation_name(
            model,
            "Why are unnamed email threads showing null?",
            "The nullable conversation name is interpolated into the subject.",
        )

        self.assertEqual(title, "Diagnose email threading")
        messages = model.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("unnamed email threads", messages[1]["content"])

    def test_names_an_unnamed_conversation(self):
        conversations = Mock()
        conversations.retrieve.return_value = SimpleNamespace(name=None)
        client = SimpleNamespace(conversations=conversations)
        model = Mock(return_value=SimpleNamespace(content="Fix unnamed email threads"))
        model_factory = Mock(return_value=model)

        title = name_conversation_if_needed(
            client,
            "conversation-id",
            "Fix the null email subject",
            "I'll add a participant-name fallback.",
            model_factory,
        )

        self.assertEqual(title, "Fix unnamed email threads")
        conversations.update.assert_called_once_with(
            "conversation-id",
            name="Fix unnamed email threads",
        )

    def test_preserves_an_existing_name_without_calling_the_model(self):
        conversations = Mock()
        conversations.retrieve.return_value = {"name": "Existing title"}
        client = SimpleNamespace(conversations=conversations)
        model_factory = Mock()

        title = name_conversation_if_needed(
            client,
            "conversation-id",
            "User message",
            "Assistant response",
            model_factory,
        )

        self.assertIsNone(title)
        model_factory.assert_not_called()
        conversations.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
