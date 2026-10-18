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
        self.assertEqual(a["conservative_uncertainty"]["status"],
                         "INSUFFICIENT_COMPLETED_EPISODES")
        self.assertEqual(a["missed_fill_metrics"]["execution_intents"], 0)
        self.assertIsNone(a["missed_fill_metrics"]["missed_fill_rate_fraction"])
        self.assertEqual(
            {row["regime"] for row in a["causal_regime_analysis"]["assignments"]},
            {"INSUFFICIENT_PRIOR_HISTORY"})
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

    def test_regimes_use_only_prior_opens_and_missed_fills_are_conservative(self):
        start = datetime(2024, 4, 1, tzinfo=timezone.utc)
        dates = [start + timedelta(hours=i) for i in range(9)]
        decisions = []
        prices = ["100", "101", "102", "101", "100", "99", "100", "101"]
        for index, price in enumerate(prices):
            decision = "BUY" if index == 2 else "REDUCE" if index == 5 else "HOLD"
            status = "FILLED" if index == 2 else "RISK_REJECTED" if index == 5 else "HOLD"
            decisions.append({
                "episode": index + 1, "equity": price, "cash": "0", "position": "1",
                "reconciled": True, "decision": decision, "status": status,
                "order_id": "order-3" if index == 2 else None,
                "fill_id": "fill-3" if index == 2 else None,
            })
        simulation = {"status": "PAUSED", "completed_episodes": 8, "cash": "0",
                      "position": "1", "decisions": decisions}
        report = holdout_report(
            simulation, execution_prices=[*prices, "999999"], execution_dates=dates,
            virtual_cash="100", slippage_bps="10")
        missed = report["missed_fill_metrics"]
        self.assertEqual((missed["execution_intents"], missed["reconciled_fills"],
                          missed["missed_fills"]), (2, 1, 1))
        self.assertEqual(missed["missed_fill_rate_fraction"], "0.5")
        self.assertEqual(missed["missed_fill_reasons"], {"RISK_REJECTED": 1})
        regimes = report["causal_regime_analysis"]
        self.assertEqual(regimes["assignments"][2]["regime_information_cut_utc"],
                         dates[1].isoformat())
        self.assertTrue(any(row["regime"].startswith("UP_")
                            for row in regimes["assignments"]))
        uncertainty = report["conservative_uncertainty"]
        self.assertEqual(uncertainty["status"], "ESTIMATED_SAMPLING_UNCERTAINTY_ONLY")
        self.assertEqual(uncertainty["samples"], 256)
        # A future price cannot change causal regime assignments or resampled bounds.
        changed_future = holdout_report(
            simulation, execution_prices=[*prices, "0.01"], execution_dates=dates,
            virtual_cash="100", slippage_bps="10")
        self.assertEqual(report, changed_future)

    def test_rejects_false_fill_evidence_and_noncausal_dates(self):
        start = datetime(2024, 5, 1, tzinfo=timezone.utc)
        simulation = {"status": "PAUSED", "completed_episodes": 2, "cash": "100",
                      "position": "0", "decisions": [
                          {"episode": 1, "equity": "100", "cash": "100", "position": "0",
                           "reconciled": True, "decision": "BUY", "status": "FILLED",
                           "order_id": None, "fill_id": None},
                          {"episode": 2, "equity": "100", "cash": "100", "position": "0",
                           "reconciled": True, "decision": "HOLD", "status": "HOLD"},
                      ]}
        with self.assertRaisesRegex(ValueError, "lacks canonical order/fill"):
            holdout_report(simulation, execution_prices=["100", "100"],
                           execution_dates=[start, start + timedelta(hours=1)],
                           virtual_cash="100", slippage_bps="10")
        simulation["decisions"][0].update(status="RISK_REJECTED")
        with self.assertRaisesRegex(ValueError, "strictly ordered UTC"):
            holdout_report(simulation, execution_prices=["100", "100"],
                           execution_dates=[start, start], virtual_cash="100",
                           slippage_bps="10")


if __name__ == "__main__":
    unittest.main()
