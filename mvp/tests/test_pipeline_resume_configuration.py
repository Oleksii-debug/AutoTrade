from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.pipeline import (
    RUN_CONFIGURATION_FILENAME,
    run_vertical_slice,
)


def _durable_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class PipelineResumeConfigurationTests(unittest.TestCase):
    def _seed_filled_run(self, root: Path) -> None:
        result = run_vertical_slice(["100", "101", "102"], root)
        self.assertEqual(result.status, "filled")
        self.assertFalse(result.resumed)

    def test_configuration_is_durable_before_market_processing_or_financial_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                run_vertical_slice(["not-a-price"], root)

            configuration_path = root / RUN_CONFIGURATION_FILENAME
            self.assertTrue(configuration_path.is_file())
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            self.assertEqual(configuration["symbol"], "SIM")
            self.assertEqual(configuration["initial_cash"], "10000.00000000")
            self.assertEqual(configuration["order_quantity"], "1.00000000")
            self.assertEqual(configuration["max_abs_position"], "10.00000000")
            self.assertEqual(configuration["max_notional"], "5000.00000000")
            self.assertEqual(configuration["fee_rate"], "0.001")
            self.assertEqual(
                configuration["strategy"],
                {"name": "moving_average", "fast": 2, "slow": 3},
            )
            self.assertFalse((root / "checkpoint.json").exists())
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertFalse((root / "journal.sqlite3").exists())
            self.assertFalse((root / "order-intents").exists())

    def test_changed_financial_configuration_rejects_before_any_durable_mutation(self):
        cases = (
            {"initial_cash": "9000"},
            {"order_quantity": "2"},
            {"max_abs_position": "11"},
            {"max_notional": "4000"},
            {"fee_rate": "0.002"},
        )
        for changed in cases:
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                root = Path(directory)
                self._seed_filled_run(root)
                before = _durable_snapshot(root)

                with self.assertRaisesRegex(
                    ValueError, "Incompatible durable run configuration"
                ):
                    run_vertical_slice(["103", "104", "105"], root, **changed)

                self.assertEqual(_durable_snapshot(root), before)

    def test_unchanged_configuration_resumes_canonically(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed_filled_run(root)
            before = _durable_snapshot(root)

            result = run_vertical_slice(["100", "101", "102"], root)

            self.assertTrue(result.resumed)
            self.assertEqual(_durable_snapshot(root), before)

    def test_legacy_checkpoint_without_configuration_identity_fails_closed(self):
        legacy_checkpoint = {
            "schema_version": 1,
            "symbol": "SIM",
            "initial_cash": "10000.00000000",
            "postings": [],
            "fills": {},
            "evidence_ids": [],
            "evidence_records": {},
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint_path.write_text(
                json.dumps(legacy_checkpoint, sort_keys=True),
                encoding="utf-8",
            )
            before = _durable_snapshot(root)

            with self.assertRaisesRegex(
                ValueError, "Legacy checkpoint lacks financial configuration identity"
            ):
                run_vertical_slice(["100", "101", "102"], root)

            self.assertEqual(_durable_snapshot(root), before)
            self.assertFalse((root / RUN_CONFIGURATION_FILENAME).exists())

    def test_checkpoint_digest_mismatch_rejects_before_reconstruction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._seed_filled_run(root)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["configuration_digest"] = "0" * 64
            checkpoint_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = _durable_snapshot(root)

            with self.assertRaisesRegex(
                ValueError, "Checkpoint financial configuration identity mismatch"
            ):
                run_vertical_slice(["103", "104", "105"], root)

            self.assertEqual(_durable_snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
