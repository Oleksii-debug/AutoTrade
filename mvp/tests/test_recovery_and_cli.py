import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.cli import get_status, main
from mvp.autotrade_mvp.pipeline import run_vertical_slice, verify_replay


class RecoveryEdgeCaseTests(unittest.TestCase):
    def test_generator_market_data_is_consumed_once(self):
        with TemporaryDirectory() as directory:
            prices = (value for value in [100, 101, 102, 103])
            result = run_vertical_slice(prices, directory)
            self.assertEqual(result.decision, "BUY")
            self.assertEqual(result.status, "filled")

    def test_checkpoint_written_before_evidence_can_be_repaired(self):
        with TemporaryDirectory() as directory:
            with patch(
                "mvp.autotrade_mvp.pipeline.handle_learning_evidence",
                side_effect=RuntimeError("simulated evidence crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated evidence crash"):
                    run_vertical_slice([100, 101, 102, 103], directory)

            self.assertTrue((Path(directory) / "checkpoint.json").exists())
            self.assertFalse((Path(directory) / "learning-evidence.jsonl").exists())

            recovered = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertTrue(recovered.resumed)
            self.assertEqual(recovered.evidence_count, 1)
            self.assertTrue(verify_replay(directory))

    def test_existing_orphan_evidence_is_adopted_exactly(self):
        with TemporaryDirectory() as directory:
            first = run_vertical_slice([100, 101, 102, 103], directory)
            evidence_path = Path(directory) / "learning-evidence.jsonl"
            original = evidence_path.read_text(encoding="utf-8")
            (Path(directory) / "checkpoint.json").unlink()

            rebuilt = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertFalse(rebuilt.resumed)
            self.assertEqual(rebuilt.fill_id, first.fill_id)
            self.assertEqual(evidence_path.read_text(encoding="utf-8"), original)
            self.assertTrue(verify_replay(directory))


class CliTests(unittest.TestCase):
    def test_status_before_start(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(get_status(directory), {"status": "not_started"})

    def test_multi_episode_cli_and_status(self):
        with TemporaryDirectory() as directory:
            argv = [
                "autotrade-mvp",
                "--state-dir",
                directory,
                "--multi-episode",
                "--prices",
                "100,101,102,103;100,100,100;103,102,101,100",
            ]
            output = io.StringIO()
            with patch("sys.argv", argv), patch("sys.stdout", output):
                self.assertEqual(main(), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual([row["decision"] for row in payload], ["BUY", "HOLD", "SELL"])
            self.assertEqual([row["status"] for row in payload], ["filled", "hold", "filled"])

            status = get_status(directory)
            self.assertEqual(status["status"], "running")
            self.assertTrue(status["replay_verified"])
            self.assertEqual(status["evidence_count"], 3)


if __name__ == "__main__":
    unittest.main()
