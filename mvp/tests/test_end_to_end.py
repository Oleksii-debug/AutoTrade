import json
import sqlite3
from datetime import datetime as real_datetime, timezone
from decimal import Decimal, Inexact, Rounded, ROUND_CEILING, localcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.pipeline import SimulatedProvider, run_multi_episode, run_vertical_slice, verify_replay
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


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

            checkpoint = json.loads(
                (Path(directory) / "checkpoint.json").read_text(encoding="utf-8")
            )
            events = JournalStore(
                Path(directory) / "journal.sqlite3"
            ).load_events("simulation_portfolio", "SIM")
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["payload"]["financial_configuration_hash"],
                checkpoint["financial_configuration_hash"],
            )

            evidence_path = Path(directory) / "learning-evidence.jsonl"
            evidence_before = evidence_path.read_bytes()
            self.assertEqual(
                json.loads(evidence_before)["risk_outcome"],
                "admitted",
            )
            self.assertEqual(json.loads(evidence_before)["episode_sequence"], 1)
            self.assertEqual(events[0]["payload"]["episode_sequence"], 1)

            restarted = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertTrue(restarted.resumed)
            self.assertEqual(restarted.order_id, first.order_id)
            self.assertEqual(restarted.fill_id, first.fill_id)
            self.assertEqual(restarted.position, 1)
            self.assertEqual(restarted.cash, first.cash)
            self.assertEqual(restarted.evidence_count, 1)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            evidence_lines = evidence_path.read_text(encoding="utf-8").splitlines()
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
            replay = run_multi_episode(episodes, directory)
            self.assertEqual(replay[-1].position, 0)
            self.assertEqual(replay[-1].evidence_count, 3)
            checkpoint = json.loads((Path(directory) / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(len(checkpoint["postings"]), 2)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 2)

    def test_multi_episode_buy_hold_sell_restart(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]]
            results = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in results], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in results], ["filled", "hold", "filled"])
            self.assertEqual(results[-1].position, 0)
            self.assertEqual(results[-1].evidence_count, 3)
            restarted = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in restarted], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in restarted], ["filled", "hold", "filled"])
            self.assertEqual(restarted[-1].position, 0)
            self.assertEqual(restarted[-1].evidence_count, 3)
            checkpoint = json.loads((Path(directory) / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(len(checkpoint["postings"]), 2)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 2)

    def test_intent_is_durable_before_simulated_execution(self):
        with TemporaryDirectory() as directory:
            with patch.object(SimulatedProvider, "execute", side_effect=RuntimeError("simulated crash")):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 1)
            checkpoint_path = Path(directory) / "checkpoint.json"
            self.assertTrue(checkpoint_path.exists())
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["schema_version"], 6)
            self.assertEqual(checkpoint["postings"], [])
            self.assertEqual(checkpoint["fills"], {})
            self.assertEqual(checkpoint["evidence_ids"], [])
            self.assertEqual(checkpoint["evidence_records"], {})
            self.assertEqual(
                checkpoint["initial_cash"],
                checkpoint["financial_configuration"]["initial_cash"],
            )
            self.assertEqual(len(checkpoint["financial_configuration_hash"]), 64)
            recovered = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(recovered.status, "filled")
            self.assertEqual(recovered.position, 1)

    def test_multi_episode_buy_hold_sell_restart_recovery(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]]
            results = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in results], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in results], ["filled", "hold", "filled"])
            self.assertEqual(results[-1].position, 0)
            self.assertEqual(results[-1].evidence_count, 3)
            with patch.object(SimulatedProvider, "execute", side_effect=RuntimeError("simulated crash")):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    run_multi_episode(episodes, directory)
            restarted = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in restarted], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in restarted], ["filled", "hold", "filled"])
            self.assertEqual(restarted[-1].position, 0)
            self.assertEqual(restarted[-1].evidence_count, 3)

    def test_invalid_evidence_timestamp_is_rejected_before_journal_creation(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = {
                "evidence_id": "evidence-invalid-time",
                "input_hash": "0" * 64,
                "decision": "HOLD",
                "decision_reason": "test",
                "risk_outcome": "hold",
                "order_id": None,
                "fill_id": None,
                "cash": "10000.00000000",
                "position": "0",
                "equity": "10000.00000000",
                "reconciled": True,
                "recorded_at": "not-a-timestampZ",
            }
            with self.assertRaisesRegex(ValueError, "valid ISO-8601"):
                pipeline_module.handle_journal_event(
                    root,
                    "SIM",
                    evidence,
                    "0" * 64,
                )
            self.assertFalse((root / "journal.sqlite3").exists())

    def test_journal_rejects_episode_sequence_outside_aggregate_order(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            evidence = json.loads(
                (root / "learning-evidence.jsonl").read_text(encoding="utf-8")
            )
            evidence["evidence_id"] = "evidence-out-of-order"
            evidence["episode_sequence"] = 3

            before = JournalStore(root / "journal.sqlite3").whole_store_state_cut()
            with self.assertRaisesRegex(
                ValueError,
                "aggregate version conflicts with episode sequence",
            ):
                pipeline_module.handle_journal_event(
                    root,
                    "SIM",
                    evidence,
                    evidence["financial_configuration_hash"],
                )
            after = JournalStore(root / "journal.sqlite3").whole_store_state_cut()
            self.assertEqual(after, before)

    def test_resume_rejects_residual_evidence_with_empty_checkpoint_authority(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                pipeline_module,
                "handle_durable_order_intent",
                side_effect=RuntimeError("stop after initial checkpoint"),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop after initial checkpoint"):
                    run_vertical_slice([100, 101, 102, 103], directory)

            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["evidence_ids"], [])
            self.assertEqual(checkpoint["evidence_records"], {})
            evidence_path = root / "learning-evidence.jsonl"
            evidence_path.write_text("\n", encoding="utf-8")

            checkpoint_before = checkpoint_path.read_bytes()
            evidence_before = evidence_path.read_bytes()
            with self.assertRaisesRegex(
                ValueError,
                "without checkpoint evidence authority",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertFalse((root / "journal.sqlite3").exists())
            self.assertFalse((root / "order-intents").exists())

    def test_resume_rejects_residual_journal_with_empty_checkpoint_authority(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                pipeline_module,
                "handle_durable_order_intent",
                side_effect=RuntimeError("stop after initial checkpoint"),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop after initial checkpoint"):
                    run_vertical_slice([100, 101, 102, 103], directory)

            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["evidence_ids"], [])
            self.assertEqual(checkpoint["evidence_records"], {})
            empty_store = JournalStore(root / "journal.sqlite3")
            journal_before = empty_store.whole_store_state_cut()
            self.assertEqual(journal_before["journal_sequence"], 0)

            checkpoint_before = checkpoint_path.read_bytes()
            with self.assertRaisesRegex(
                ValueError,
                "without checkpoint evidence authority",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(
                JournalStore(root / "journal.sqlite3").whole_store_state_cut(),
                journal_before,
            )
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertFalse((root / "order-intents").exists())

    def test_missing_evidence_after_checkpoint_is_repaired(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            evidence = Path(directory) / "learning-evidence.jsonl"
            evidence.unlink()
            replay = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(replay.evidence_count, 1)
            self.assertEqual(len(evidence.read_text(encoding="utf-8").splitlines()), 1)

    def test_resume_repairs_latest_missing_evidence_before_new_episode(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [[100, 101, 102, 103], [100, 100, 100]],
                directory,
            )
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            evidence_path.write_text(rows[0] + "\n", encoding="utf-8")

            resumed = run_vertical_slice(
                [103, 102, 101, 100],
                directory,
            )

            self.assertEqual(resumed.status, "filled")
            self.assertEqual(resumed.decision, "SELL")
            self.assertEqual(resumed.position, 0)
            self.assertEqual(resumed.evidence_count, 3)
            self.assertEqual(
                len(evidence_path.read_text(encoding="utf-8").splitlines()),
                3,
            )
            self.assertTrue(verify_replay(directory))

    def test_resume_repairs_sequence_tail_when_wall_clock_moves_backward(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        class ControlledDateTime:
            current = real_datetime(2030, 1, 1, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return cls.current.replace(tzinfo=None)
                return cls.current.astimezone(tz)

            @classmethod
            def fromisoformat(cls, value):
                return real_datetime.fromisoformat(value)

        with TemporaryDirectory() as directory:
            with patch.object(pipeline_module, "datetime", ControlledDateTime):
                ControlledDateTime.current = real_datetime(
                    2030, 1, 1, tzinfo=timezone.utc
                )
                run_vertical_slice([100, 101, 102, 103], directory)
                ControlledDateTime.current = real_datetime(
                    2020, 1, 1, tzinfo=timezone.utc
                )
                run_vertical_slice([100, 100, 100], directory)

            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            parsed = [json.loads(row) for row in rows]
            self.assertEqual([row["episode_sequence"] for row in parsed], [1, 2])
            self.assertLess(parsed[1]["recorded_at"], parsed[0]["recorded_at"])

            evidence_path.write_text(rows[0] + "\n", encoding="utf-8")
            resumed = run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual(resumed.status, "filled")
            self.assertEqual(resumed.decision, "SELL")
            repaired = [
                json.loads(row)
                for row in evidence_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [row["episode_sequence"] for row in repaired],
                [1, 2, 3],
            )
            self.assertTrue(verify_replay(directory))

    def test_replay_rejects_evidence_order_outside_episode_sequence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [
                    [100, 101, 102, 103],
                    [100, 100, 100],
                    [103, 102, 101, 100],
                ],
                directory,
            )
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 3)
            evidence_path.write_text(
                "\n".join([rows[1], rows[0], rows[2]]) + "\n",
                encoding="utf-8",
            )

            checkpoint_before = (root / "checkpoint.json").read_bytes()
            evidence_before = evidence_path.read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            self.assertFalse(verify_replay(directory))
            with self.assertRaisesRegex(
                ValueError,
                "Learning evidence chronology conflicts with checkpoint episode sequence",
            ):
                run_vertical_slice([100, 100, 100], directory)

            self.assertEqual((root / "checkpoint.json").read_bytes(), checkpoint_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )

    def test_resume_rejects_reordered_postings_before_tail_repair_write(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [
                    [100, 101, 102, 103],
                    [100, 100, 100],
                    [103, 102, 101, 100],
                ],
                directory,
            )
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 3)
            evidence_path.write_text(
                "\n".join(rows[:2]) + "\n",
                encoding="utf-8",
            )

            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(len(checkpoint["postings"]), 2)
            checkpoint["postings"].reverse()
            checkpoint_path.write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )

            checkpoint_before = checkpoint_path.read_bytes()
            evidence_before = evidence_path.read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "posting chronology conflicts with durable replay prefix",
            ):
                run_vertical_slice([100, 100, 100], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )

    def test_resume_rejects_foreign_journal_before_repairing_latest_evidence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [[100, 101, 102, 103], [100, 100, 100]],
                directory,
            )
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            evidence_path.write_text(rows[0] + "\n", encoding="utf-8")

            store = JournalStore(root / "journal.sqlite3")
            payload = {"kind": "foreign-before-repair"}
            store.append_event(
                {
                    "event_id": "foreign-before-replay-repair",
                    "event_type": "ForeignEvent",
                    "aggregate_type": "foreign",
                    "aggregate_id": "noise",
                    "aggregate_version": "1",
                    "committed_at": "2026-10-06T00:00:00Z",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )

            checkpoint_before = (root / "checkpoint.json").read_bytes()
            evidence_before = evidence_path.read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Unexpected durable journal state before replay repair",
            ):
                run_vertical_slice(
                    [103, 102, 101, 100],
                    directory,
                )

            self.assertEqual((root / "checkpoint.json").read_bytes(), checkpoint_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_checkpoint_event_divergence_before_repairing_evidence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [[100, 101, 102, 103], [100, 100, 100]],
                directory,
            )
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            evidence_path.write_text(rows[0] + "\n", encoding="utf-8")

            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            latest_id = max(
                checkpoint["evidence_ids"],
                key=lambda evidence_id: checkpoint["evidence_records"][evidence_id][
                    "recorded_at"
                ],
            )
            checkpoint["evidence_records"][latest_id]["cash"] = "999.00000000"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            checkpoint_before = checkpoint_path.read_bytes()
            evidence_before = evidence_path.read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Corrupt simulation journal replay event",
            ):
                run_vertical_slice(
                    [103, 102, 101, 100],
                    directory,
                )

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_foreign_evidence_before_repairing_latest_evidence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [[100, 101, 102, 103], [100, 100, 100]],
                directory,
            )
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            foreign = json.loads(rows[0])
            foreign["evidence_id"] = "evidence-foreign-before-repair"
            evidence_path.write_text(
                rows[0] + "\n" + json.dumps(foreign) + "\n",
                encoding="utf-8",
            )

            checkpoint_before = (root / "checkpoint.json").read_bytes()
            evidence_before = evidence_path.read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Learning evidence conflicts with checkpoint before replay repair",
            ):
                run_vertical_slice(
                    [103, 102, 101, 100],
                    directory,
                )

            self.assertEqual((root / "checkpoint.json").read_bytes(), checkpoint_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_historical_evidence_gap_before_new_financial_work(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [
                    [100, 101, 102, 103],
                    [100, 100, 100],
                    [103, 102, 101, 100],
                ],
                directory,
            )
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 3)
            evidence_path.write_text(
                rows[1] + "\n" + rows[2] + "\n",
                encoding="utf-8",
            )
            checkpoint_before = (root / "checkpoint.json").read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }
            evidence_before = evidence_path.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "Historical learning evidence is incomplete",
            ):
                run_vertical_slice(
                    [100, 101, 102, 103],
                    directory,
                )

            self.assertEqual((root / "checkpoint.json").read_bytes(), checkpoint_before)
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_financial_corruption_before_repairing_latest_evidence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [[100, 101, 102, 103], [100, 100, 100]],
                directory,
            )
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            evidence_path = root / "learning-evidence.jsonl"
            rows = evidence_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            evidence_path.write_text(rows[0] + "\n", encoding="utf-8")

            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertTrue(checkpoint["postings"])
            checkpoint["postings"][0]["cash_delta"] = "0"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            checkpoint_before = checkpoint_path.read_bytes()
            evidence_before = evidence_path.read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Fill and economic ledger do not reconcile",
            ):
                run_vertical_slice(
                    [103, 102, 101, 100],
                    directory,
                )

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_malformed_latest_evidence_before_any_repair_write(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            evidence_path = root / "learning-evidence.jsonl"

            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][-1]
            checkpoint["evidence_records"][evidence_id].pop("risk_outcome")
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            evidence_path.unlink()
            for journal_path in root.glob("journal.sqlite3*"):
                journal_path.unlink()

            checkpoint_before = checkpoint_path.read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Corrupt checkpoint replay evidence",
            ):
                run_vertical_slice(
                    [103, 102, 101, 100],
                    directory,
                )

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertFalse(evidence_path.exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_latest_evidence_financial_tamper_before_repair(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            evidence_path = root / "learning-evidence.jsonl"

            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][-1]
            checkpoint["evidence_records"][evidence_id]["cash"] = "1"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            evidence_path.unlink()
            for journal_path in root.glob("journal.sqlite3*"):
                journal_path.unlink()

            checkpoint_before = checkpoint_path.read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "does not match final financial state",
            ):
                run_vertical_slice(
                    [103, 102, 101, 100],
                    directory,
                )

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertFalse(evidence_path.exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_false_equity_before_repairing_missing_tail(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            evidence_path = root / "learning-evidence.jsonl"

            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][-1]
            record = checkpoint["evidence_records"][evidence_id]
            self.assertIn("valuation_price", record)
            record["equity"] = "1.00000000"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            evidence_path.unlink()
            for journal_path in root.glob("journal.sqlite3*"):
                journal_path.unlink()

            checkpoint_before = checkpoint_path.read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "equity does not match valuation",
            ):
                run_vertical_slice(
                    [103, 102, 101, 100],
                    directory,
                )

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertFalse(evidence_path.exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )


    def test_resume_rejects_duplicate_checkpoint_json_keys_before_mutation(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint_text = checkpoint_path.read_text(encoding="utf-8")
            marker = '"schema_version": 6,'
            self.assertEqual(checkpoint_text.count(marker), 1)
            checkpoint_path.write_text(
                checkpoint_text.replace(
                    marker,
                    marker + '\n  "schema_version": 5,',
                    1,
                ),
                encoding="utf-8",
            )

            checkpoint_before = checkpoint_path.read_bytes()
            evidence_before = (root / "learning-evidence.jsonl").read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(ValueError, "Corrupt checkpoint JSON"):
                run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(
                (root / "learning-evidence.jsonl").read_bytes(),
                evidence_before,
            )
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )

    def test_replay_rejects_duplicate_evidence_json_keys(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            encoded = json.dumps(row, sort_keys=True)
            duplicate = (
                encoded[:-1]
                + ',"risk_outcome":'
                + json.dumps(row["risk_outcome"])
                + '}'
            )
            evidence_path.write_text(duplicate + "\n", encoding="utf-8")

            self.assertFalse(verify_replay(directory))
            evidence_before = evidence_path.read_bytes()
            checkpoint_before = (root / "checkpoint.json").read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "Corrupt learning evidence before replay repair",
            ):
                run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual(evidence_path.read_bytes(), evidence_before)
            self.assertEqual(
                (root / "checkpoint.json").read_bytes(),
                checkpoint_before,
            )
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)


    def test_checkpoint_ledger_mismatch_is_rejected(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["postings"][0]["position_delta"] = "2"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reconcile"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_corrupt_financial_checkpoint_rejects_unbounded_decimal_text(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["postings"][0]["cash_delta"] = "1e999999"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "checkpoint cash_delta"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_corrupt_financial_checkpoint_rejects_non_text_fill_scalar(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            fill = next(iter(checkpoint["fills"].values()))
            fill["quantity"] = 1
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "checkpoint fill quantity"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restored_fill_must_match_bound_provider_and_intent_semantics(self):
        cases = (
            ("symbol", "OTHER"),
            ("side", "BROKEN"),
            ("fee", "999.00000000"),
            ("fill_id", "fill-" + "0" * 20),
            ("client_order_id", "intent-" + "0" * 20),
        )
        for field, value in cases:
            with self.subTest(field=field), TemporaryDirectory() as directory:
                run_vertical_slice([100, 101, 102, 103], directory)
                root = Path(directory)
                checkpoint_path = root / "checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                fill = next(iter(checkpoint["fills"].values()))
                fill[field] = value
                checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                durable_before = {
                    "checkpoint": checkpoint_path.read_bytes(),
                    "evidence": (root / "learning-evidence.jsonl").read_bytes(),
                    "journal": (root / "journal.sqlite3").read_bytes(),
                    "intents": {
                        path.name: path.read_bytes()
                        for path in (root / "order-intents").glob("*.json")
                    },
                }

                with self.assertRaisesRegex(ValueError, "checkpoint fill"):
                    run_vertical_slice([100, 101, 102, 103], directory)

                self.assertEqual(checkpoint_path.read_bytes(), durable_before["checkpoint"])
                self.assertEqual(
                    (root / "learning-evidence.jsonl").read_bytes(),
                    durable_before["evidence"],
                )
                self.assertEqual(
                    (root / "journal.sqlite3").read_bytes(),
                    durable_before["journal"],
                )
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in (root / "order-intents").glob("*.json")
                    },
                    durable_before["intents"],
                )

    def test_restored_fill_requires_its_durable_order_intent(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            intent_path = next((root / "order-intents").glob("*.json"))
            intent_path.unlink()
            checkpoint_before = (root / "checkpoint.json").read_bytes()
            evidence_before = (root / "learning-evidence.jsonl").read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "Durable order intent missing for checkpoint fill",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            self.assertEqual((root / "checkpoint.json").read_bytes(), checkpoint_before)
            self.assertEqual(
                (root / "learning-evidence.jsonl").read_bytes(),
                evidence_before,
            )
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertFalse(intent_path.exists())

    def test_market_data_rejects_oversized_text_before_decimal_construction(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        with patch.object(
            pipeline_module,
            "Decimal",
            side_effect=AssertionError("unbounded Decimal construction"),
        ):
            with self.assertRaisesRegex(ValueError, "finite and positive"):
                pipeline_module.handle_market_data(["1e999999"])

    def test_market_data_rejects_hostile_scalar_subclasses_without_virtual_conversion(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        class HostileStr(str):
            def __str__(self):
                raise AssertionError("hostile str conversion")

        class HostileFloat(float):
            def __str__(self):
                raise AssertionError("hostile float conversion")

        class HostileInt(int):
            def __str__(self):
                raise AssertionError("hostile int conversion")

        class HostileDecimal(Decimal):
            def __str__(self):
                raise AssertionError("hostile Decimal conversion")

        for value in (
            HostileStr("100"),
            HostileFloat(100.0),
            HostileInt(100),
            HostileDecimal("100"),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError,
                    "exact float, str, int or Decimal",
                ):
                    pipeline_module.handle_market_data([value])

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

    def test_binary_float_financial_configuration_is_rejected(self):
        cases = (
            {"initial_cash": 10000.0},
            {"order_quantity": 1.0},
            {"max_abs_position": 10.0},
            {"max_notional": 5000.0},
            {"fee_rate": 0.001},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), TemporaryDirectory() as directory:
                with self.assertRaises(TypeError):
                    run_vertical_slice([100, 101, 102, 103], directory, **kwargs)

    def test_money_quantization_ignores_mutable_module_aliases(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        expected = pipeline_module._money("1.234567895")
        with (
            patch.object(pipeline_module, "MONEY_QUANTUM", Decimal("1")),
            patch.object(
                pipeline_module,
                "round_fraction_to_quantum",
                side_effect=AssertionError("mutable rounder alias executed"),
            ),
            patch.object(
                pipeline_module,
                "as_fraction",
                side_effect=AssertionError("mutable fraction alias executed"),
            ),
            patch.object(
                pipeline_module,
                "parse_bounded_exact_decimal",
                side_effect=AssertionError("mutable parser alias executed"),
            ),
        ):
            observed = pipeline_module._money("1.234567895")

        self.assertEqual(observed, expected)
        self.assertEqual(observed, Decimal("1.23456790"))

    def test_financial_configuration_rejects_noncanonical_symbol_text(self):
        for symbol in (" SIM", "SIM ", " SIM ", "\tSIM", "SIM\n"):
            with self.subTest(symbol=repr(symbol)), TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    ValueError,
                    "symbol must be non-empty canonical text",
                ):
                    run_vertical_slice(
                        [100, 101, 102, 103],
                        directory,
                        symbol=symbol,
                    )
                self.assertEqual(list(Path(directory).iterdir()), [])


    def test_financial_configuration_rejects_hostile_scalar_subclasses_before_use(self):
        class HostileText(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("hostile strip callback executed")

            def __bool__(self):
                raise AssertionError("hostile bool callback executed")

        class HostileDecimal(Decimal):
            def __str__(self):
                raise AssertionError("hostile Decimal string callback executed")

        class HostileInt(int):
            def __str__(self):
                raise AssertionError("hostile int string callback executed")

        with TemporaryDirectory() as directory:
            cases = (
                {"symbol": HostileText("SIM")},
                {"initial_cash": HostileDecimal("10000")},
                {"order_quantity": HostileInt(1)},
            )
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(TypeError):
                        run_vertical_slice(
                            [100, 101, 102, 103],
                            directory,
                            **kwargs,
                        )

    def test_financial_outputs_ignore_ambient_decimal_context(self):
        prices = ["100.12345678", "101.23456789", "102.34567891", "103.45678912"]
        kwargs = {
            "order_quantity": "3.14159265",
            "max_abs_position": "10",
            "max_notional": "1000",
            "fee_rate": "0.00123456",
        }
        with TemporaryDirectory() as reference_dir, TemporaryDirectory() as hostile_dir:
            reference = run_vertical_slice(prices, reference_dir, **kwargs)
            with localcontext() as context:
                context.prec = 2
                context.rounding = ROUND_CEILING
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                hostile = run_vertical_slice(prices, hostile_dir, **kwargs)

        self.assertEqual(hostile.status, reference.status)
        self.assertEqual(hostile.decision, reference.decision)
        self.assertEqual(hostile.cash, reference.cash)
        self.assertEqual(hostile.position, reference.position)
        self.assertEqual(hostile.equity, reference.equity)
        self.assertEqual(hostile.reconciled, reference.reconciled)

    def test_buy_sell_cycle_ignores_ambient_decimal_context(self):
        episodes = [
            ["100.12345678", "101.23456789", "102.34567891", "103.45678912"],
            ["103.45678912", "102.34567891", "101.23456789", "100.12345678"],
        ]
        kwargs = {
            "order_quantity": "3.14159265",
            "max_abs_position": "10",
            "max_notional": "1000",
            "fee_rate": "0.00123456",
        }
        with TemporaryDirectory() as reference_dir, TemporaryDirectory() as hostile_dir:
            reference = run_multi_episode(episodes, reference_dir, **kwargs)
            with localcontext() as context:
                context.prec = 2
                context.rounding = ROUND_CEILING
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                hostile = run_multi_episode(episodes, hostile_dir, **kwargs)

        self.assertEqual([item.decision for item in hostile], ["BUY", "SELL"])
        self.assertEqual(
            [(item.cash, item.position, item.equity, item.reconciled) for item in hostile],
            [(item.cash, item.position, item.equity, item.reconciled) for item in reference],
        )
        self.assertEqual(hostile[-1].position, reference[-1].position)

    def test_resume_accepts_equivalent_fee_rate_presentations(self):
        with TemporaryDirectory() as directory:
            first = run_vertical_slice(
                [100, 101, 102, 103],
                directory,
                fee_rate="0.0010",
            )
            root = Path(directory)
            checkpoint_before = json.loads(
                (root / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                checkpoint_before["financial_configuration"]["fee_rate"],
                "0.001",
            )
            configuration_hash = checkpoint_before["financial_configuration_hash"]

            resumed = run_vertical_slice(
                [100, 101, 102, 103],
                directory,
                fee_rate="1e-3",
            )
            self.assertTrue(resumed.resumed)
            self.assertEqual(resumed.order_id, first.order_id)
            self.assertEqual(resumed.fill_id, first.fill_id)

            checkpoint_after = json.loads(
                (root / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                checkpoint_after["financial_configuration"]["fee_rate"],
                "0.001",
            )
            self.assertEqual(
                checkpoint_after["financial_configuration_hash"],
                configuration_hash,
            )

    def test_resume_rejects_changed_financial_configuration_before_mutation(self):
        changes = (
            {"initial_cash": "9999"},
            {"order_quantity": "2"},
            {"max_abs_position": "9"},
            {"max_notional": "4999"},
            {"fee_rate": "0.002"},
        )
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_before = (root / "checkpoint.json").read_text(encoding="utf-8")
            evidence_before = (root / "learning-evidence.jsonl").read_text(encoding="utf-8")
            intents_before = sorted(path.name for path in (root / "order-intents").glob("*.json"))

            for kwargs in changes:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaisesRegex(
                        ValueError,
                        "financial configuration changed",
                    ):
                        run_vertical_slice([100, 101, 102, 103], directory, **kwargs)

            self.assertEqual(
                (root / "checkpoint.json").read_text(encoding="utf-8"),
                checkpoint_before,
            )
            self.assertEqual(
                (root / "learning-evidence.jsonl").read_text(encoding="utf-8"),
                evidence_before,
            )
            self.assertEqual(
                sorted(path.name for path in (root / "order-intents").glob("*.json")),
                intents_before,
            )

    def test_checkpoint_state_cannot_diverge_from_bound_financial_configuration(self):
        cases = (
            ("initial_cash", "9999", "initial cash does not match"),
            ("symbol", "OTHER", "symbol does not match"),
        )
        for field, value, message in cases:
            with self.subTest(field=field), TemporaryDirectory() as directory:
                run_vertical_slice([100, 101, 102, 103], directory)
                root = Path(directory)
                checkpoint_path = root / "checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                checkpoint[field] = value
                checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                evidence_before = (root / "learning-evidence.jsonl").read_bytes()
                journal_before = (root / "journal.sqlite3").read_bytes()
                intents_before = {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                }

                with self.assertRaisesRegex(ValueError, message):
                    run_vertical_slice([100, 101, 102, 103], directory)

                self.assertEqual(
                    (root / "learning-evidence.jsonl").read_bytes(),
                    evidence_before,
                )
                self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in (root / "order-intents").glob("*.json")
                    },
                    intents_before,
                )

    def test_atomic_json_sync_failure_never_commits_canonical_state(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            with patch.object(
                pipeline_module.os,
                "fsync",
                side_effect=OSError("forced durable sync failure"),
            ):
                with self.assertRaisesRegex(OSError, "forced durable sync failure"):
                    pipeline_module._atomic_json(
                        checkpoint_path,
                        {"schema_version": 5},
                    )

            temporary = root / "checkpoint.json.tmp"
            self.assertFalse(checkpoint_path.exists())
            self.assertTrue(temporary.exists())
            temporary_before = temporary.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "Interrupted durable atomic write requires explicit recovery",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            self.assertEqual(temporary.read_bytes(), temporary_before)
            self.assertFalse(checkpoint_path.exists())
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])


    def test_resume_rejects_incoherent_strategy_evidence_before_tail_repair(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][0]
            evidence = checkpoint["evidence_records"][evidence_id]
            self.assertEqual(evidence["decision"], "BUY")
            evidence["decision_reason"] = "averages_equal"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            (root / "learning-evidence.jsonl").unlink()
            for journal_path in root.glob("journal.sqlite3*"):
                journal_path.unlink()

            checkpoint_before = checkpoint_path.read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Corrupt checkpoint replay evidence semantics",
            ):
                run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])

    def test_resume_revalidates_bound_risk_outcome_before_tail_repair(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][0]
            checkpoint["evidence_records"][evidence_id]["risk_outcome"] = "max_notional"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            (root / "learning-evidence.jsonl").unlink()
            for journal_path in root.glob("journal.sqlite3*"):
                journal_path.unlink()

            checkpoint_before = checkpoint_path.read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Checkpoint replay fill violates bound risk admission",
            ):
                run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])

    def test_resume_rejects_rebound_intent_identity_before_tail_repair(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(len(checkpoint["fills"]), 1)

            original_order_id, fill = next(iter(checkpoint["fills"].items()))
            original_fill_id = fill["fill_id"]
            forged_order_id = "intent-" + "e" * 20
            self.assertNotEqual(forged_order_id, original_order_id)
            forged_fill_id = (
                "fill-"
                + sha256(forged_order_id.encode("utf-8")).hexdigest()[:20]
            )

            intents_root = root / "order-intents"
            original_intent_path = intents_root / f"{original_order_id}.json"
            forged_intent_path = intents_root / f"{forged_order_id}.json"
            intent_payload = json.loads(
                original_intent_path.read_text(encoding="utf-8")
            )
            intent_payload["client_order_id"] = forged_order_id
            forged_intent_path.write_text(
                json.dumps(intent_payload),
                encoding="utf-8",
            )
            original_intent_path.unlink()

            forged_fill = dict(fill)
            forged_fill["client_order_id"] = forged_order_id
            forged_fill["fill_id"] = forged_fill_id
            checkpoint["fills"] = {forged_order_id: forged_fill}
            for posting in checkpoint["postings"]:
                if posting["fill_id"] == original_fill_id:
                    posting["fill_id"] = forged_fill_id
            evidence_id = checkpoint["evidence_ids"][0]
            evidence = checkpoint["evidence_records"][evidence_id]
            evidence["order_id"] = forged_order_id
            evidence["fill_id"] = forged_fill_id
            checkpoint_path.write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )

            (root / "learning-evidence.jsonl").unlink()
            for journal_path in root.glob("journal.sqlite3*"):
                journal_path.unlink()

            checkpoint_before = checkpoint_path.read_bytes()
            intent_before = forged_intent_path.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "Checkpoint replay intent identity is not causally bound",
            ):
                run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(forged_intent_path.read_bytes(), intent_before)
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])

    def test_resume_rejects_orphan_durable_order_intent_before_mutation(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_before = (root / "checkpoint.json").read_bytes()
            evidence_before = (root / "learning-evidence.jsonl").read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents = root / "order-intents"
            original_intent = next(intents.glob("*.json"))
            orphan = intents / ("intent-" + "f" * 20 + ".json")
            orphan.write_bytes(original_intent.read_bytes())
            orphan_before = orphan.read_bytes()

            self.assertFalse(verify_replay(directory))
            with self.assertRaisesRegex(
                ValueError,
                "Durable order intents do not match checkpoint fills",
            ):
                run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual((root / "checkpoint.json").read_bytes(), checkpoint_before)
            self.assertEqual(
                (root / "learning-evidence.jsonl").read_bytes(),
                evidence_before,
            )
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(orphan.read_bytes(), orphan_before)

    def test_crash_after_durable_intent_fails_closed_on_resume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                SimulatedProvider,
                "execute",
                side_effect=RuntimeError("forced provider interruption"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "forced provider interruption",
                ):
                    run_vertical_slice([100, 101, 102, 103], directory)

            checkpoint_path = root / "checkpoint.json"
            checkpoint_before = checkpoint_path.read_bytes()
            checkpoint = json.loads(checkpoint_before)
            self.assertEqual(checkpoint["fills"], {})
            self.assertEqual(checkpoint["evidence_ids"], [])
            intents = list((root / "order-intents").glob("*.json"))
            self.assertEqual(len(intents), 1)
            intent_before = intents[0].read_bytes()
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertFalse((root / "journal.sqlite3").exists())

            with self.assertRaisesRegex(
                ValueError,
                "Durable order intents do not match checkpoint fills",
            ):
                run_vertical_slice([103, 102, 101, 100], directory)

            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before)
            self.assertEqual(intents[0].read_bytes(), intent_before)
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertFalse((root / "journal.sqlite3").exists())

    def test_orphan_order_intent_temp_blocks_fresh_run_rebinding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            intents = root / "order-intents"
            intents.mkdir(parents=True)
            orphan = intents / "intent-orphan.json.tmp"
            orphan.write_text('{"orphan":true}\n', encoding="utf-8")
            orphan_before = orphan.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "Interrupted durable atomic write requires explicit recovery",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            self.assertEqual(orphan.read_bytes(), orphan_before)
            self.assertFalse((root / "checkpoint.json").exists())
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertEqual(list(root.glob("journal.sqlite3*")), [])
            self.assertEqual(list(intents.glob("*.json")), [])


    def test_pending_atomic_temp_blocks_existing_checkpoint_resume(self):
        for pending_relative in (
            "checkpoint.json.tmp",
            "order-intents/intent-pending.json.tmp",
        ):
            with self.subTest(pending_relative=pending_relative), TemporaryDirectory() as directory:
                run_vertical_slice([100, 101, 102, 103], directory)
                root = Path(directory)
                pending = root / pending_relative
                pending.parent.mkdir(parents=True, exist_ok=True)
                pending.write_bytes(b"pending-atomic-replacement")
                pending_before = pending.read_bytes()

                checkpoint_before = (root / "checkpoint.json").read_bytes()
                evidence_before = (root / "learning-evidence.jsonl").read_bytes()
                journal_before = (root / "journal.sqlite3").read_bytes()
                intents_before = {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                }

                with self.assertRaisesRegex(
                    ValueError,
                    "Interrupted durable atomic write requires explicit recovery",
                ):
                    run_vertical_slice([103, 102, 101, 100], directory)

                self.assertEqual(pending.read_bytes(), pending_before)
                self.assertEqual(
                    (root / "checkpoint.json").read_bytes(),
                    checkpoint_before,
                )
                self.assertEqual(
                    (root / "learning-evidence.jsonl").read_bytes(),
                    evidence_before,
                )
                self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in (root / "order-intents").glob("*.json")
                    },
                    intents_before,
                )


    def test_missing_checkpoint_cannot_rebind_residual_durable_state(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint_path.unlink()
            evidence_before = (root / "learning-evidence.jsonl").read_bytes()
            journal_before = (root / "journal.sqlite3").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            with self.assertRaisesRegex(
                ValueError,
                "Durable state exists without exact financial configuration identity",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            self.assertFalse(checkpoint_path.exists())
            self.assertEqual(
                (root / "learning-evidence.jsonl").read_bytes(),
                evidence_before,
            )
            self.assertEqual((root / "journal.sqlite3").read_bytes(), journal_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )

    def test_learning_evidence_is_bound_to_financial_configuration(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            evidence_path = root / "learning-evidence.jsonl"
            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            row["financial_configuration_hash"] = "0" * 64
            evidence_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

            self.assertFalse(verify_replay(directory))
            with self.assertRaisesRegex(
                ValueError,
                "Learning evidence conflicts with checkpoint",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_rehashed_checkpoint_configuration_cannot_redefine_resume_policy(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["financial_configuration"]["max_notional"] = "999"
            checkpoint["financial_configuration_hash"] = sha256(
                json.dumps(
                    checkpoint["financial_configuration"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            self.assertFalse(verify_replay(directory))

            with self.assertRaisesRegex(
                ValueError,
                "financial configuration changed",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_legacy_checkpoint_without_financial_identity_fails_closed(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            original = json.loads(checkpoint_path.read_text(encoding="utf-8"))

            checkpoint = dict(original)
            checkpoint["schema_version"] = 1
            checkpoint.pop("financial_configuration", None)
            checkpoint.pop("financial_configuration_hash", None)
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "Legacy checkpoint schema 1",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            checkpoint = dict(original)
            checkpoint["schema_version"] = 4
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "Unsupported or corrupt checkpoint schema",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_replay_requires_one_journal_event_per_evidence_record(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_path = root / "learning-evidence.jsonl"
            original = json.loads(evidence_path.read_text(encoding="utf-8"))
            missing_journal = dict(original)
            missing_journal["evidence_id"] = "evidence-missing-journal"
            checkpoint["evidence_ids"].append(missing_journal["evidence_id"])
            checkpoint["evidence_ids"].sort()
            checkpoint["evidence_records"][missing_journal["evidence_id"]] = missing_journal
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            evidence_path.write_text(
                json.dumps(original) + "\n" + json.dumps(missing_journal) + "\n",
                encoding="utf-8",
            )

            self.assertFalse(verify_replay(directory))

    def test_replay_rejects_journal_state_cut_drift(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            original = JournalStore.whole_store_state_cut
            calls = {"count": 0}

            def drifting_cut(store):
                cut = original(store)
                calls["count"] += 1
                if calls["count"] > 1:
                    cut = {
                        "journal_sequence": cut["journal_sequence"] + 1,
                        "counts": dict(cut["counts"]),
                    }
                return cut

            with patch.object(
                JournalStore,
                "whole_store_state_cut",
                new=drifting_cut,
            ):
                self.assertFalse(verify_replay(directory))

            self.assertGreaterEqual(calls["count"], 2)

    def test_replay_and_resume_reject_corrupt_simulation_outbox(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            database = root / "journal.sqlite3"

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE outbox SET payload_json = ?",
                    ("{}",),
                )
                connection.commit()
                corrupt_payload = connection.execute(
                    "SELECT payload_json FROM outbox"
                ).fetchone()[0]

            checkpoint_before = (root / "checkpoint.json").read_bytes()
            evidence_before = (root / "learning-evidence.jsonl").read_bytes()
            intents_before = {
                path.name: path.read_bytes()
                for path in (root / "order-intents").glob("*.json")
            }

            self.assertFalse(verify_replay(directory))
            with self.assertRaisesRegex(
                ValueError,
                "outbox envelope hash does not match stored payload",
            ):
                run_vertical_slice([103, 102, 101, 100], directory)

            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT payload_json FROM outbox"
                    ).fetchone()[0],
                    corrupt_payload,
                )
            self.assertEqual(
                (root / "checkpoint.json").read_bytes(),
                checkpoint_before,
            )
            self.assertEqual(
                (root / "learning-evidence.jsonl").read_bytes(),
                evidence_before,
            )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (root / "order-intents").glob("*.json")
                },
                intents_before,
            )

    def test_replay_rejects_unrelated_simulation_aggregate_event(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint = json.loads(
                (Path(directory) / "checkpoint.json").read_text(encoding="utf-8")
            )
            store = JournalStore(Path(directory) / "journal.sqlite3")
            next_version = store.next_aggregate_version(
                "simulation_portfolio",
                checkpoint["symbol"],
            )
            payload = {
                "financial_configuration_hash": checkpoint["financial_configuration_hash"],
            }
            store.append_event(
                {
                    "event_id": "simulation-noise-event",
                    "event_type": "UnrelatedSimulationEvent",
                    "aggregate_type": "simulation_portfolio",
                    "aggregate_id": checkpoint["symbol"],
                    "aggregate_version": str(next_version),
                    "committed_at": "2026-10-06T00:00:00Z",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )
            self.assertFalse(verify_replay(directory))

    def test_replay_rejects_checkpoint_posting_tamper(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertTrue(checkpoint["postings"])
            checkpoint["postings"][0]["cash_delta"] = "0"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            self.assertFalse(verify_replay(directory))

    def test_replay_rejects_checkpoint_fill_tamper(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertTrue(checkpoint["fills"])
            fill = next(iter(checkpoint["fills"].values()))
            fill["quantity"] = "2"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            self.assertFalse(verify_replay(directory))

    def test_replay_rejects_unrelated_journal_aggregate(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            payload = {"kind": "unrelated"}
            store.append_event(
                {
                    "event_id": "unrelated-journal-event",
                    "event_type": "UnrelatedEvent",
                    "aggregate_type": "unrelated",
                    "aggregate_id": "noise",
                    "aggregate_version": "1",
                    "committed_at": "2026-10-06T00:00:00Z",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )

            self.assertFalse(verify_replay(directory))

    def test_replay_rejects_incomplete_evidence_schema_even_when_checkpoint_matches(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            root = Path(directory)
            checkpoint_path = root / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][0]
            checkpoint["evidence_records"][evidence_id].pop("risk_outcome")
            checkpoint_path.write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )
            evidence_path = root / "learning-evidence.jsonl"
            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            row.pop("risk_outcome")
            evidence_path.write_text(
                json.dumps(row) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(verify_replay(directory))

    def test_replay_rejects_valid_event_shape_with_wrong_evidence_identity(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint = json.loads(
                (Path(directory) / "checkpoint.json").read_text(encoding="utf-8")
            )
            store = JournalStore(Path(directory) / "journal.sqlite3")
            original = store.load_events("simulation_portfolio", checkpoint["symbol"])[0]
            forged = dict(original)
            forged["event_id"] = "simulation-forged-evidence-identity"
            forged["aggregate_version"] = str(
                store.next_aggregate_version("simulation_portfolio", checkpoint["symbol"])
            )
            forged_payload = dict(original["payload"])
            forged_payload["cash"] = "1"
            forged["payload"] = forged_payload
            forged["payload_hash"] = payload_digest(forged_payload)
            store.append_event(forged)
            self.assertFalse(verify_replay(directory))

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