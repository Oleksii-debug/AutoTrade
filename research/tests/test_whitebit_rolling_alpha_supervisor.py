from datetime import UTC, datetime
import unittest

from tools.run_whitebit_rolling_alpha_supervisor import (
    compact_evaluation,
    completed_window,
)


class RollingAlphaSupervisorTests(unittest.TestCase):
    def test_completed_window_excludes_current_incomplete_hour(self):
        start, end = completed_window(datetime(2026, 11, 12, 0, 37, tzinfo=UTC), 160)
        self.assertEqual(end, int(datetime(2026, 11, 11, 23, tzinfo=UTC).timestamp()))
        self.assertEqual((end-start)//3600+1, 160)

    def test_compact_evaluation_preserves_costed_long_short_evidence_only(self):
        value = compact_evaluation({
            "market": "SOL_USDT", "evaluation_id": "abc",
            "dataset_identity_sha256": "d"*64,
            "models": {"XGBOOST": {
                "signal_count": 2, "executed_trade_count": 2,
                "long_trade_count": 1, "short_trade_count": 1,
                "winning_trade_count": 1, "net_pnl_after_costs_quote": "1.2",
                "net_return_ratio": "0.0012", "maximum_drawdown_ratio": "0.01",
                "research_gate_passed": False, "rejection_reasons": ["TEST"],
                "calibration_policy_selection": {
                    "selected_alpha": "0.3", "selection_passed": True,
                },
                "private": "excluded",
            }},
        })
        self.assertEqual(value["models"]["XGBOOST"]["short_trade_count"], 1)
        self.assertEqual(value["models"]["XGBOOST"]["calibration_selected_alpha"], "0.3")
        self.assertTrue(value["models"]["XGBOOST"]["calibration_selection_passed"])
        self.assertNotIn("private", value["models"]["XGBOOST"])
        self.assertFalse(value["live_authority"])
        self.assertFalse(value["financial_operation_executed"])


if __name__ == "__main__":
    unittest.main()

