"""Tests for chat history compaction, failed-turn markers, and plans pointer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from ouro_agents.chat_compaction import (
    CompactionRecord,
    build_injectable_history,
    compact_history,
    estimate_chat_prompt_tokens,
    find_watermark_index,
    load_compaction,
    run_compaction_locked,
    save_compaction,
    should_compact,
    turn_stable_id,
)
from ouro_agents.modes.framing import CHAT_FRAMING
from ouro_agents.modes.planning import (
    format_quests_index_for_prompt,
    format_quests_index_pointer,
)
from ouro_agents.utils.conversation import (
    EMPTY_ASSISTANT_REPLY_MARKER,
    UNANSWERED_USER_MARKER,
    build_history_steps,
)


def _turns(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append(
            {
                "role": "user",
                "content": f"user message {i}",
                "turn_id": f"u{i}",
                "timestamp": f"2026-01-01T00:00:{i:02d}Z",
            }
        )
        out.append(
            {
                "role": "assistant",
                "content": f"assistant reply {i}",
                "turn_id": f"a{i}",
                "timestamp": f"2026-01-01T00:01:{i:02d}Z",
            }
        )
    return out


class _FakeModel:
    def __init__(self, text: str = "Continuity summary of earlier chat."):
        self.text = text
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        return MagicMock(content=self.text)


class TestShouldCompact(unittest.TestCase):
    def test_thresholds(self):
        self.assertIsNone(
            should_compact(
                50_000,
                context_tokens=100_000,
                soft_fraction=0.6,
                hard_fraction=0.85,
            )
        )
        self.assertEqual(
            should_compact(
                65_000,
                context_tokens=100_000,
                soft_fraction=0.6,
                hard_fraction=0.85,
            ),
            "soft",
        )
        self.assertEqual(
            should_compact(
                90_000,
                context_tokens=100_000,
                soft_fraction=0.6,
                hard_fraction=0.85,
            ),
            "hard",
        )


class TestWatermarkRebuild(unittest.TestCase):
    def test_rebuild_keeps_tail_after_watermark(self):
        turns = _turns(5)
        # Compact through the 3rd assistant reply (index 5).
        wm = turn_stable_id(turns[5], 5)
        record = CompactionRecord(
            watermark_turn_id=wm,
            summary="Earlier: discussed scouting for Ouro services.",
            reason="soft",
            turns_compacted=6,
        )
        built = build_injectable_history(turns, compaction=record)
        self.assertTrue(built.compacted)
        self.assertEqual(built.summary, record.summary)
        # Tail starts at turn index 6 (user message 3).
        self.assertEqual(built.injected_turns[0]["turn_id"], "u3")
        self.assertEqual(len(built.injected_turns), 4)  # u3,a3,u4,a4

    def test_summary_step_then_tail_is_cache_stable(self):
        turns = _turns(4)
        wm = turn_stable_id(turns[3], 3)
        record = CompactionRecord(
            watermark_turn_id=wm,
            summary="Prior work on Ouro scouting.",
            reason="soft",
        )
        built = build_injectable_history(turns, compaction=record)
        steps_a = build_history_steps(built.injected_turns, summary=built.summary)
        # Growing the tail must not change the summary prefix steps.
        turns2 = turns + [
            {"role": "user", "content": "new", "turn_id": "u99"},
            {"role": "assistant", "content": "ok", "turn_id": "a99"},
        ]
        built2 = build_injectable_history(turns2, compaction=record)
        steps_b = build_history_steps(built2.injected_turns, summary=built2.summary)
        # First two steps are the synthetic summary exchange.
        self.assertEqual(
            steps_a[0].to_messages()[0].content[0]["text"],
            steps_b[0].to_messages()[0].content[0]["text"],
        )
        self.assertEqual(steps_a[1].model_output, steps_b[1].model_output)
        self.assertEqual(steps_a[1].model_output, "Prior work on Ouro scouting.")
        self.assertGreater(len(steps_b), len(steps_a))

    def test_missing_watermark_falls_back_to_full_fetched_as_tail(self):
        turns = _turns(2)
        record = CompactionRecord(
            watermark_turn_id="missing-id",
            summary="Old summary covering dropped turns.",
            reason="soft",
        )
        built = build_injectable_history(turns, compaction=record)
        self.assertEqual(built.injected_turns, turns)
        self.assertTrue(built.compacted)


class TestCompactHistory(unittest.TestCase):
    def test_persists_record_and_does_not_invent_goals_in_prompt(self):
        # The system prompt forbids inventing goals; we just check the call shape
        # and that the returned record is usable.
        model = _FakeModel(
            "User asked to scout non-MLIP services for Ouro. "
            "Open thread: whether to continue scouting."
        )
        turns = _turns(6)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            record = run_compaction_locked(
                workspace,
                "conv-1",
                turns,
                model,
                reason="soft",
                keep_recent=4,
                model_id="utility/test",
            )
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.reason, "soft")
            self.assertEqual(model.calls, 1)
            # keep_recent=4 means last 4 turns stay out; 12-4=8 folded.
            self.assertEqual(record.turns_compacted, 8)
            loaded = load_compaction(workspace, "conv-1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.summary, record.summary)
            built = build_injectable_history(turns, compaction=loaded)
            self.assertEqual(len(built.injected_turns), 4)
            self.assertNotIn("Active goals", loaded.summary)

    def test_keep_recent_skips_when_too_short(self):
        model = _FakeModel()
        turns = _turns(1)
        record = compact_history(
            turns, model, reason="soft", keep_recent=4, model_id="t"
        )
        self.assertIsNone(record)
        self.assertEqual(model.calls, 0)


class TestFailedTurnMarkers(unittest.TestCase):
    def test_unpaired_user_gets_unanswered_marker(self):
        turns = [
            {"role": "user", "content": "Please, not more MLIPs"},
            {"role": "user", "content": "Hear that?"},
            {"role": "user", "content": "Hello?"},
            {
                "role": "assistant",
                "content": "Loud and clear: no more MLIPs.",
            },
        ]
        steps = build_history_steps(turns)
        # user + marker, user + marker, user + assistant
        outputs = [
            s.model_output
            for s in steps
            if getattr(s, "model_output", None) is not None
        ]
        self.assertEqual(outputs.count(UNANSWERED_USER_MARKER), 2)
        self.assertIn("Loud and clear", outputs[-1])

    def test_empty_assistant_gets_empty_marker(self):
        turns = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": ""},
        ]
        steps = build_history_steps(turns)
        self.assertEqual(steps[1].model_output, EMPTY_ASSISTANT_REPLY_MARKER)

    def test_interrupted_flag_uses_prefix_when_empty(self):
        from ouro_agents.utils.conversation import INTERRUPTED_REPLY_PREFIX

        turns = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "interrupted": True},
        ]
        steps = build_history_steps(turns)
        self.assertEqual(steps[1].model_output, INTERRUPTED_REPLY_PREFIX)


class TestPlansPointer(unittest.TestCase):
    def test_pointer_is_one_line_without_quest_titles(self):
        quests = [
            {"id": "q1", "name": "Cu2Sb Validation Gate", "team_id": "t1"},
            {"id": "q2", "name": "GGen Heusler Calibration", "team_id": "t1"},
        ]
        pointer = format_quests_index_pointer(quests)
        self.assertIn("2 active plan quests", pointer)
        self.assertNotIn("Cu2Sb", pointer)
        self.assertNotIn("GGen", pointer)
        full = format_quests_index_for_prompt(quests)
        self.assertIn("Cu2Sb", full)

    def test_pointer_empty_when_no_quests(self):
        self.assertEqual(format_quests_index_pointer([]), "")


class TestChatFraming(unittest.TestCase):
    def test_commit_and_continue_guidance_present(self):
        self.assertIn("commit-and-continue", CHAT_FRAMING)
        self.assertIn("Reading you as X rather than Y", CHAT_FRAMING)


class TestEstimateIncludesSummary(unittest.TestCase):
    def test_summary_adds_tokens(self):
        turns = _turns(1)
        without = estimate_chat_prompt_tokens(
            system_prompt="sys",
            dynamic_context="dyn",
            task="task",
            injected_turns=turns,
            summary="",
        )
        with_sum = estimate_chat_prompt_tokens(
            system_prompt="sys",
            dynamic_context="dyn",
            task="task",
            injected_turns=turns,
            summary="x" * 400,
        )
        self.assertGreater(with_sum, without)


class TestFindWatermark(unittest.TestCase):
    def test_finds_by_turn_id(self):
        turns = _turns(2)
        idx = find_watermark_index(turns, "a0")
        self.assertEqual(idx, 1)


if __name__ == "__main__":
    unittest.main()
