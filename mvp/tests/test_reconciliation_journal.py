from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ProviderActivityEvidence,
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    load_latest_reconciliation_checkpoint,
    record_reconciliation_checkpoint,
    unknown_submissions_from_dispatch,
    unresolved_attempt_ids_from_checkpoint,
    unresolved_provider_activity_ids_from_checkpoint,
)


def snapshot():
    return SnapshotConsistencyEvidence(
        mode="ATOMIC",
        query_started_at="2026-09-24T17:00:00Z",
        query_completed_at="2026-09-24T19:00:00Z",
    )


def fill():
    return ProviderFillEvidence.create(
        provider_execution_id="e1",
        client_order_id="c1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time="2026-09-24T18:00:00Z",
    )


def reconciliation(**overrides):
    values = dict(
        local_cash={"USD": "900"},
        provider_cash={"USD": "900"},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["e1"],
        provider_fills=[fill()],
        snapshot_consistency=snapshot(),
        coverage_start="2026-09-24T17:00:00Z",
        coverage_end="2026-09-24T19:00:00Z",
        pagination_complete=True,
    )
    values.update(overrides)
    return reconcile_account(**values)


class ReconciliationJournalTests(unittest.TestCase):
    def test_checkpoint_round_trip_is_exact_and_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            result = reconciliation(provider_cash={"USD": "899.50"})

            first = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            )
            second = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            )

            self.assertEqual(first["event_id"], second["event_id"])
            self.assertEqual(
                first["payload"]["cash_differences"]["USD"],
                "-0.50",
            )
            self.assertEqual(
                len(store.load_events("account_reconciliation", "acct-1")),
                1,
            )
            self.assertEqual(len(store.pending_outbox()), 1)

            reopened = JournalStore(path)
            latest = load_latest_reconciliation_checkpoint(
                reopened,
                reconciliation_id="acct-1",
            )
            self.assertEqual(latest["event_id"], first["event_id"])
            self.assertEqual(
                latest["payload"]["blocking_resources"],
                ["CASH:USD"],
            )

    def test_changed_checkpoint_appends_new_version(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=reconciliation(provider_cash={"USD": "899.50"}),
                observed_at="2026-09-24T19:00:00Z",
            )
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=reconciliation(),
                observed_at="2026-09-24T19:01:00Z",
            )
            events = store.load_events("account_reconciliation", "acct-1")
            self.assertEqual(
                [event["aggregate_version"] for event in events],
                [1, 2],
            )
            self.assertFalse(events[0]["payload"]["complete"])
            self.assertTrue(events[1]["payload"]["complete"])

    def test_unknown_dispatch_is_rebuilt_after_restart_without_resend(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(store)
            outbound_calls = []

            def authority_check(intent_hash, now):
                return True, "authorized"

            def ambiguous_transport(client_order_id, request, final_guard):
                outbound_calls.append((client_order_id, dict(request)))
                final_guard()
                raise TimeoutError("provider response lost")

            outcome = dispatcher.dispatch(
                attempt_id="attempt-1",
                intent_id="intent-1",
                intent_hash="sha256:intent",
                provider="provider-x",
                request={"side": "BUY", "quantity": "1"},
                now="2026-09-24T18:00:00Z",
                authority_check=authority_check,
                transport_send=ambiguous_transport,
            )
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(len(outbound_calls), 1)

            reopened = JournalStore(path)
            recovered = unknown_submissions_from_dispatch(
                reopened,
                attempt_ids=["attempt-1"],
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].attempt_id, "attempt-1")
            self.assertEqual(
                recovered[0].client_order_id,
                outcome.client_order_id,
            )
            self.assertEqual(len(outbound_calls), 1)

    def test_checkpoint_recovers_unresolved_attempt_ids(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            unknown = UnknownSubmission.create(
                attempt_id="attempt-unknown",
                client_order_id="client-unknown",
                started_at="2026-09-24T18:00:00Z",
            )
            result = reconciliation(
                unknown_submissions=[unknown],
                searched_client_order_ids=[],
            )
            self.assertFalse(result.complete)
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            )
            self.assertEqual(
                unresolved_attempt_ids_from_checkpoint(checkpoint),
                ("attempt-unknown",),
            )

    def test_activity_gap_checkpoint_survives_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            result = reconciliation(
                provider_activities=[
                    ProviderActivityEvidence.create(
                        activity_id="manual-cash-1",
                        activity_type="CASH_ADJUSTMENT",
                        origin="MANUAL",
                        occurred_at="2026-09-24T18:15:00Z",
                        currency="USD",
                    )
                ],
            )
            self.assertFalse(result.complete)
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-activity",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            )
            self.assertEqual(
                checkpoint["payload"]["unexpected_provider_activity_ids"],
                ["manual-cash-1"],
            )
            self.assertEqual(
                checkpoint["payload"]["manual_or_external_activity_ids"],
                ["manual-cash-1"],
            )
            self.assertEqual(
                unresolved_provider_activity_ids_from_checkpoint(checkpoint),
                ("manual-cash-1",),
            )

            reopened = JournalStore(path)
            latest = load_latest_reconciliation_checkpoint(
                reopened,
                reconciliation_id="acct-activity",
            )
            self.assertEqual(
                unresolved_provider_activity_ids_from_checkpoint(latest),
                ("manual-cash-1",),
            )
            self.assertIn(
                "CASH:USD",
                latest["payload"]["blocking_resources"],
            )

    def test_missing_expected_activity_is_recoverable_from_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            result = reconciliation(
                local_provider_activity_ids=["expected-activity"],
            )
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-missing-activity",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            )
            self.assertEqual(
                unresolved_provider_activity_ids_from_checkpoint(checkpoint),
                ("expected-activity",),
            )

    def test_activity_gap_recovery_validates_checkpoint_shape(self):
        with self.assertRaisesRegex(ValueError, "must be a list"):
            unresolved_provider_activity_ids_from_checkpoint(
                {
                    "payload": {
                        "unexpected_provider_activity_ids": "not-a-list",
                        "missing_local_provider_activity_ids": [],
                    }
                }
            )


    def test_corrupt_null_identities_fail_closed_in_checkpoint_recovery(self):
        malformed_submission = {
            "payload": {
                "submission_resolutions": [
                    {"attempt_id": None, "outcome": "UNKNOWN"}
                ]
            }
        }
        with self.assertRaises(ValueError):
            unresolved_attempt_ids_from_checkpoint(malformed_submission)

        malformed_activity = {
            "payload": {
                "unexpected_provider_activity_ids": [None],
                "missing_local_provider_activity_ids": [],
            }
        }
        with self.assertRaises(ValueError):
            unresolved_provider_activity_ids_from_checkpoint(malformed_activity)


if __name__ == "__main__":
    unittest.main()
