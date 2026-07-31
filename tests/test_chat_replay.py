"""Tests for the chat replay harness and history-coverage regressions.

The fixture is a real exported transcript of the conversation where Apollo's
scouting origin was lost behind a history-window cliff (019fb5df). These tests
pin the harness itself — that it reconstructs turns, reproduces the cliff, and
shows that append-only history keeps the origin visible.
"""

import unittest
from pathlib import Path

from ouro_agents.chat_replay import (
    ReplayTurn,
    first_turn_drop,
    format_trajectory,
    load_transcript,
    load_transcript_from_run_log,
    replay,
    save_transcript,
    strip_task_wrapper,
    turn_visible_at,
)
from ouro_agents.utils.conversation import select_history_window

FIXTURE = Path(__file__).parent / "fixtures" / "chat" / "goal_drift_019fb5df.json"


def _identity_window(turns: list[dict]) -> list[dict]:
    return turns


class TestTaskWrapper(unittest.TestCase):
    def test_strips_webhook_envelope(self):
        task = (
            "New conversation message from matt (conversation_id: abc-123).\n\n"
            "What should we add to Ouro?"
        )
        self.assertEqual(strip_task_wrapper(task), "What should we add to Ouro?")

    def test_plain_message_untouched(self):
        self.assertEqual(strip_task_wrapper("just a message"), "just a message")

    def test_handles_empty(self):
        self.assertEqual(strip_task_wrapper(""), "")


class TestTranscriptIO(unittest.TestCase):
    def test_round_trips_through_json(self):
        import tempfile

        turns = [
            ReplayTurn(index=0, user="hi", assistant="hello"),
            ReplayTurn(index=1, user="bye", assistant="see ya"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            save_transcript(path, turns)
            loaded = load_transcript(path)
        self.assertEqual([t.user for t in loaded], ["hi", "bye"])
        self.assertEqual([t.assistant for t in loaded], ["hello", "see ya"])

    def test_builds_transcript_from_run_log_rows(self):
        class FakeStore:
            def query_runs(self, **kwargs):
                return [
                    {
                        "run_id": "r2",
                        "started_at": "2026-07-31T02:00:00Z",
                        "task": "New conversation message from matt "
                        "(conversation_id: c).\n\nsecond",
                        "result": "reply two",
                    },
                    {
                        "run_id": "r1",
                        "started_at": "2026-07-31T01:00:00Z",
                        "task": "New conversation message from matt "
                        "(conversation_id: c).\n\nfirst",
                        "result": "reply one",
                    },
                ]

        turns = load_transcript_from_run_log(FakeStore(), "c")
        self.assertEqual([t.user for t in turns], ["first", "second"])
        self.assertEqual([t.index for t in turns], [0, 1])

    def test_skips_runs_with_no_user_message(self):
        class FakeStore:
            def query_runs(self, **kwargs):
                return [
                    {"run_id": "r1", "started_at": "1", "task": "", "result": "x"},
                    {"run_id": "r2", "started_at": "2", "task": "real", "result": "y"},
                ]

        turns = load_transcript_from_run_log(FakeStore(), "c")
        self.assertEqual([t.user for t in turns], ["real"])


class TestHistoryCoverageFixture(unittest.TestCase):
    """The real incident transcript, replayed for history visibility."""

    @classmethod
    def setUpClass(cls):
        cls.turns = load_transcript(FIXTURE)

    def test_fixture_captures_the_full_conversation(self):
        self.assertEqual(len(self.turns), 11)
        self.assertEqual(self.turns[0].user, "Hey!")
        self.assertIn("I mean in our research right now", self.turns[7].user)
        self.assertIn("Lets keep researching", self.turns[9].user)

    def test_failed_turns_are_present_with_empty_replies(self):
        # The two BlockingIOError turns and the interrupted one left the user
        # with silence; a replay must preserve them or it misrepresents the gap.
        empty = [t.index for t in self.turns if not t.assistant]
        self.assertEqual(empty, [4, 5, 9])

    def test_current_window_drops_the_scouting_origin(self):
        steps = replay(self.turns, window_fn=select_history_window)
        # Turn 2 is the Ouro scouting origin. The cliff fires around
        # "Summarize this chat." and the origin is gone by "lets keep researching".
        self.assertEqual(steps[7].dropped_history_turns, 0)
        self.assertEqual(steps[8].dropped_history_turns, 8)
        self.assertFalse(turn_visible_at(steps, source_index=2, at_index=9))
        drop = first_turn_drop(steps, 2)
        self.assertIsNotNone(drop)
        self.assertEqual(drop.turn.index, 8)

    def test_append_only_never_drops_turns(self):
        steps = replay(self.turns, window_fn=_identity_window)
        self.assertTrue(all(s.dropped_history_turns == 0 for s in steps))
        self.assertTrue(turn_visible_at(steps, source_index=2, at_index=9))

    def test_whole_conversation_is_small(self):
        # The conversation that broke never approached a context limit, so the
        # window dropped context for no budget reason at all.
        steps = replay(self.turns, window_fn=_identity_window)
        self.assertLess(steps[-1].est_history_tokens, 8000)

    def test_trajectory_report_flags_the_drop(self):
        steps = replay(self.turns, window_fn=select_history_window)
        report = format_trajectory(steps)
        self.assertIn("DROPPED 8", report)
        self.assertIn("visible user turns:", report)


class TestReplayMechanics(unittest.TestCase):
    def test_history_grows_by_two_entries_per_turn(self):
        turns = [ReplayTurn(index=i, user=f"u{i}", assistant=f"a{i}") for i in range(4)]
        steps = replay(turns)
        self.assertEqual([s.turns_available for s in steps], [0, 2, 4, 6])

    def test_identity_window_keeps_all_prior_user_turns_visible(self):
        turns = [ReplayTurn(index=i, user=f"u{i}", assistant=f"a{i}") for i in range(4)]
        steps = replay(turns, window_fn=_identity_window)
        self.assertEqual(steps[3].visible_turn_indices, [0, 1, 2])
        self.assertTrue(turn_visible_at(steps, source_index=0, at_index=3))

    def test_first_turn_drop_reports_the_cliff(self):
        turns = [ReplayTurn(index=i, user=f"u{i}", assistant=f"a{i}") for i in range(5)]

        def keep_last_two(history: list[dict]) -> list[dict]:
            return history[-2:] if len(history) > 2 else history

        steps = replay(turns, window_fn=keep_last_two)
        drop = first_turn_drop(steps, 0)
        self.assertIsNotNone(drop)
        self.assertEqual(drop.turn.index, 2)


if __name__ == "__main__":
    unittest.main()
