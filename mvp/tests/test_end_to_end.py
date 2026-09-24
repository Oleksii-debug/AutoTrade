import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.pipeline import SimulatedProvider, run_multi_episode, run_vertical_slice, verify_replay


class VerticalSliceTests(unittest.TestCase):
    def test_full_path_and_restart_are_idempotent(self):
        with TemporaryDirectory() as directory:
            first = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(first.status, "filled")
            self.assertEqual(first.decision, "BUY")
            self.assertEqual(first.position, 1)
            self.assertTrue(first.reconciled)
            self.assertEqual(first.evidence_count, 1)
            self.assertFalse(first.resumed)

            restarted = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertTrue(restarted.resumed)
            self.assertEqual(restarted.order_id, first.order_id)
            self.assertEqual(restarted.fill_id, first.fill_id)
            self.assertEqual(restarted.position, 1)
            self.assertEqual(restarted.cash, first.cash)
            self.assertEqual(restarted.evidence_count, 1)
            evidence_lines = (Path(directory) / "learning-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(evidence_lines), 1)
            self.assertTrue(json.loads(evidence_lines[0])["reconciled"])

    def test_risk_rejection_creates_no_fill(self):
        with TemporaryDirectory() as directory:
            result = run_vertical_slice([100, 101, 102, 103], directory, max_notional="10")
            self.assertEqual(result.status, "risk_rejected")
            self.assertIsNone(result.fill_id)
            self.assertEqual(result.position, 0)
            self.assertTrue(result.reconciled)

    def test_invalid_market_data_is_rejected(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_vertical_slice([100, float("nan")], directory)

    def test_corrupt_checkpoint_is_not_silently_accepted(self):
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint.write_text('{"schema_version": 99}', encoding="utf-8")
            with self.assertRaises(ValueError):
                run_vertical_slice([100, 101, 102], directory)

    def test_multi_episode_buy_hold_sell_and_replay(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]]
            results = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in results], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in results], ["filled", "hold", "filled"])
            self.assertEqual(results[-1].position, 0)
            self.assertEqual(results[-1].evidence_count, 3)
            replay = run_multi_episode(episodes, directory, max_abs_position="1")
            self.assertEqual(replay[-1].position, 0)
            self.assertEqual(replay[-1].evidence_count, 3)
            checkpoint = json.loads((Path(directory) / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(len(checkpoint["postings"]), 2)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 2)

    def test_intent_is_durable_before_simulated_execution(self):
        with TemporaryDirectory() as directory:
            with patch.object(SimulatedProvider, "execute", side_effect=RuntimeError("simulated crash")):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 1)
            self.assertFalse((Path(directory) / "checkpoint.json").exists())
            recovered = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(recovered.status, "filled")
            self.assertEqual(recovered.position, 1)

    def test_missing_evidence_after_checkpoint_is_repaired(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            evidence = Path(directory) / "learning-evidence.jsonl"
            evidence.unlink()
            replay = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(replay.evidence_count, 1)
            self.assertEqual(len(evidence.read_text(encoding="utf-8").splitlines()), 1)

    def test_checkpoint_ledger_mismatch_is_rejected(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["postings"][0]["position_delta"] = "2"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reconcile"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_cash_limit_and_invalid_configuration(self):
        with TemporaryDirectory() as directory:
            rejected = run_vertical_slice([100, 101, 102, 103], directory, initial_cash="10")
            self.assertEqual(rejected.status, "risk_rejected")
            self.assertEqual(rejected.position, 0)
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_vertical_slice([100, 101, 102, 103], directory, fee_rate="-0.1")
            with self.assertRaises(ValueError):
                run_vertical_slice(["0.000000001"], directory)

    def test_replay_verification_detects_tampered_evidence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode([[100, 101, 102, 103], [103, 102, 101, 100]], directory)
            self.assertTrue(verify_replay(directory))
            evidence_path = Path(directory) / "learning-evidence.jsonl"
            rows = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["reconciled"] = False
            evidence_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            self.assertFalse(verify_replay(directory))
            with self.assertRaises(ValueError):
                run_vertical_slice([100, 101, 102, 103], directory)


if __name__ == "__main__":
    unittest.main()
