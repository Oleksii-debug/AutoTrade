"""Causal learning and independent holdout over synthetic verified archives."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.adaptive_ma import (
    train_closed_candles, verify_model, verify_validation_report,
)
from tools.historical_public_klines import collect_month
from tools.historical_causal_replay import closed_candle_simulation_input
from tools.run_historical_zero import main as run_holdout
from tools.train_historical_agent import main as train, read_model, save_model
from tests.History.test_historical_learning_adapter import candidates as closed_candidates
from tools.validate_historical_agent import main as validate
from tests.History.test_run_historical_zero import full_month_fixture


class CausalTrainingTests(unittest.TestCase):
    def test_competing_model_publication_cannot_replace_first_version(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "models" / "candidate.json"
            original_link = os.link

            def competing_writer(source, destination):
                target.write_text('{"winner":true}\n', encoding="utf-8")
                return original_link(source, destination)

            with patch("tools.train_historical_agent.os.link", side_effect=competing_writer):
                with self.assertRaisesRegex(ValueError, "existing model differs"):
                    save_model(target, {"winner": False})
            self.assertEqual(json.loads(target.read_text()), {"winner": True})
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

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
            for month in ("2024-01", "2024-02", "2024-03"):
                raw, checksum, _ = full_month_fixture(
                    month=month,
                    close_at=lambda i: ("100", "100", "102", "102")[i % 4],
                )
                collect_month(symbol="BTCUSDT", interval="1h", month=month,
                              output=root, artifact_store=store,
                              fetch=lambda url, _, r=raw, c=checksum:
                              c if url.endswith(".CHECKSUM") else r)
            path = root / "models" / "trained-2024-01.json"
            results_path = root / "models" / "closed-simulation-results.json"
            results_path.parent.mkdir(parents=True, exist_ok=True)
            _dates, _opens, _closes, january_digest = closed_candle_simulation_input(
                root, symbol="BTCUSDT", interval="1h",
                from_month="2024-01", through_month="2024-01",
                artifact_store=store,
            )
            candidate_results = closed_candidates()
            for candidate in candidate_results:
                candidate["dataset_identity_sha256"] = january_digest
                simulation = candidate["simulation"]
                while len(simulation["decisions"]) < len(_dates) - 1:
                    episode = len(simulation["decisions"]) + 1
                    simulation["decisions"].append({
                        **simulation["decisions"][-1],
                        "episode": episode,
                        "status": "HOLD",
                        "decision": "HOLD",
                        "order_id": None,
                        "fill_id": None,
                        "reconciliation_event_id": f"reconcile-padding-{episode}",
                    })
                simulation["completed_episodes"] = len(simulation["decisions"])
                candidate["execution_count"] = len(simulation["decisions"])
            results_path.write_text(json.dumps({
                "candidate_simulations": candidate_results,
            }), encoding="utf-8")
            command = ["--data-root", tmp, "--through-month", "2024-01",
                       "--model-out", str(path),
                       "--simulation-results", str(results_path)]
            with redirect_stdout(StringIO()):
                self.assertEqual(train(command), 0)
                self.assertEqual(train(command), 0)  # identical retry
            model = read_model(path)
            self.assertTrue(model["model_sha256"])
            self.assertGreater(model["candidates"][0]["closed_outcomes"], 0)
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
            validation_path = root / "models" / "validation-2024-02.json"
            with redirect_stdout(StringIO()):
                self.assertEqual(validate(["--data-root", tmp,
                    "--from-month", "2024-02", "--through-month", "2024-02",
                    "--model-path", str(root / "models" / "trained-2024-01.json"),
                    "--report-out", str(validation_path)]), 2)
            # The model was deliberately tampered above. Restore the exact
            # original file, then produce a real independent validation cut.
            path.write_text(json.dumps(model), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(validate(["--data-root", tmp,
                    "--from-month", "2024-02", "--through-month", "2024-02",
                    "--model-path", str(path),
                    "--report-out", str(validation_path)]), 0)
            report = read_model(validation_path)
            self.assertFalse(report["model_updated"])
            self.assertGreater(report["matured_labels"], 0)
            march = datetime(2024, 3, 1, tzinfo=timezone.utc)
            verify_validation_report(report, model_sha256=model["model_sha256"],
                                     symbol="BTCUSDT", interval="1h",
                                     holdout_start_open=march)
            with self.assertRaisesRegex(ValueError, "overlaps"):
                verify_validation_report(report, model_sha256=model["model_sha256"],
                                         symbol="BTCUSDT", interval="1h",
                                         holdout_start_open=march - timedelta(hours=1))
            with self.assertRaisesRegex(ValueError, "integrity"):
                verify_validation_report(dict(report, correct_labels=report["correct_labels"] + 1),
                                         model_sha256=model["model_sha256"],
                                         symbol="BTCUSDT", interval="1h",
                                         holdout_start_open=march)
            march_command = ["--data-root", tmp, "--from-month", "2024-03",
                             "--through-month", "2024-03", "--next-bar-open",
                             "--model-path", str(path),
                             "--validation-report-path", str(validation_path),
                             "--virtual-cash", "200",
                             "--state-dir", str(root / "march-holdout"),
                             "--run-id", "march-independent",
                             "--stop-after-episodes", "2"]
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_holdout(march_command), 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["validation_report_sha256"],
                             report["report_sha256"])
            for altered in (
                dict(report, correct_labels=report["correct_labels"] + 1),
                dict(report, model_sha256="b" * 64),
                dict(report, validation_last_close_utc="2024-04-01T00:00:00+00:00"),
            ):
                if altered["correct_labels"] == report["correct_labels"]:
                    altered["report_sha256"] = sha256(json.dumps(
                        {key: value for key, value in altered.items() if key != "report_sha256"},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8")).hexdigest()
                validation_path.write_text(json.dumps(altered), encoding="utf-8")
                invalid_state = root / "invalid-march-holdout"
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run_holdout([
                        *march_command[:march_command.index("--state-dir")],
                        "--state-dir", str(invalid_state),
                        "--run-id", "invalid-march",
                        "--stop-after-episodes", "2",
                    ]), 2, output.getvalue())
                self.assertFalse(invalid_state.exists())


if __name__ == "__main__":
    unittest.main()
