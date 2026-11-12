from datetime import UTC, datetime
import unittest

from tools.run_whitebit_course_supervisor import completed_window, partition_counts, _compact


class CourseSupervisorTests(unittest.TestCase):
    def test_window_uses_only_completed_aligned_candles(self):
        start, end = completed_window(datetime(2026, 11, 12, 10, 7, tzinfo=UTC), 4000)
        self.assertEqual(end % 900, 0)
        self.assertLess(end, int(datetime(2026, 11, 12, 10, 7, tzinfo=UTC).timestamp()))
        self.assertEqual((end - start) // 900 + 1, 4000)

    def test_partitions_retain_substantial_locked_final(self):
        train, development, final = partition_counts(6000)
        self.assertEqual(train + development + final, 6000)
        self.assertGreaterEqual(train, 2000)
        self.assertGreaterEqual(development, 1000)
        self.assertGreaterEqual(final, 1000)

    def test_checkpoint_projection_cannot_grant_authority(self):
        value = _compact({"strategy_schema": "x", "train": {
            "market_count": 3, "closed_cycles": 1, "wins": 1, "losses": 0,
            "win_rate": "1", "net_pnl": "1", "fees": "1", "funding": "0"},
            "train_rejection_reasons": [], "development": None,
            "development_rejection_reasons": [], "final": None,
            "final_rejection_reasons": ["LOCKED"], "development_consumed": False,
            "final_consumed": False, "prospective_qualification": False,
            "result_sha256": "a" * 64})
        self.assertFalse(value["network_orders"])
        self.assertFalse(value["trading_authority"])
        self.assertFalse(value["automatic_promotion"])
        self.assertEqual(value["paid_model_calls"], 0)


if __name__ == "__main__":
    unittest.main()

