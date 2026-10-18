"""One command launches the existing finance engine with two isolated budgets."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from tools.historical_public_klines import collect_month
from tools.run_historical_campaign import (
    MAX_SIMULATION_BUNDLE_BYTES,
    _has_closed_position_cycle,
    _read_simulation_bundle,
    campaign,
    main,
)
from tests.History.test_run_historical_zero import full_month_fixture


class HistoricalCampaignTests(unittest.TestCase):
    def _archives(self, root: Path):
        store = ArtifactStore(root / ".artifact-store")
        for month in ("2024-01", "2024-02", "2024-03"):
            raw, checksum, _ = full_month_fixture(
                month=month,
                close_at=lambda i: ("100", "100", "102", "102")[i % 4],
            )
            collect_month(
                symbol="BTCUSDT", interval="1h", month=month,
                output=root, artifact_store=store,
                fetch=lambda url, _, r=raw, c=checksum: c if url.endswith(".CHECKSUM") else r,
            )

    def _args(self, root):
        return dict(data_root=root / "data", workspace=root / "state",
                    symbol="BTCUSDT", train_from="2024-01", train_through="2024-01",
                    validate_from="2024-02", validate_through="2024-02",
                    holdout_from="2024-03", holdout_through="2024-03",
                    episode_limit=2)

    def test_two_budgets_restart_and_immutable_prefix_report(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._archives(root / "data")
            params = self._args(root)
            first = campaign(**params)
            self.assertEqual(set(first["accounts"]), {"200", "1000"})
            self.assertEqual({row["completed_episodes"] for row in first["accounts"].values()}, {2})
            self.assertEqual(first, campaign(**params))
            self.assertTrue((root / "state" / "accounts" / "BTCUSDT" / "2024-03-2024-03" / "usd-200").is_dir())
            self.assertTrue((root / "state" / "accounts" / "BTCUSDT" / "2024-03-2024-03" / "usd-1000").is_dir())
            saved = json.loads((root / "state" / "reports" / "BTCUSDT-2024-03-2024-03-2.json").read_text())
            self.assertEqual(saved, first)
            resumed = campaign(**dict(params, episode_limit=3))
            self.assertEqual({row["completed_episodes"] for row in resumed["accounts"].values()}, {3})
            self.assertEqual(first["model_sha256"], resumed["model_sha256"])
            self.assertEqual(first["validation_report_sha256"], resumed["validation_report_sha256"])

    def test_preflight_and_interrupted_second_account_resume(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._archives(root / "data")
            params = self._args(root)
            with self.assertRaisesRegex(ValueError, "ordered and disjoint"):
                campaign(**dict(params, validate_from="2024-01"))
            self.assertFalse((root / "state").exists())
            from tools import run_historical_campaign as module
            original = module.run_holdout
            calls = 0

            def interrupted(args):
                nonlocal calls
                calls += 1
                if calls == 5:  # three training candidates, then two holdout accounts
                    return 2
                return original(args)

            with patch.object(module, "run_holdout", side_effect=interrupted):
                with self.assertRaisesRegex(ValueError, "USD 1000 holdout blocked"):
                    campaign(**params)
            self.assertTrue((root / "state" / "accounts" / "BTCUSDT" / "2024-03-2024-03" / "usd-200").is_dir())
            self.assertFalse((root / "state" / "reports").exists())
            self.assertEqual(campaign(**params)["accounts"]["1000"]["completed_episodes"], 2)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--data-root", str(root / "data"),
                    "--workspace", str(root / "bad"), "--symbol", "BTCUSDT",
                    "--train-from", "2024-01", "--train-through", "2024-02",
                    "--validate-from", "2024-02", "--validate-through", "2024-02",
                    "--holdout-from", "2024-03", "--holdout-through", "2024-03"]), 2)
            self.assertFalse((root / "bad").exists())

    def test_frozen_simulation_bundle_has_its_own_bounded_reader(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate-simulations.json"
            payload = {
                "candidate_simulations": [{"receipt": "x" * 25000}],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertGreater(path.stat().st_size, 20000)
            self.assertEqual(_read_simulation_bundle(path), payload)
            with path.open("ab") as stream:
                stream.truncate(MAX_SIMULATION_BUNDLE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "unbounded"):
                _read_simulation_bundle(path)

    def test_closed_cycle_requires_position_to_return_flat(self):
        self.assertFalse(_has_closed_position_cycle({
            "decisions": [{"position": "0"}, {"position": "0.25"}],
        }))
        self.assertTrue(_has_closed_position_cycle({
            "decisions": [
                {"position": "0"},
                {"position": "0.25"},
                {"position": "0.10"},
                {"position": "0"},
            ],
        }))
        with self.assertRaisesRegex(ValueError, "malformed"):
            _has_closed_position_cycle({"decisions": [{"position": 0.0}]})


if __name__ == "__main__":
    unittest.main()
