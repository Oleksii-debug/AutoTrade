import io
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.accessibility import format_accessible_status
from mvp.autotrade_mvp.cli import main
from mvp.autotrade_mvp.pipeline import run_vertical_slice


class AccessibleStatusTests(unittest.TestCase):
    def test_not_started_is_truthful_and_non_visual(self):
        text = format_accessible_status({"status": "not_started"})
        self.assertIn("System state: Not started", text)
        self.assertIn("Live order submission: unavailable", text)
        self.assertIn("Economic edge: unproven", text)
        self.assertNotIn("\x1b", text)

    def test_recovery_state_is_explicit(self):
        text = format_accessible_status(
            {
                "status": "needs_recovery",
                "symbol": "SIM",
                "initial_cash": "1000",
                "evidence_count": 2,
                "fills": {},
                "replay_verified": False,
            }
        )
        self.assertIn("Replay verification: failed", text)
        self.assertIn("Action required: recovery or reconciliation is needed", text)
        self.assertNotIn("completed", text.lower())

    def test_economic_fields_are_copyable_text(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM",
                "initial_cash": "1000",
                "evidence_count": 1,
                "fills": {"fill-1": {}},
                "replay_verified": True,
            },
            {
                "final_equity": "1001.25",
                "net_pnl": "1.25",
                "total_fees": "0.25",
                "turnover": "100",
                "max_drawdown": "0",
                "reconciled": True,
            },
        )
        for expected in (
            "Final equity: 1001.25",
            "Net profit or loss: 1.25",
            "Total fees: 0.25",
            "Maximum drawdown: 0",
            "Economic reconciliation: passed",
        ):
            self.assertIn(expected, text)

    def test_cli_accessible_status_after_simulation(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            output = io.StringIO()
            with patch("sys.argv", ["autotrade", "--state-dir", directory, "--accessible-status"]):
                with patch("sys.stdout", output):
                    self.assertEqual(main(), 0)
            text = output.getvalue()
            self.assertIn("Mode: simulation only", text)
            self.assertIn("Replay verification: passed", text)
            self.assertIn("Instrument: SIM", text)
            self.assertIn("Economic edge: unproven", text)
            self.assertNotIn("{", text)


if __name__ == "__main__":
    unittest.main()
