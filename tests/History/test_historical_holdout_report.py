"""Comparative research reports never read a paused run's future prices."""
from datetime import datetime, timedelta, timezone
import unittest

from tools.historical_holdout_report import holdout_report


class HistoricalHoldoutReportTests(unittest.TestCase):
    def test_paused_comparison_ignores_future_prices_and_rejects_fake_equity(self):
        start = datetime(2024, 2, 1, tzinfo=timezone.utc)
        dates = [start + timedelta(hours=i) for i in range(3)]
        sim = {"status": "PAUSED", "completed_episodes": 2, "cash": "200",
               "position": "0", "decisions": [
                   {"episode": 1, "equity": "200", "reconciled": True},
                   {"episode": 2, "equity": "200", "reconciled": True},
               ]}
        a = holdout_report(sim, execution_prices=["100", "100", "999"],
                           execution_dates=dates, virtual_cash="200", slippage_bps="10")
        b = holdout_report(sim, execution_prices=["100", "100", "1"],
                           execution_dates=dates, virtual_cash="200", slippage_bps="10")
        self.assertEqual(a, b)
        self.assertEqual(a["through_episode"], 2)
        bad = dict(sim, decisions=[sim["decisions"][0],
                                   {"episode": 2, "equity": "201", "reconciled": True}])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            holdout_report(bad, execution_prices=["100", "100", "999"],
                           execution_dates=dates, virtual_cash="200", slippage_bps="10")


if __name__ == "__main__":
    unittest.main()
