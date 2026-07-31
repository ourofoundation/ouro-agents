"""Tests for chat-mode prompt hygiene: plain task messages, cache-stable
history windows, working-memory dedup, and conversation-tool exclusion."""

import unittest

from ouro_agents.agent import _dedup_bullet_lines
from ouro_agents.modes.profiles import CHAT, ModeProfile
from ouro_agents.soul import build_prompt
from ouro_agents.tools.agent_base import PlainTaskStep
from ouro_agents.usage import inject_cache_control
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


class TestCatchAllMemoryScope(unittest.TestCase):
    def test_load_shared_prompt_context_remaps_nil_to_root(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from ouro_agents.agent import OuroAgent

        root_store = MagicMock()
        root_store.memory_name.return_value = "MEMORY:hermes"
        root_store.log_name.return_value = "LOG:hermes:2026-W31"
        root_store.read.side_effect = lambda name: {
            "MEMORY:hermes": "# Shared root memory\n",
            "LOG:hermes:2026-W31": "",
            "NOTES:hermes": "",
            "SHARED:memory": "# Shared root memory\n",
        }.get(name, "")

        team_store = MagicMock()
        team_store.memory_name.return_value = "MEMORY:hermes:all"
        team_store.log_name.return_value = "LOG:hermes:all:2026-W31"
        team_store.read.return_value = "# Should not load for catch-all\n"

        agent = OuroAgent.__new__(OuroAgent)
        agent.config = SimpleNamespace(
            agent=SimpleNamespace(name="hermes", workspace="/tmp"),
            memory=SimpleNamespace(rhythm="weekly"),
        )
        agent.doc_store = root_store
        agent.notes = ""
        agent.soul = ""
        agent._load_platform_context = lambda: ""
        agent._own_quests_index = lambda: ""
        agent.doc_store_for = MagicMock(return_value=team_store)
        # store_rhythm reads from doc_store; stub via a simple rhythm attr path
        from ouro_agents.memory import naming as naming_mod

        original_store_rhythm = naming_mod.store_rhythm
        naming_mod.store_rhythm = lambda _ds: "weekly"
        try:
            ctx = agent._load_shared_prompt_context(
                team_id="00000000-0000-0000-0000-000000000000"
            )
        finally:
            naming_mod.store_rhythm = original_store_rhythm

        agent.doc_store_for.assert_not_called()
        self.assertIn("Shared root memory", ctx["working_memory"])
        self.assertNotIn("Should not load for catch-all", ctx["working_memory"])


class TestChatHistoryGate(unittest.TestCase):
    def test_chat_history_gate_is_conversational(self):
        self.assertTrue(CHAT.conversational)
        self.assertFalse(hasattr(CHAT, "load_conversation_state"))
        self.assertFalse(hasattr(CHAT, "update_conversation_state"))
        self.assertNotIn("load_conversation_state", ModeProfile.model_fields)
        self.assertNotIn("update_conversation_state", ModeProfile.model_fields)


class TestChatToolExclusion(unittest.TestCase):
    def test_chat_profile_excludes_conversation_tools(self):
        self.assertIn("ouro:send_message", CHAT.excluded_tools)
        self.assertIn("ouro:list_conversations", CHAT.excluded_tools)
        self.assertIn("ouro:create_conversation", CHAT.excluded_tools)

    def test_send_message_prohibition_language_removed(self):
        self.assertNotIn("send_message", CHAT.output_format)
        self.assertNotIn("send_message", CHAT.conversation_id_annotation or "")


class TestConversationIdPlacement(unittest.TestCase):
    """The per-conversation id must live in dynamic context, not the static
    system prompt — otherwise every conversation gets a unique prefix and
    never shares a prompt cache entry."""

    def test_conversation_id_is_dynamic_not_static(self):
        conv_id = "abc-123-conv"
        system_prompt, dynamic_context = build_prompt(
            soul="Be precise.",
            notes="",
            skills="",
            profile=CHAT,
            chat_conversation_id=conv_id,
        )
        self.assertNotIn(conv_id, system_prompt)
        self.assertIn(conv_id, dynamic_context)

    def test_no_conversation_section_without_id(self):
        system_prompt, dynamic_context = build_prompt(
            soul="Be precise.",
            notes="",
            skills="",
            profile=CHAT,
            chat_conversation_id=None,
        )
        self.assertNotIn("Conversation id for this run", system_prompt)
        self.assertNotIn("Conversation id for this run", dynamic_context)


class TestInjectCacheControl(unittest.TestCase):
    def _messages(self):
        return [
            {"role": "system", "content": "long stable system prompt"},
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "thinking", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "tool result"},
        ]

    def _markers(self, messages):
        count = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                count += sum(
                    1 for b in content if isinstance(b, dict) and "cache_control" in b
                )
        return count

    def test_marks_system_and_last_message(self):
        messages = self._messages()
        inject_cache_control(messages, ttl="5m")
        # System prefix breakpoint.
        self.assertEqual(
            messages[0]["content"][0]["cache_control"], {"type": "ephemeral"}
        )
        # Advancing breakpoint on the most recent cacheable message.
        self.assertEqual(
            messages[-1]["content"][0]["cache_control"], {"type": "ephemeral"}
        )
        self.assertEqual(self._markers(messages), 2)

    def test_ttl_is_propagated(self):
        messages = self._messages()
        inject_cache_control(messages, ttl="1h")
        self.assertEqual(
            messages[0]["content"][0]["cache_control"],
            {"type": "ephemeral", "ttl": "1h"},
        )

    def test_idempotent_across_calls(self):
        # The agent loop rebuilds messages each step; re-injecting must not
        # accumulate markers beyond the two breakpoints (providers cap at 4).
        messages = self._messages()
        inject_cache_control(messages, ttl="5m")
        inject_cache_control(messages, ttl="5m")
        self.assertEqual(self._markers(messages), 2)

    def test_skips_message_without_text(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        ]
        inject_cache_control(messages, ttl="5m")
        # System gets the prefix marker; the tool-call-only turn is skipped, so
        # the advancing breakpoint falls back onto the system message.
        self.assertEqual(self._markers(messages), 1)
        self.assertIsNone(messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
