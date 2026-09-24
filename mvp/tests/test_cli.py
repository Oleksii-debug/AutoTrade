import io
import json
from contextlib import redirect_stdout
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.cli import get_status, main
from mvp.autotrade_mvp.pipeline import run_vertical_slice


class CliTests(unittest.TestCase):
    def test_status_before_start(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(get_status(directory), {"status": "not_started"})

    def test_status_after_run_reports_verified_replay(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            status = get_status(directory)
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["evidence_count"], 1)
            self.assertTrue(status["replay_verified"])

    def test_main_status_mode(self):
        with TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch("sys.argv", ["autotrade-mvp", "--state-dir", directory, "--status"]):
                with redirect_stdout(output):
                    self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {"status": "not_started"})

    def test_multi_episode_mode_emits_json_array(self):
        with TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "autotrade-mvp",
                    "--state-dir",
                    directory,
                    "--multi-episode",
                    "--prices",
                    "100,101,102,103;100,100,100;103,102,101,100",
                ],
            ):
                with redirect_stdout(output):
                    self.assertEqual(main(), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual([item["decision"] for item in payload], ["BUY", "HOLD", "SELL"])
            self.assertEqual(Decimal(payload[-1]["position"]), Decimal("0"))

    def test_single_episode_mode_emits_json_object(self):
        with TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch(
                "sys.argv",
                ["autotrade-mvp", "--state-dir", directory, "--prices", "100,101,102,103"],
            ):
                with redirect_stdout(output):
                    self.assertEqual(main(), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["decision"], "BUY")
            self.assertTrue(payload["reconciled"])


if __name__ == "__main__":
    unittest.main()
