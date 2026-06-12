"""Tests for chat-mode prompt hygiene: plain task messages, cache-stable
history windows, working-memory dedup, and conversation-tool exclusion."""

import unittest

from ouro_agents.agent import _dedup_bullet_lines
from ouro_agents.memory.conversation_state import ConversationState
from ouro_agents.modes.framing import CHAT_REPLY_OUTPUT
from ouro_agents.modes.profiles import CHAT, CHAT_REPLY
from ouro_agents.tools.agent_base import PlainTaskStep
from ouro_agents.utils.conversation import (
    HISTORY_WINDOW_MIN,
    HISTORY_WINDOW_STEP,
    build_history_steps,
    select_history_window,
)


class TestPlainTaskStep(unittest.TestCase):
    def test_renders_user_content_verbatim(self):
        messages = PlainTaskStep(task="hey there!").to_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content[0]["text"], "hey there!")

    def test_history_user_turns_have_no_new_task_prefix(self):
        turns = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi!"},
        ]
        steps = build_history_steps(turns)
        user_messages = steps[0].to_messages()
        self.assertNotIn("New task", user_messages[0].content[0]["text"])
        self.assertEqual(user_messages[0].content[0]["text"], "hello")


class TestHistoryWindow(unittest.TestCase):
    def _turns(self, n: int) -> list[dict]:
        return [{"role": "user", "content": f"turn {i}"} for i in range(n)]

    def test_short_conversations_kept_whole(self):
        turns = self._turns(HISTORY_WINDOW_MIN)
        self.assertEqual(select_history_window(turns), turns)

    def test_anchor_is_stable_between_steps(self):
        # Window start must not move while the conversation grows within a step.
        lo = select_history_window(self._turns(HISTORY_WINDOW_MIN + 1))
        hi = select_history_window(
            self._turns(HISTORY_WINDOW_MIN + HISTORY_WINDOW_STEP - 1)
        )
        self.assertEqual(lo[0]["content"], "turn 0")
        self.assertEqual(hi[0]["content"], "turn 0")

    def test_anchor_moves_once_per_step(self):
        n = HISTORY_WINDOW_MIN + HISTORY_WINDOW_STEP
        window = select_history_window(self._turns(n))
        self.assertEqual(window[0]["content"], f"turn {HISTORY_WINDOW_STEP}")
        self.assertEqual(len(window), HISTORY_WINDOW_MIN)

    def test_window_never_shrinks_below_minimum(self):
        for n in range(1, 50):
            window = select_history_window(self._turns(n))
            self.assertGreaterEqual(len(window), min(n, HISTORY_WINDOW_MIN))


class TestWorkingMemoryDedup(unittest.TestCase):
    def test_duplicate_bullets_dropped_keeping_first(self):
        text = (
            "## Facts\n"
            "- fact one\n"
            "- fact two\n"
            "- fact one\n"
            "\n"
            "## Preferences\n"
            "- fact one\n"
            "- pref one\n"
        )
        deduped = _dedup_bullet_lines(text)
        self.assertEqual(deduped.count("- fact one"), 1)
        self.assertIn("- fact two", deduped)
        self.assertIn("- pref one", deduped)
        self.assertIn("## Facts", deduped)
        self.assertIn("## Preferences", deduped)

    def test_non_bullet_lines_untouched(self):
        text = "intro\n\nintro\n- a\n- a\n"
        deduped = _dedup_bullet_lines(text)
        self.assertEqual(deduped.count("intro"), 2)
        self.assertEqual(deduped.count("- a"), 1)


class TestConversationStateSummary(unittest.TestCase):
    def _state(self) -> ConversationState:
        return ConversationState(
            current_topic="testing",
            conversation_summary="We discussed many things.",
        )

    def test_summary_included_by_default(self):
        self.assertIn("Conversation so far", self._state().format_for_prompt())

    def test_summary_omitted_when_history_is_verbatim(self):
        rendered = self._state().format_for_prompt(include_summary=False)
        self.assertNotIn("Conversation so far", rendered)
        self.assertIn("Topic: testing", rendered)


class TestChatToolExclusion(unittest.TestCase):
    def test_chat_profiles_exclude_conversation_tools(self):
        for profile in (CHAT, CHAT_REPLY):
            self.assertIn("ouro:send_message", profile.excluded_tools)
            self.assertIn("ouro:list_conversations", profile.excluded_tools)
            self.assertIn("ouro:create_conversation", profile.excluded_tools)

    def test_send_message_prohibition_language_removed(self):
        self.assertNotIn("send_message", CHAT_REPLY_OUTPUT)
        for profile in (CHAT, CHAT_REPLY):
            self.assertNotIn(
                "send_message", profile.conversation_id_annotation or ""
            )


if __name__ == "__main__":
    unittest.main()
