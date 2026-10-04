"""Crash-boundary recovery tests for canonical simulation store ownership."""

from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import simulation_session
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    PROVIDER,
    run_canonical_simulation,
)


NOW = "2026-10-03T17:40:00Z"
HOLD = ["100", "101"]


def _foreign_event(store: JournalStore) -> None:
    payload = {"kind": "foreign"}
    store.append_event(
        {
            "event_id": "foreign-event",
            "event_type": "ForeignEvent",
            "schema_version": "1.0.0",
            "aggregate_type": "foreign",
            "aggregate_id": "foreign",
            "aggregate_version": "1",
            "host_id": "test",
            "owner_epoch": "1",
            "environment": ENVIRONMENT,
            "occurred_at": NOW,
            "observed_at": NOW,
            "committed_at": NOW,
            "correlation_id": "foreign-correlation",
            "causation_id": None,
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "evidence_refs": [],
        }
    )


class SimulationBootstrapRecoveryTests(unittest.TestCase):
    def test_owner_is_global_sequence_one_before_seed_mutation(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                DurableProviderEconomicBook,
                "append",
                side_effect=RuntimeError("crash-after-owner"),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash-after-owner"):
                    run_canonical_simulation(
                        HOLD, directory, episode_id="owner-first", now=NOW
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            owners = store.load_events(
                "canonical_simulation_store_owner", "canonical"
            )
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0]["event_type"], "SimulationSessionOwned")
            self.assertEqual(owners[0]["journal_sequence"], 1)
            self.assertEqual(store.current_journal_sequence(), 1)
            self.assertEqual(
                store.load_events_by_aggregate_type("economic_book"), []
            )

            resumed = run_canonical_simulation(
                HOLD, directory, episode_id="owner-first"
            )
            self.assertEqual(resumed["status"], "HOLD")
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["new_outbound_requests"], 0)

    def test_seed_persisted_before_delivery_recovers_without_duplicate_economics(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                simulation_session,
                "_deliver_event",
                side_effect=RuntimeError("crash-before-seed-delivery"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "crash-before-seed-delivery"
                ):
                    run_canonical_simulation(
                        HOLD, directory, episode_id="seed-recovery", now=NOW
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = DurableProviderEconomicBook(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            before = store.load_events("economic_book", book.book_id)
            self.assertEqual(len(before), 1)
            self.assertEqual(store.current_journal_sequence(), 2)

            resumed = run_canonical_simulation(
                HOLD, directory, episode_id="seed-recovery"
            )
            self.assertEqual(resumed["status"], "HOLD")
            self.assertTrue(resumed["resumed"])
            self.assertEqual(
                store.load_events("economic_book", book.book_id), before
            )

    def test_checkpoint_persisted_before_delivery_recovers_exact_prefix(self):
        with TemporaryDirectory() as directory:
            real_deliver = simulation_session._deliver_event
            calls = 0

            def crash_second(store, event_id, *, topic, expected_cut):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("crash-before-checkpoint-delivery")
                return real_deliver(store, event_id, topic=topic, expected_cut=expected_cut)

            with patch.object(
                simulation_session, "_deliver_event", side_effect=crash_second
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "crash-before-checkpoint-delivery"
                ):
                    run_canonical_simulation(
                        HOLD, directory, episode_id="checkpoint-recovery", now=NOW
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            checkpoints = store.load_events_by_aggregate_type(
                "account_reconciliation"
            )
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]["journal_sequence"], 3)
            self.assertEqual(store.current_journal_sequence(), 3)

            resumed = run_canonical_simulation(
                HOLD, directory, episode_id="checkpoint-recovery"
            )
            self.assertEqual(resumed["status"], "HOLD")
            self.assertTrue(resumed["resumed"])
            self.assertEqual(
                store.load_events_by_aggregate_type("account_reconciliation"),
                checkpoints,
            )

    def test_missing_seed_outbox_row_fails_closed_before_checkpoint(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                simulation_session,
                "_deliver_event",
                side_effect=RuntimeError("crash-before-seed-delivery"),
            ):
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        HOLD, directory, episode_id="missing-outbox", now=NOW
                    )

            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            before_sequence = store.current_journal_sequence()
            with sqlite3.connect(path) as connection:
                deleted = connection.execute("DELETE FROM outbox")
                self.assertEqual(deleted.rowcount, 1)
                connection.commit()

            with self.assertRaisesRegex(
                ValueError, "bootstrap durable state is not exact"
            ):
                run_canonical_simulation(
                    HOLD, directory, episode_id="missing-outbox"
                )

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(
                store.load_events_by_aggregate_type("account_reconciliation"), []
            )
            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"), []
            )

    def test_foreign_command_after_owner_blocks_before_bootstrap_mutation(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                DurableProviderEconomicBook,
                "append",
                side_effect=RuntimeError("crash-after-owner"),
            ):
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        HOLD, directory, episode_id="foreign-command", now=NOW
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.record_command(
                command_id="foreign-command-id",
                actor="foreign",
                environment=ENVIRONMENT,
                idempotency_key="foreign-command-key",
                request={"operation": "foreign"},
                result={"ok": True},
                state_version=0,
            )
            before_sequence = store.current_journal_sequence()

            with self.assertRaisesRegex(
                ValueError, "bootstrap durable state is not exact"
            ):
                run_canonical_simulation(
                    HOLD, directory, episode_id="foreign-command"
                )

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(
                store.load_events_by_aggregate_type("economic_book"), []
            )

    def test_foreign_projection_after_owner_blocks_before_bootstrap_mutation(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                DurableProviderEconomicBook,
                "append",
                side_effect=RuntimeError("crash-after-owner"),
            ):
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        HOLD, directory, episode_id="foreign-projection", now=NOW
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertTrue(
                store.save_global_projection_checkpoint(
                    projection_name="foreign-projection",
                    journal_sequence=1,
                    state={"foreign": True},
                )
            )
            before_sequence = store.current_journal_sequence()

            with self.assertRaisesRegex(
                ValueError, "bootstrap durable state is not exact"
            ):
                run_canonical_simulation(
                    HOLD, directory, episode_id="foreign-projection"
                )

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(
                store.load_events_by_aggregate_type("economic_book"), []
            )

    def test_foreign_event_after_owner_blocks_before_bootstrap_continuation(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                DurableProviderEconomicBook,
                "append",
                side_effect=RuntimeError("crash-after-owner"),
            ):
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        HOLD, directory, episode_id="foreign-after-owner", now=NOW
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            _foreign_event(store)
            before_sequence = store.current_journal_sequence()
            before_events = store.load_events_by_aggregate_type("foreign")

            with self.assertRaisesRegex(
                ValueError, "foreign durable journal authority"
            ):
                run_canonical_simulation(
                    HOLD, directory, episode_id="foreign-after-owner"
                )

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(
                store.load_events_by_aggregate_type("foreign"), before_events
            )
            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"), []
            )


if __name__ == "__main__":
    unittest.main()
