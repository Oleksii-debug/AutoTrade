from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.learning.whitebit_live_zero import (
    WhiteBitLiveZeroLearning,
    validate_public_snapshot,
)


ORIGIN = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def snapshot(index: int, *, bid: str, ask: str) -> dict:
    available = ORIGIN + timedelta(minutes=index)
    text = available.isoformat()
    observed = text.replace("+00:00", "Z")
    return {
        "schema": "autotrade-whitebit-public-supervisor-v1",
        "market": "BTC_USDT",
        "venue": "WHITEBIT_SPOT",
        "bid_price": bid,
        "bid_quantity": "1.25",
        "ask_price": ask,
        "ask_quantity": "1.5",
        "source_event_at": (available - timedelta(milliseconds=20)).isoformat(),
        "available_at": text,
        "published_at": (available + timedelta(milliseconds=10)).isoformat(),
        "update_id": 1000 + index,
        "stream_generation": 7,
        "raw_evidence_ref": {
            "artifact_id": f"artifact-{index}",
            "sha256": "sha256:" + format(index, "064x"),
            "source_uri": "wss://wss.whitebit.com/ws#bookTicker",
            "observed_at": observed,
            "rights_id": "whitebit-public-websocket-internal-v1",
        },
        "public_data_only": True,
        "trading_authority": False,
    }


class WhiteBitLiveZeroLearningTests(unittest.TestCase):
    def test_accumulates_costed_episodes_and_keeps_evaluation_out_of_training(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            learner = WhiteBitLiveZeroLearning(root, evaluation_every=2)
            self.assertEqual(
                learner.process_snapshot(snapshot(1, bid="100", ask="101"))["status"],
                "PROCESSED",
            )
            learner.process_snapshot(snapshot(2, bid="101", ask="102"))
            train_result = learner.process_snapshot(snapshot(3, bid="103", ask="104"))
            self.assertEqual(train_result["matured_episode"]["split"], "TRAIN")
            self.assertEqual(train_result["matured_episode"]["actual_direction"], "UP")
            self.assertGreater(
                float(train_result["matured_episode"]["net_outcome_usdt"]), 0.0
            )
            self.assertGreater(
                float(train_result["matured_episode"]["fees_usdt"]), 0.0
            )
            self.assertEqual(json.loads((root / "latest-model.json").read_text())[
                "model_version"], 0)

            evaluation_result = learner.process_snapshot(
                snapshot(4, bid="102", ask="103")
            )
            self.assertEqual(evaluation_result["matured_episode"]["split"], "EVALUATION")
            self.assertEqual(json.loads((root / "latest-model.json").read_text())[
                "model_version"], 0)
            evaluation = json.loads((root / "independent-evaluation.json").read_text())
            self.assertEqual(evaluation["episode_count"], 1)
            self.assertEqual(evaluation["split"], "EVALUATION_ONLY_NO_MODEL_UPDATE")
            self.assertFalse(evaluation["profitability_proven"])

            restarted = WhiteBitLiveZeroLearning(root, evaluation_every=2)
            self.assertEqual(
                restarted.process_snapshot(snapshot(4, bid="102", ask="103"))["status"],
                "DUPLICATE",
            )
            con = sqlite3.connect(root / "episode-memory.sqlite3")
            try:
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM decisions WHERE episode_json IS NOT NULL"
                ).fetchone()[0], 2)
            finally:
                con.close()

    def test_restart_rebuilds_projections_from_sqlite_truth(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            learner = WhiteBitLiveZeroLearning(root)
            learner.process_snapshot(snapshot(1, bid="100", ask="101"))
            learner.process_snapshot(snapshot(2, bid="101", ask="102"))
            (root / "checkpoint.json").unlink()
            (root / "latest-model.json").unlink()
            restarted = WhiteBitLiveZeroLearning(root)
            checkpoint = restarted.status()
            self.assertEqual(checkpoint["last_observation_key"], "7:1002")
            self.assertEqual(len(checkpoint["pending_decision_ids"]), 1)
            self.assertFalse(checkpoint["trading_authority"])

    def test_tamper_future_and_reorder_fail_before_episode_mutation(self):
        with TemporaryDirectory() as temporary:
            learner = WhiteBitLiveZeroLearning(Path(temporary))
            learner.process_snapshot(snapshot(2, bid="101", ask="102"))
            with self.assertRaisesRegex(ValueError, "stale or reordered"):
                learner.process_snapshot(snapshot(1, bid="100", ask="101"))
            poisoned = snapshot(3, bid="103", ask="104")
            poisoned["trading_authority"] = True
            with self.assertRaisesRegex(ValueError, "authority differs"):
                learner.process_snapshot(poisoned)
            crossed = snapshot(3, bid="105", ask="104")
            with self.assertRaisesRegex(ValueError, "crossed"):
                validate_public_snapshot(crossed)


if __name__ == "__main__":
    unittest.main()
