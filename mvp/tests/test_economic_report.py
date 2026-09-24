from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.economics import build_economic_report
from mvp.autotrade_mvp.pipeline import run_multi_episode, run_vertical_slice


class EconomicReportTests(unittest.TestCase):
    def test_closed_round_trip_reports_costs_and_loss_without_edge_claim(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]],
                directory,
            )
            report = build_economic_report(directory)
            self.assertEqual(report.trade_count, 2)
            self.assertEqual(report.ending_position, Decimal("0E-8"))
            self.assertEqual(report.total_fees, Decimal("0.20300000"))
            self.assertEqual(report.turnover, Decimal("203.00000000"))
            self.assertEqual(report.net_pnl, Decimal("-3.20300000"))
            self.assertEqual(report.gross_pnl_before_fees, Decimal("-3.00000000"))
            self.assertGreater(report.max_drawdown, Decimal("0"))
            self.assertTrue(report.reconciled)
            self.assertEqual(report.economic_edge_claim, "UNPROVEN_SIMULATION_ONLY")

    def test_hold_only_has_zero_turnover_and_fee_rate(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 100, 100], directory)
            report = build_economic_report(directory)
            self.assertEqual(report.trade_count, 0)
            self.assertEqual(report.turnover, Decimal("0E-8"))
            self.assertEqual(report.total_fees, Decimal("0E-8"))
            self.assertEqual(report.effective_fee_rate, Decimal("0E-8"))
            self.assertEqual(report.net_pnl, Decimal("0E-8"))
            self.assertEqual(report.net_return, Decimal("0E-8"))

    def test_missing_state_is_not_reported_as_economic_evidence(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "completed simulated state"):
                build_economic_report(directory)


if __name__ == "__main__":
    unittest.main()
