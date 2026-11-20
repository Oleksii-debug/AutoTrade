import unittest
from datetime import UTC, datetime
from decimal import Decimal as D
from channel import resting_exit
from live import apply, buy, initial


class SwingTests(unittest.TestCase):
    def test_gap_and_ambiguous_hour(self):
        self.assertEqual(
            resting_exit((101, 115, 85, 106, 2200), 110, 92, 0.0015), (92, "stop")
        )
        self.assertEqual(
            resting_exit((85, 115, 84, 106, 2200), 110, 92, 0.0015), (85, "stop")
        )
        self.assertIsNone(resting_exit((100, 110.01, 99, 101, 3), 110, 92, 0.0015))

    def test_depth_and_full_cycle(self):
        book = dict(asks=[("10", "50"), ("11", "100")], bids=[("9.95", "200")])
        self.assertIsNone(buy([("10", "1")], D(1000)))
        at = datetime(2026, 11, 21, 1, tzinfo=UTC)
        s, action = apply(initial(), "wait10", book, dict(drawdown=0.12), True, at)
        self.assertEqual(action, "BUY_SIMULATED")
        self.assertGreater(D(s["paid"]), D(10))
        self.assertLess(D(s["last_mark"]), 1000)
        higher = dict(book, bids=[("14", "200")])
        s, action = apply(s, "wait10", higher, None, False, at)
        self.assertEqual(action, "SELL_TARGET")
        self.assertEqual(s["cycles"], 1)
        self.assertGreater(D(s["realized_usdt"]), 0)
        s, action = apply(s, "wait10", book, dict(drawdown=0.12), True, at)
        self.assertEqual(action, "HOLD")
        s, _ = apply(s, "wait10", book, dict(drawdown=0.01), True, at)
        self.assertTrue(s["armed"])

    def test_no_entry_without_daily_signal(self):
        s, action = apply(initial(), "channel", {}, None, False, datetime.now(UTC))
        self.assertEqual(action, "OBSERVE")
        self.assertEqual(s["cash"], "1000")


if __name__ == "__main__":
    unittest.main()
