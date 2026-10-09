"""Whole-store CAS regressions for canonical simulation bootstrap."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp import simulation_session
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    run_canonical_simulation,
)


NOW = "2026-10-03T18:20:00Z"
HOLD = ["100", "100", "100"]


def _foreign_command(store: JournalStore, key: str) -> None:
    store.record_command(
        command_id=f"foreign-command-{key}",
        actor="foreign-bootstrap-writer",
        environment=ENVIRONMENT,
        idempotency_key=f"foreign-idempotency-{key}",
        request={"foreign": key},
        result={"inserted": True},
        state_version=0,
    )


class SimulationBootstrapWholeStoreCasTests(unittest.TestCase):
    def _store(self, directory: str) -> JournalStore:
        return JournalStore(Path(directory) / "journal.sqlite3")

    def test_foreign_command_before_seed_commit_blocks_seed_mutation(self):
        episode_id = "cas-before-seed"
        real_append = DurableProviderEconomicBook.append
        injected = False

        def race_append(book, transaction, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                _foreign_command(book.store, "seed")
            return real_append(book, transaction, **kwargs)

        with TemporaryDirectory() as directory:
            with patch.object(
                DurableProviderEconomicBook,
                "append",
                new=race_append,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "whole-store state changed after financial evidence validation",
                ):
                    run_canonical_simulation(
                        HOLD,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = self._store(directory)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "canonical_simulation_store_owner"
                    )
                ],
                ["SimulationSessionOwned"],
            )
            self.assertEqual(
                store.load_events_by_aggregate_type("economic_book"),
                [],
            )
            self.assertEqual(store.pending_outbox_count(), 0)
            self.assertEqual(
                store.whole_store_state_counts()["command_dedupe"],
                1,
            )

    def test_foreign_command_before_seed_delivery_keeps_outbox_pending(self):
        episode_id = "cas-before-seed-delivery"
        real_mark = JournalStore.mark_outbox_delivered
        injected = False

        def race_mark(store, outbox_id, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                _foreign_command(store, "seed-delivery")
            return real_mark(store, outbox_id, **kwargs)

        with TemporaryDirectory() as directory:
            with patch.object(
                JournalStore,
                "mark_outbox_delivered",
                new=race_mark,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "whole-store state changed after bootstrap validation",
                ):
                    run_canonical_simulation(
                        HOLD,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = self._store(directory)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type("economic_book")
                ],
                ["EconomicTransactionBatchBooked"],
            )
            self.assertEqual(
                store.load_events_by_aggregate_type("account_reconciliation"),
                [],
            )
            self.assertEqual(store.pending_outbox_count(), 1)
            self.assertEqual(
                store.load_events(
                    "canonical_simulation_session",
                    "single-episode",
                ),
                [],
            )

    def test_foreign_command_before_reconciliation_commit_blocks_checkpoint(self):
        episode_id = "cas-before-reconciliation"
        real_record = simulation_session.record_reconciliation_checkpoint
        injected = False

        def race_record(store, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                _foreign_command(store, "reconciliation")
            return real_record(store, **kwargs)

        with TemporaryDirectory() as directory:
            with patch.object(
                simulation_session,
                "record_reconciliation_checkpoint",
                new=race_record,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "whole-store state changed after bootstrap validation",
                ):
                    run_canonical_simulation(
                        HOLD,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = self._store(directory)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type("economic_book")
                ],
                ["EconomicTransactionBatchBooked"],
            )
            self.assertEqual(
                store.load_events_by_aggregate_type("account_reconciliation"),
                [],
            )
            self.assertEqual(store.pending_outbox_count(), 0)
            self.assertEqual(
                store.load_events(
                    "canonical_simulation_session",
                    "single-episode",
                ),
                [],
            )

    def test_foreign_projection_before_started_blocks_session_start(self):
        episode_id = "cas-before-started"
        real_event = simulation_session._event
        injected = False

        def race_event(store, kind, episode_id, payload, now, *, expected_cut=None):
            nonlocal injected
            if not injected and kind == "SimulationSessionStarted":
                injected = True
                store.save_projection_checkpoint(
                    projection_name="foreign-bootstrap-projection",
                    aggregate_type="canonical_simulation_store_owner",
                    aggregate_id="canonical",
                    aggregate_version=1,
                    state={"foreign": True},
                )
            return real_event(
                store, kind, episode_id, payload, now, expected_cut=expected_cut,
            )

        with TemporaryDirectory() as directory:
            # Inject at the caller boundary; never replace the canonical
            # reconciliation-protected JournalStore.append_event authority.
            with patch.object(simulation_session, "_event", new=race_event):
                with self.assertRaisesRegex(
                    ValueError,
                    "whole-store state changed after bootstrap validation",
                ):
                    run_canonical_simulation(
                        HOLD,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = self._store(directory)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type("economic_book")
                ],
                ["EconomicTransactionBatchBooked"],
            )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "account_reconciliation"
                    )
                ],
                ["AccountReconciled"],
            )
            self.assertEqual(store.pending_outbox_count(), 0)
            self.assertEqual(
                store.load_events(
                    "canonical_simulation_session",
                    "single-episode",
                ),
                [],
            )
            self.assertEqual(
                store.whole_store_state_counts()["projection_checkpoints"],
                1,
            )

    def test_foreign_command_before_hold_completed_blocks_terminal_append(self):
        episode_id = "cas-before-hold-completed"
        real_event = simulation_session._event
        injected = False

        def race_event(store, kind, episode_id, payload, now, *, expected_cut=None):
            nonlocal injected
            if not injected and kind == "SimulationSessionCompleted":
                injected = True
                _foreign_command(store, "hold-completed")
            return real_event(
                store, kind, episode_id, payload, now, expected_cut=expected_cut,
            )

        with TemporaryDirectory() as directory:
            # Preserve the reconciliation trust root while racing the
            # canonical episode publisher at its existing call boundary.
            with patch.object(simulation_session, "_event", new=race_event):
                with self.assertRaisesRegex(
                    ValueError,
                    "whole-store state changed after bootstrap validation",
                ):
                    run_canonical_simulation(
                        HOLD,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = self._store(directory)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events(
                        "canonical_simulation_session",
                        "single-episode",
                    )
                ],
                ["SimulationSessionStarted"],
            )
            self.assertEqual(store.pending_outbox_count(), 0)
            self.assertEqual(
                store.whole_store_state_counts()["command_dedupe"],
                2,
            )

            with self.assertRaisesRegex(
                ValueError,
                "HOLD terminal durable state is not exact",
            ):
                run_canonical_simulation(
                    HOLD,
                    directory,
                    episode_id=episode_id,
                )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events(
                        "canonical_simulation_session",
                        "single-episode",
                    )
                ],
                ["SimulationSessionStarted"],
            )

    def test_clean_bootstrap_still_completes_and_replays(self):
        episode_id = "cas-clean-bootstrap"
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                HOLD,
                directory,
                episode_id=episode_id,
                now=NOW,
            )
            self.assertEqual(first["status"], "HOLD")
            self.assertEqual(first["new_outbound_requests"], 0)

            replay = run_canonical_simulation(
                HOLD,
                directory,
                episode_id=episode_id,
            )
            self.assertEqual(replay["status"], "HOLD")
            self.assertTrue(replay["resumed"])
            self.assertEqual(replay["new_outbound_requests"], 0)


if __name__ == "__main__":
    unittest.main()
