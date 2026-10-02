from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    _issue_recovery_guarded_dispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


class RecoverySenderIssuanceTests(unittest.TestCase):
    def _mark_ready(self, recovery: RecoveryController) -> None:
        checkpoint = {
            "event_id": "ready-event",
            "payload_hash": "sha256:" + "a" * 64,
            "journal_sequence": 1,
            "payload": {
                "provider_id": "SIM",
                "account_id": "acct",
                "environment": "SIMULATION",
                "complete": True,
                "snapshot_consistent": True,
                "activity_coverage_complete": True,
                "blocking_resources": [],
                "submission_resolutions": [],
            },
        }
        with patch(
            "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness",
            return_value=checkpoint,
        ):
            recovery.record_reconciliation_checkpoint(
                reconciliation_id="ready",
                provider_id="SIM",
                account_id="acct",
                environment="SIMULATION",
            )
        self.assertEqual(recovery.state, HostState.READY)

    def test_public_dispatcher_constructor_cannot_mint_bound_sender_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(
                PermissionError,
                "issued by RecoveryController",
            ):
                GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="caller",
                    owner_epoch=1,
                    bound_sender_check=lambda _owner, _epoch: None,
                )

    def test_direct_recovery_dispatcher_issuer_rejects_caller_sender_callback(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact RecoveryController"):
                _issue_recovery_guarded_dispatcher(
                    lambda _owner, _epoch: None,
                    store,
                    environment="PAPER",
                    account_id="acct",
                    prepared_lease_seconds=60,
                )

    def test_paper_live_per_call_sender_callback_cannot_replace_product_issuer(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment=environment,
                    account_id="acct",
                    owner_token="caller",
                    owner_epoch=1,
                )
                financial = AuthorityService(store).dispatch_guard(
                    "missing-admission",
                    account_id="acct",
                    environment=environment,
                    instrument_id="11111111-1111-4111-8111-111111111111",
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                )
                wire_calls = []
                with self.assertRaisesRegex(
                    PermissionError,
                    "sender authority must be issued by RecoveryController",
                ):
                    dispatcher.dispatch(
                        attempt_id=f"caller-sender-{environment.lower()}",
                        intent_id="intent",
                        intent_hash="hash",
                        provider="provider",
                        request={},
                        now="2026-10-02T05:55:00Z",
                        authority_check=financial,
                        transport_send=lambda *_args: wire_calls.append("wire"),
                        sender_check=lambda _owner, _epoch: None,
                    )
                self.assertEqual(wire_calls, [])
                self.assertEqual(
                    store.load_events_by_aggregate_type("submission_attempt"),
                    [],
                )

    def test_recovery_dispatcher_ignores_per_call_sender_replacement(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-a")
            self._mark_ready(recovery)

            dispatcher = recovery.build_guarded_dispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
            )
            recovery.on_lease_expired()
            self.assertEqual(recovery.state, HostState.DEGRADED)

            bypass_calls = []
            wire_calls = []

            def bypass(_owner, _epoch):
                bypass_calls.append("called")

            def transport(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "must-not-send"}

            outcome = dispatcher.dispatch(
                attempt_id="replacement-attempt",
                intent_id="replacement-intent",
                intent_hash="replacement-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-10-02T05:55:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=bypass,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(bypass_calls, [])
            self.assertEqual(wire_calls, [])
            events = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_post_mint_bound_sender_replacement_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-a")
            self._mark_ready(recovery)
            dispatcher = recovery.build_guarded_dispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
            )
            dispatcher._bound_sender_check = lambda _owner, _epoch: None
            wire_calls = []

            def transport(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "must-not-send"}

            outcome = dispatcher.dispatch(
                attempt_id="mutated-bound-attempt",
                intent_id="mutated-bound-intent",
                intent_hash="mutated-bound-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-10-02T05:55:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(wire_calls, [])

    def test_recovery_issued_sender_allows_current_ready_owner(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-a")
            self._mark_ready(recovery)
            dispatcher = recovery.build_guarded_dispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
            )
            wire_calls = []

            def transport(_client_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"provider_order_id": "provider-order-1"}

            outcome = dispatcher.dispatch(
                attempt_id="issued-positive-attempt",
                intent_id="issued-positive-intent",
                intent_hash="issued-positive-hash",
                provider="sim",
                request={"side": "BUY"},
                now="2026-10-02T05:55:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(wire_calls, ["wire"])

    def test_recovery_subclass_cannot_mint_product_sender_authority(self):
        class ForgedRecovery(RecoveryController):
            pass

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = ForgedRecovery(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-a")
            with self.assertRaisesRegex(TypeError, "exact RecoveryController"):
                recovery.build_guarded_dispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                )


if __name__ == "__main__":
    unittest.main()
