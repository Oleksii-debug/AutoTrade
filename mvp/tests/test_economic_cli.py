import io
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.cli import main
from mvp.autotrade_mvp.pipeline import run_multi_episode


class EconomicCliTests(unittest.TestCase):
    def test_economic_report_command_reads_existing_state_without_trading(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [[100, 101, 102, 103], [103, 102, 101, 100]],
                directory,
            )
            output = io.StringIO()
            argv = ["autotrade-mvp", "--state-dir", directory, "--economic-report"]
            with patch("sys.argv", argv), patch("sys.stdout", output):
                self.assertEqual(main(), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["trade_count"], 2)
            self.assertEqual(report["economic_edge_claim"], "UNPROVEN_SIMULATION_ONLY")
            self.assertTrue(report["reconciled"])


if __name__ == "__main__":
    unittest.main()
