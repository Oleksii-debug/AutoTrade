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
                   {"episode": 1, "equity": "200", "cash": "200", "position": "0", "reconciled": True},
                   {"episode": 2, "equity": "200", "cash": "200", "position": "0", "reconciled": True},
               ]}
        a = holdout_report(sim, execution_prices=["100", "100", "999"],
                           execution_dates=dates, virtual_cash="200", slippage_bps="10")
        b = holdout_report(sim, execution_prices=["100", "100", "1"],
                           execution_dates=dates, virtual_cash="200", slippage_bps="10")
        self.assertEqual(a, b)
        self.assertEqual(a["through_episode"], 2)
        self.assertEqual(len(a["agent_equity_curve"]), 2)
        self.assertEqual(a["agent_gross_mark_turnover_proxy_fraction"], "0")
        self.assertIsNone(a["agent_hourly_sharpe_zero_cash_rate_unannualized"])
        bad = dict(sim, decisions=[sim["decisions"][0],
                                   {"episode": 2, "equity": "201", "cash": "200", "position": "0", "reconciled": True}])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            holdout_report(bad, execution_prices=["100", "100", "999"],
                           execution_dates=dates, virtual_cash="200", slippage_bps="10")

    def test_reconciled_curve_and_hourly_risk_must_match_completed_prefix(self):
        start = datetime(2024, 3, 1, tzinfo=timezone.utc)
        dates = [start + timedelta(hours=i) for i in range(4)]
        sim = {"status": "PAUSED", "completed_episodes": 3, "cash": "100",
               "position": "1", "decisions": [
                   {"episode": 1, "equity": "200", "cash": "100", "position": "1", "reconciled": True},
                   {"episode": 2, "equity": "210", "cash": "100", "position": "1", "reconciled": True},
                   {"episode": 3, "equity": "200", "cash": "100", "position": "1", "reconciled": True},
               ]}
        report = holdout_report(sim, execution_prices=["100", "110", "100", "999"],
                                execution_dates=dates, virtual_cash="200", slippage_bps="10")
        self.assertEqual([row["mark_equity_usd"] for row in report["agent_equity_curve"]],
                         ["200", "210", "200"])
        self.assertEqual(report["agent_max_mark_drawdown_fraction"],
                         "0.04761904761904761904761904762")
        self.assertEqual(report["agent_gross_mark_turnover_proxy_fraction"], "0.5")
        self.assertIsNotNone(report["agent_hourly_return_sample_volatility_fraction"])
        self.assertLess(float(report["agent_hourly_sharpe_zero_cash_rate_unannualized"]), 1)
        corrupted = dict(sim, decisions=[dict(row) for row in sim["decisions"]])
        corrupted["decisions"][1]["cash"] = "109"
        with self.assertRaisesRegex(ValueError, "curve conflicts"):
            holdout_report(corrupted, execution_prices=["100", "110", "100", "999"],
                           execution_dates=dates, virtual_cash="200", slippage_bps="10")


if __name__ == "__main__":
    unittest.main()
