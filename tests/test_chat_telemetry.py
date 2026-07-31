"""Tests for per-turn chat telemetry: prompt measurement and run-log storage."""

import tempfile
import unittest
from pathlib import Path

from ouro_agents.chat_telemetry import (
    apply_usage,
    build_chat_turn_record,
    estimate_tokens,
    estimate_turn_tokens,
    format_chat_turn,
)
from ouro_agents.run_log import ChatTurnRecord, RunLogStore


class _FakeUsage:
    model_id = "test/model"
    input_tokens = 10000
    cached_input_tokens = 7500
    cache_write_tokens = 200
    output_tokens = 500
    num_api_calls = 3
    cost_usd = 0.0123


def _turns(n: int) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": "x" * 400,
        }
        for i in range(n)
    ]


class TestEstimation(unittest.TestCase):
    def test_empty_text_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_scales_with_length(self):
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_turn_estimate_includes_tool_summaries(self):
        plain = [{"role": "assistant", "content": "hello"}]
        with_tools = [
            {
                "role": "assistant",
                "content": "hello",
                "tool_summary": [{"name": "search_assets", "args": {"q": "x" * 200}}],
            }
        ]
        self.assertGreater(
            estimate_turn_tokens(with_tools), estimate_turn_tokens(plain)
        )


class TestRecordBuilding(unittest.TestCase):
    def _build(self, *, all_turns, injected) -> ChatTurnRecord:
        return build_chat_turn_record(
            run_id="run-1",
            conversation_id="conv-1",
            agent_name="apollo",
            model="test/model",
            all_turns=all_turns,
            injected_turns=injected,
            history_steps=len(injected),
            system_prompt="s" * 4000,
            dynamic_context="d" * 800,
            task="t" * 200,
        )

    def test_measures_each_prompt_section(self):
        record = self._build(all_turns=_turns(4), injected=_turns(4))
        self.assertEqual(record.est_system_tokens, 1000)
        self.assertEqual(record.est_dynamic_tokens, 200)
        self.assertEqual(record.est_task_tokens, 50)
        self.assertEqual(
            record.est_prompt_tokens,
            record.est_system_tokens
            + record.est_dynamic_tokens
            + record.est_history_tokens
            + record.est_task_tokens,
        )

    def test_records_dropped_turns(self):
        record = self._build(all_turns=_turns(20), injected=_turns(12))
        self.assertEqual(record.turns_fetched, 20)
        self.assertEqual(record.turns_injected, 12)
        self.assertEqual(record.dropped_oldest_turns, 8)
        self.assertFalse(record.history_covers_conversation)

    def test_full_history_marks_coverage(self):
        record = self._build(all_turns=_turns(6), injected=_turns(6))
        self.assertEqual(record.dropped_oldest_turns, 0)
        self.assertTrue(record.history_covers_conversation)


class TestUsageApplication(unittest.TestCase):
    def test_fills_provider_accounting(self):
        record = ChatTurnRecord(run_id="r", conversation_id="c")
        apply_usage(record, _FakeUsage())
        self.assertEqual(record.input_tokens, 10000)
        self.assertEqual(record.cached_input_tokens, 7500)
        self.assertAlmostEqual(record.cache_hit_ratio, 0.75)

    def test_missing_usage_is_a_noop(self):
        record = ChatTurnRecord(run_id="r", conversation_id="c")
        apply_usage(record, None)
        self.assertEqual(record.input_tokens, 0)
        self.assertEqual(record.cache_hit_ratio, 0.0)

    def test_summary_line_mentions_cache_and_history(self):
        record = ChatTurnRecord(
            run_id="r", conversation_id="c", turns_injected=8, turns_fetched=20
        )
        apply_usage(record, _FakeUsage())
        line = format_chat_turn(record)
        self.assertIn("turns=8/20", line)
        self.assertIn("cached=7500 (75%)", line)


class TestRunLogStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunLogStore(Path(self._tmp.name) / "runs.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _record(self, run_id="run-1") -> ChatTurnRecord:
        return ChatTurnRecord(
            run_id=run_id,
            conversation_id="conv-1",
            agent_name="apollo",
            turns_fetched=20,
            turns_injected=12,
            dropped_oldest_turns=8,
            est_prompt_tokens=4200,
        )

    def test_round_trips_a_turn(self):
        self.store.record_chat_turn(self._record())
        rows = self.store.query_chat_turns(conversation_id="conv-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turns_injected"], 12)
        self.assertEqual(rows[0]["dropped_oldest_turns"], 8)
        self.assertEqual(rows[0]["est_prompt_tokens"], 4200)

    def test_usage_update_preserves_history_accounting(self):
        record = self._record()
        self.store.record_chat_turn(record)
        apply_usage(record, _FakeUsage())
        self.store.update_chat_turn_usage(record)

        row = self.store.query_chat_turns(conversation_id="conv-1")[0]
        self.assertEqual(row["turns_injected"], 12)
        self.assertEqual(row["dropped_oldest_turns"], 8)
        self.assertEqual(row["input_tokens"], 10000)
        self.assertAlmostEqual(row["cache_hit_ratio"], 0.75)

    def test_turns_are_returned_oldest_first_when_asked(self):
        for i in range(3):
            rec = self._record(run_id=f"run-{i}")
            rec.created_at = f"2026-07-31T0{i}:00:00+00:00"
            self.store.record_chat_turn(rec)
        rows = self.store.query_chat_turns(
            conversation_id="conv-1", newest_first=False
        )
        self.assertEqual([r["run_id"] for r in rows], ["run-0", "run-1", "run-2"])

    def test_disabled_store_is_inert(self):
        store = RunLogStore(Path(self._tmp.name) / "nope.db", enabled=False)
        store.record_chat_turn(self._record())
        self.assertEqual(store.query_chat_turns(), [])

    def test_readonly_store_over_missing_db_is_empty(self):
        store = RunLogStore(Path(self._tmp.name) / "missing.db", readonly=True)
        self.assertEqual(store.query_chat_turns(), [])


if __name__ == "__main__":
    unittest.main()
