"""#1046 fail-closed atomic durable-UNKNOWN recovery projection regression.

A malformed later submission aggregate must not leave earlier aggregate state
partially installed in a RecoveryController.  The canonical journal remains the
only authority; projection is all-or-none for one scoped scan.
"""
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


class AtomicDurableUnknownProjectionTests(unittest.TestCase):
    @staticmethod
    def _dispatch_unknown(store: JournalStore, *, attempt_id: str, intent_id: str) -> None:
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="sender-a",
            owner_epoch=1,
        )

        def ambiguous(_client_order_id, _request, final_guard):
            final_guard()
            raise TimeoutError("ambiguous provider result")

        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash="hash-" + attempt_id,
            provider="SIM",
            request={"side": "BUY"},
            now="2026-09-30T18:45:00Z",
            authority_check=lambda _intent_hash, _now: (True, "allowed"),
            transport_send=ambiguous,
        )
        if outcome.status != "UNKNOWN":
            raise AssertionError("fixture must produce durable UNKNOWN")

    @staticmethod
    def _append_invalid_tail(store: JournalStore, *, aggregate_id: str) -> None:
        payload = {"reason": "synthetic-invalid-tail"}
        store.append_event(
            {
                "event_id": "invalid-tail-event",
                "event_type": "SubmissionUnexpected",
                "aggregate_type": "submission_attempt",
                "aggregate_id": aggregate_id,
                "aggregate_version": "4",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-30T18:45:01Z",
                "owner_epoch": "1",
            }
        )

    def test_malformed_later_aggregate_cannot_partially_mutate_recovery_projection(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            self._dispatch_unknown(
                store,
                attempt_id="attempt-a-valid",
                intent_id="intent-a-valid",
            )
            self._dispatch_unknown(
                store,
                attempt_id="attempt-b-corrupt",
                intent_id="intent-b-corrupt",
            )

            events = store.load_events_by_aggregate_type("submission_attempt")
            aggregate_b = None
            for event in events:
                payload = event.get("payload")
                if (
                    event.get("event_type") == "SubmissionPrepared"
                    and isinstance(payload, dict)
                    and payload.get("attempt_id") == "attempt-b-corrupt"
                ):
                    aggregate_b = event["aggregate_id"]
                    break
            self.assertIsNotNone(aggregate_b)
            self._append_invalid_tail(store, aggregate_id=aggregate_b)

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            self.assertEqual(recovery.state, HostState.STOPPED)
            self.assertEqual(recovery.unresolved_attempts, set())

            with self.assertRaisesRegex(
                RuntimeError,
                "transition sequence is invalid",
            ):
                recovery.recover_durable_submission_uncertainty(
                    environment="SIMULATION",
                    account_id="acct",
                )

            # Projection is a single authority read.  A valid aggregate visited
            # before the malformed aggregate must not survive a failed scan as
            # process-local financial/recovery authority.
            self.assertEqual(recovery.state, HostState.STOPPED)
            self.assertEqual(recovery.unresolved_attempts, set())
            self.assertEqual(recovery._unresolved_send_attempts, set())
            self.assertEqual(recovery._unresolved_send_bindings, {})
            self.assertEqual(recovery._recovered_unknown_identities, {})
            self.assertNotIn("provider_uncertainty", recovery.reason_codes)

    def test_foreign_scope_projection_is_rejected_without_state_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            self._dispatch_unknown(
                store,
                attempt_id="attempt-a",
                intent_id="intent-a",
            )
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            before = (
                recovery.state,
                set(recovery.unresolved_attempts),
                set(recovery._unresolved_send_attempts),
                dict(recovery._unresolved_send_bindings),
                dict(recovery._recovered_unknown_identities),
                set(recovery.reason_codes),
                recovery.provider_reconciled,
            )
            with self.assertRaisesRegex(
                PermissionError,
                "scope does not match durable recovery owner scope",
            ):
                recovery.recover_durable_submission_uncertainty(
                    environment="SIMULATION",
                    account_id="foreign-acct",
                )
            after = (
                recovery.state,
                set(recovery.unresolved_attempts),
                set(recovery._unresolved_send_attempts),
                dict(recovery._unresolved_send_bindings),
                dict(recovery._recovered_unknown_identities),
                set(recovery.reason_codes),
                recovery.provider_reconciled,
            )
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
