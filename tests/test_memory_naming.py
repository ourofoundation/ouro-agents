import unittest
from datetime import datetime

from ouro_agents.memory.naming import log_entry_timestamp


class LogEntryTimestampTests(unittest.TestCase):
    def test_daily_uses_time_only(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("daily", when), "14:30")

    def test_weekly_includes_date(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("weekly", when), "2026-06-09 14:30")

    def test_biweekly_includes_date(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("biweekly", when), "2026-06-09 14:30")

    def test_unknown_rhythm_defaults_to_daily(self):
        when = datetime(2026, 6, 9, 14, 30)
        self.assertEqual(log_entry_timestamp("monthly", when), "14:30")


if __name__ == "__main__":
    unittest.main()
