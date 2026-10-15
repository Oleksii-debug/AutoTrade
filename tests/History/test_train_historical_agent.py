"""Causal learning and independent holdout over synthetic verified archives."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.adaptive_ma import train_closed_candles, verify_model
from tools.historical_public_klines import collect_month
from tools.run_historical_zero import main as run_holdout
from tools.train_historical_agent import main as train, read_model
from tests.History.test_run_historical_zero import full_month_fixture


class CausalTrainingTests(unittest.TestCase):
    def test_label_matures_after_prediction_and_weights_change(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        prices = ["100", "100", "102", "102", "100", "100", "102", "102",
                  "100", "100", "102", "102"]
        model = train_closed_candles(
            dates=[start + timedelta(hours=i) for i in range(len(prices))],
            prices=prices, dataset_sha256="a" * 64,
            symbol="BTCUSDT", interval="1h",
        )
        self.assertTrue(any(row["matured_labels"] > 0 for row in model["candidates"]))
        self.assertGreater(len({row["weight"] for row in model["candidates"]}), 1)
        self.assertEqual(model["training_last_close_utc"],
                         "2024-01-01T12:00:00+00:00")
        verify_model(model, symbol="BTCUSDT", interval="1h",
                     holdout_start_open=start + timedelta(hours=12))
        with self.assertRaisesRegex(ValueError, "overlaps"):
            verify_model(model, symbol="BTCUSDT", interval="1h",
                         holdout_start_open=start + timedelta(hours=11))
        poisoned = dict(model, source_dataset_sha256="b" * 64)
        with self.assertRaisesRegex(ValueError, "integrity"):
            verify_model(poisoned, symbol="BTCUSDT", interval="1h",
                         holdout_start_open=start + timedelta(hours=12))

    def test_train_save_restart_and_evaluate_later_month_without_retraining(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / ".artifact-store")
            for month in ("2024-01", "2024-02"):
                raw, checksum, _ = full_month_fixture(
                    month=month,
                    close_at=lambda i: ("100", "100", "102", "102")[i % 4],
                )
                collect_month(symbol="BTCUSDT", interval="1h", month=month,
                              output=root, artifact_store=store,
                              fetch=lambda url, _, r=raw, c=checksum:
                              c if url.endswith(".CHECKSUM") else r)
            path = root / "models" / "trained-2024-01.json"
            command = ["--data-root", tmp, "--through-month", "2024-01",
                       "--model-out", str(path)]
            with redirect_stdout(StringIO()):
                self.assertEqual(train(command), 0)
                self.assertEqual(train(command), 0)  # identical retry
            model = read_model(path)
            self.assertTrue(model["model_sha256"])
            self.assertGreater(model["candidates"][0]["matured_labels"], 0)
            holdout = ["--data-root", tmp, "--from-month", "2024-02",
                       "--through-month", "2024-02", "--next-bar-open",
                       "--model-path", str(path), "--virtual-cash", "200",
                       "--state-dir", str(root / "holdout-state"),
                       "--run-id", "independent-holdout"]
            for stop in (2, 3):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run_holdout([*holdout, "--stop-after-episodes", str(stop)]),
                                     0, output.getvalue())
                result = json.loads(output.getvalue())
                self.assertEqual(result["trained_model_sha256"], model["model_sha256"])
                self.assertEqual(result["trained_strategy_parameters"], model["selected_parameters"])
                self.assertEqual(result["simulation"]["completed_episodes"], stop)
                self.assertEqual(result["simulation"]["resumed"], stop == 3)
            # No replay of training data into a holdout state, even when the
            # same model artifact is otherwise valid.
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_holdout(["--data-root", tmp,
                    "--through-month", "2024-01", "--next-bar-open",
                    "--model-path", str(path), "--state-dir", str(root / "bad-state")]), 2)
            self.assertIn("overlaps", output.getvalue())
            self.assertFalse((root / "bad-state").exists())
            tampered = dict(model)
            tampered["selected_parameters"] = (
                {"fast": 5, "slow": 8}
                if model["selected_parameters"] != {"fast": 5, "slow": 8}
                else {"fast": 2, "slow": 3}
            )
            path.write_text(json.dumps(tampered), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_holdout([*holdout[:holdout.index("--state-dir")],
                    "--state-dir", str(root / "tampered-state"),
                    "--run-id", "tampered"]), 2)
            self.assertIn("integrity", output.getvalue())
            self.assertFalse((root / "tampered-state").exists())


if __name__ == "__main__":
    unittest.main()
