"""Two successive learning cuts preserve separate frozen models and accounts."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore
from tools.historical_public_klines import collect_month
from tools.run_historical_walkforward import main, walk_forward
from tools.export_historical_diagnostics import main as export_diagnostics
from tests.History.test_run_historical_zero import full_month_fixture


class WalkForwardTests(unittest.TestCase):
    def test_successive_cuts_learn_only_after_previous_holdout_and_resume(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            store = ArtifactStore(data / ".artifact-store")
            for month in ("2024-01", "2024-02", "2024-03", "2024-04"):
                raw, checksum, _ = full_month_fixture(
                    month=month,
                    close_at=lambda i: ("100", "100", "102", "102")[i % 4],
                )
                collect_month(
                    symbol="BTCUSDT", interval="1h", month=month,
                    output=data, artifact_store=store,
                    fetch=lambda url, _, r=raw, c=checksum:
                    c if url.endswith(".CHECKSUM") else r,
                )
            args = dict(data_root=data, workspace=root / "state", symbol="BTCUSDT",
                        train_from="2024-01", first_holdout="2024-03",
                        last_holdout="2024-04", max_cycles=2, episode_limit=2)
            first = walk_forward(**args)
            self.assertEqual(first, walk_forward(**args))
            self.assertEqual(first["completed_cuts"], 2)
            self.assertIsNone(first["next_holdout_month"])
            march, april = first["cuts"]
            self.assertEqual((march["training_through"], april["training_through"]),
                             ("2024-01", "2024-02"))
            self.assertNotEqual(march["model_sha256"], april["model_sha256"])
            self.assertTrue(april["closed_training_outcomes"])
            self.assertEqual(set(april["accounts"]), {"200", "1000"})
            self.assertEqual({row["completed_episodes"] for row in april["accounts"].values()}, {2})
            self.assertTrue((root / "state" / "models" / "BTCUSDT-2024-01-2024-02.json").is_file())
            self.assertTrue((root / "state" / "accounts" / "BTCUSDT" /
                             "2024-04-2024-04" / "usd-1000").is_dir())
            spoken = StringIO()
            with redirect_stdout(spoken):
                self.assertEqual(main([
                    "--data-root", str(data), "--workspace", str(root / "state"),
                    "--symbol", "BTCUSDT", "--train-from", "2024-01",
                    "--first-holdout", "2024-03", "--last-holdout", "2024-04",
                    "--max-cycles", "2", "--episode-limit", "2", "--text",
                ]), 0)
            self.assertIn("Simulation month 2024-03", spoken.getvalue())
            self.assertIn("Virtual USD 1000", spoken.getvalue())
            self.assertIn("simulated marked P&L", spoken.getvalue())
            self.assertIn("not real fills or proven profit", spoken.getvalue())
            export_file = root / "share-with-support.json"
            self.assertEqual(export_diagnostics([
                "--workspace", str(root / "state"), "--output", str(export_file),
            ]), 0)
            diagnostic = json.loads(export_file.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["scope"], "HISTORICAL_SIMULATION")
            self.assertTrue(any(name.startswith("reports/") for name in diagnostic["artifacts"]))
            self.assertEqual(len(diagnostic["journals"]), 4)
            self.assertTrue(all(events for events in diagnostic["journals"].values()))
            self.assertEqual(export_diagnostics([
                "--workspace", str(root / "state"), "--output", str(export_file),
            ]), 2)
            with self.assertRaisesRegex(ValueError, "completed UTC months"):
                walk_forward(**dict(args, last_holdout="2099-01"))
            with self.assertRaisesRegex(ValueError, "max_cycles"):
                walk_forward(**dict(args, max_cycles=0))


if __name__ == "__main__":
    unittest.main()
