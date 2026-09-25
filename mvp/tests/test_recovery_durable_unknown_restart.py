from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


class DurableUnknownRestartTests(unittest.TestCase):
    def _unknown_dispatch(self, store: JournalStore, *, attempt_id: str = "attempt-1"):
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="sender-a",
            owner_epoch=1,
        )

        def transport(_client_id, _request, final_guard):
            final_guard()
            raise TimeoutError("ambiguous provider result")

        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="intent-hash-1",
            provider="sim",
            request={"side": "BUY"},
            now="2026-09-25T20:00:00Z",
            authority_check=lambda _intent_hash, _now: (True, "allowed"),
            transport_send=transport,
        )
        self.assertEqual(outcome.status, "UNKNOWN")
        return outcome

    def test_restart_rebuilds_durable_unknown_before_ready(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            self._unknown_dispatch(store)

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-restarted")

            self.assertEqual(owner.epoch, 1)
            self.assertEqual(recovery.state, HostState.DEGRADED)
            self.assertEqual(recovery.unresolved_attempts, {"attempt-1"})
            self.assertIn("provider_uncertainty", recovery.reason_codes)
            with self.assertRaisesRegex(PermissionError, "not ready|unresolved"):
                recovery.validate_admission(owner.epoch)

    def test_completed_send_is_not_recovered_as_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="sender-a",
                owner_epoch=1,
            )

            def transport(_client_id, _request, final_guard):
                final_guard()
                return {"provider_order_id": "provider-order-1"}

            outcome = dispatcher.dispatch(
                attempt_id="attempt-sent",
                intent_id="intent-sent",
                intent_hash="intent-hash-sent",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-25T20:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            self.assertEqual(outcome.status, "SENT")

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-restarted")

            self.assertEqual(recovery.unresolved_attempts, set())
            self.assertEqual(recovery.state, HostState.RECOVERING)

    def test_exact_reconciliation_terminal_verdict_clears_recovered_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            outcome = self._unknown_dispatch(store)
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-restarted")
            self.assertEqual(recovery.unresolved_attempts, {"attempt-1"})

            checkpoint = {
                "event_id": "reconciliation-event-1",
                "payload_hash": "sha256:" + "a" * 64,
                "journal_sequence": 99,
                "payload": {
                    "provider_id": "SIM",
                    "account_id": "acct",
                    "environment": "SIMULATION",
                    "complete": True,
                    "snapshot_consistent": True,
                    "activity_coverage_complete": True,
                    "blocking_resources": [],
                    "submission_resolutions": [
                        {
                            "attempt_id": "attempt-1",
                            "intent_id": "intent-1",
                            "client_order_id": outcome.client_order_id,
                            "outcome": "PROVEN_ABSENT",
                            "evidence_reason": "complete provider absence proof",
                            "provider_order_ids": [],
                            "provider_execution_ids": [],
                        }
                    ],
                },
            }
            with patch(
                "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness",
                return_value=checkpoint,
            ):
                evidence = recovery.record_reconciliation_checkpoint(
                    reconciliation_id="recon-1",
                    provider_id="SIM",
                    account_id="acct",
                    environment="SIMULATION",
                )

            self.assertEqual(evidence["event_id"], "reconciliation-event-1")
            self.assertEqual(recovery.unresolved_attempts, set())
            self.assertEqual(recovery.state, HostState.READY)
            recovery.validate_admission(owner.epoch)

    def test_reconciliation_identity_mismatch_cannot_clear_recovered_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            outcome = self._unknown_dispatch(store)
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-restarted")

            checkpoint = {
                "event_id": "reconciliation-event-bad",
                "payload_hash": "sha256:" + "b" * 64,
                "journal_sequence": 100,
                "payload": {
                    "provider_id": "SIM",
                    "account_id": "acct",
                    "environment": "SIMULATION",
                    "complete": True,
                    "snapshot_consistent": True,
                    "activity_coverage_complete": True,
                    "blocking_resources": [],
                    "submission_resolutions": [
                        {
                            "attempt_id": "attempt-1",
                            "intent_id": "different-intent",
                            "client_order_id": outcome.client_order_id,
                            "outcome": "PROVEN_ABSENT",
                            "evidence_reason": "forged identity",
                            "provider_order_ids": [],
                            "provider_execution_ids": [],
                        }
                    ],
                },
            }
            with patch(
                "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness",
                return_value=checkpoint,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "identity conflicts with recovered submission",
                ):
                    recovery.record_reconciliation_checkpoint(
                        reconciliation_id="recon-bad",
                        provider_id="SIM",
                        account_id="acct",
                        environment="SIMULATION",
                    )

            self.assertEqual(recovery.unresolved_attempts, {"attempt-1"})
            self.assertEqual(recovery.state, HostState.DEGRADED)


if __name__ == "__main__":
    unittest.main()
