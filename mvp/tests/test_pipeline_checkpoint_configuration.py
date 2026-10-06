import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.pipeline import (
    CHECKPOINT_CONFIGURATION_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    SIMULATION_STRATEGY_FAST,
    SIMULATION_STRATEGY_ID,
    SIMULATION_STRATEGY_SLOW,
    _event_uuid,
    _stable_hash,
    run_vertical_slice,
)
from mvp.autotrade_mvp.persistence import JournalStore


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class PipelineCheckpointConfigurationTests(unittest.TestCase):
    def test_new_checkpoint_binds_complete_financial_configuration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_vertical_slice(["100", "101", "102", "103"], root)
            self.assertEqual(result.status, "filled")

            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            configuration = checkpoint["checkpoint_configuration"]
            self.assertEqual(checkpoint["schema_version"], CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(
                configuration,
                {
                    "configuration_version": CHECKPOINT_CONFIGURATION_VERSION,
                    "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "symbol": "SIM",
                    "initial_cash": "10000",
                    "order_quantity": "1",
                    "max_abs_position": "10",
                    "max_notional": "5000",
                    "fee_rate": "0.001",
                    "strategy": {
                        "id": SIMULATION_STRATEGY_ID,
                        "fast": SIMULATION_STRATEGY_FAST,
                        "slow": SIMULATION_STRATEGY_SLOW,
                    },
                },
            )
            self.assertEqual(
                checkpoint["checkpoint_configuration_digest"],
                _stable_hash(configuration),
            )

    def test_journal_event_explicitly_binds_configuration_digest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_vertical_slice(["100", "101", "102", "103"], root)
            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][0]
            event = JournalStore(root / "journal.sqlite3").get_event(
                _event_uuid("simulation-episode", evidence_id)
            )
            self.assertIsNotNone(event)
            self.assertEqual(
                event["payload"]["checkpoint_configuration_digest"],
                checkpoint["checkpoint_configuration_digest"],
            )

    def test_configuration_identity_is_durable_before_episode_work(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "mvp.autotrade_mvp.pipeline.handle_market_data",
                side_effect=RuntimeError("stop after bootstrap"),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop after bootstrap"):
                    run_vertical_slice(["100", "101", "102", "103"], root)

            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["schema_version"], CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(
                checkpoint["checkpoint_configuration_digest"],
                _stable_hash(checkpoint["checkpoint_configuration"]),
            )
            self.assertEqual(checkpoint["postings"], [])
            self.assertEqual(checkpoint["fills"], {})
            self.assertEqual(checkpoint["evidence_ids"], [])
            self.assertFalse((root / "journal.sqlite3").exists())
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertFalse((root / "order-intents").exists())

    def test_changed_financial_configuration_rejects_before_any_durable_mutation(self):
        cases = {
            "initial_cash": {"initial_cash": "10001"},
            "order_quantity": {"order_quantity": "2"},
            "max_abs_position": {"max_abs_position": "11"},
            "max_notional": {"max_notional": "5001"},
            "fee_rate": {"fee_rate": "0.002"},
            "symbol": {"symbol": "OTHER"},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                run_vertical_slice(["100", "101", "102", "103"], root)
                before = _snapshot_tree(root)

                with self.assertRaisesRegex(
                    ValueError,
                    "Checkpoint financial configuration is incompatible with this resume",
                ):
                    run_vertical_slice(
                        ["100", "101", "102", "103"],
                        root,
                        **overrides,
                    )

                self.assertEqual(_snapshot_tree(root), before)

    def test_unchanged_configuration_resumes_with_same_configuration_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = run_vertical_slice(["100", "101", "102", "103"], root)
            before = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))

            resumed = run_vertical_slice(["100", "101", "102", "103"], root)
            after = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))

            self.assertFalse(first.resumed)
            self.assertTrue(resumed.resumed)
            self.assertEqual(
                after["checkpoint_configuration"],
                before["checkpoint_configuration"],
            )
            self.assertEqual(
                after["checkpoint_configuration_digest"],
                before["checkpoint_configuration_digest"],
            )
            self.assertEqual(after, before)

    def test_legacy_checkpoint_requires_explicit_migration_before_mutation(self):
        legacy = {
            "schema_version": 1,
            "symbol": "SIM",
            "initial_cash": "10000",
            "postings": [],
            "fills": {},
            "evidence_ids": [],
            "evidence_records": {},
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint_path.write_text(json.dumps(legacy), encoding="utf-8")
            before = _snapshot_tree(root)

            with self.assertRaisesRegex(
                ValueError,
                "Legacy checkpoint requires explicit migration before resume",
            ):
                run_vertical_slice(["100", "101", "102", "103"], root)

            self.assertEqual(_snapshot_tree(root), before)
            self.assertFalse((root / "journal.sqlite3").exists())
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertFalse((root / "order-intents").exists())

    def test_tampered_configuration_digest_fails_closed_before_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_vertical_slice(["100", "101", "102", "103"], root)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["checkpoint_configuration"]["fee_rate"] = "0.002"
            checkpoint_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = _snapshot_tree(root)

            with self.assertRaisesRegex(
                ValueError,
                "Checkpoint configuration identity is corrupt",
            ):
                run_vertical_slice(["100", "101", "102", "103"], root)

            self.assertEqual(_snapshot_tree(root), before)

    def test_rehashed_but_changed_configuration_still_cannot_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_vertical_slice(["100", "101", "102", "103"], root)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            configuration = checkpoint["checkpoint_configuration"]
            configuration["max_notional"] = "5001"
            checkpoint["checkpoint_configuration_digest"] = _stable_hash(configuration)
            checkpoint_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = _snapshot_tree(root)

            with self.assertRaisesRegex(
                ValueError,
                "Checkpoint financial configuration is incompatible with this resume",
            ):
                run_vertical_slice(["100", "101", "102", "103"], root)

            self.assertEqual(_snapshot_tree(root), before)

    def test_strategy_parameters_are_part_of_resume_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_vertical_slice(["100", "101", "102", "103"], root)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["checkpoint_configuration"]["strategy"]["fast"] = (
                SIMULATION_STRATEGY_FAST + 1
            )
            checkpoint["checkpoint_configuration_digest"] = _stable_hash(
                checkpoint["checkpoint_configuration"]
            )
            checkpoint_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = _snapshot_tree(root)

            with self.assertRaisesRegex(
                ValueError,
                "Checkpoint financial configuration is incompatible with this resume",
            ):
                run_vertical_slice(["100", "101", "102", "103"], root)

            self.assertEqual(_snapshot_tree(root), before)

    def test_checkpoint_initial_cash_must_match_configuration_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_vertical_slice(["100", "101", "102", "103"], root)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["initial_cash"] = "9999"
            checkpoint_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = _snapshot_tree(root)

            with self.assertRaisesRegex(
                ValueError,
                "Checkpoint initial cash conflicts with configuration identity",
            ):
                run_vertical_slice(["100", "101", "102", "103"], root)

            self.assertEqual(_snapshot_tree(root), before)


if __name__ == "__main__":
    unittest.main()
