from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.recovery import HostState, OwnerFence, RecoveryController
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery_dispatch import (
    RecoveryIssuedDispatcher,
    build_recovery_issued_dispatcher,
)
from mvp.tests.test_reconciliation_journal import reconciliation


class RecoveryIssuedDispatcherTests(unittest.TestCase):
    def _journal(self, directory: str, name: str = "journal.sqlite3") -> JournalStore:
        return JournalStore(Path(directory) / name)

    def _recovery(self, journal: JournalStore) -> RecoveryController:
        recovery = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:acct",
        )
        recovery.start("host-a")
        return recovery

    def _mark_ready(
        self,
        recovery: RecoveryController,
        journal: JournalStore,
        *,
        provider_id: str = "BYBIT",
        reconciliation_id: str = "issued-dispatcher-ready",
    ) -> None:
        owner = recovery.owner
        self.assertIsNotNone(owner)
        result = reconciliation(
            provider_id=provider_id,
            account_id="acct",
            environment="PAPER",
        )
        record_reconciliation_checkpoint(
            journal,
            reconciliation_id=reconciliation_id,
            result=result,
            observed_at="2026-10-04T00:59:59Z",
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        recovery.record_reconciliation_checkpoint(
            reconciliation_id=reconciliation_id,
            provider_id=provider_id,
            account_id="acct",
            environment="PAPER",
        )
        self.assertIs(recovery.state, HostState.READY)

    def test_issued_dispatcher_ignores_hostile_legacy_sender_callback(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            recovery = self._recovery(journal)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            hostile_calls = []
            wire_calls = []

            def hostile_sender_check(_owner_id, _owner_epoch):
                hostile_calls.append("called")
                raise AssertionError(
                    "legacy sender callback must not replace recovery authority"
                )

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                raise AssertionError("wire must remain blocked while recovery is not READY")

            outcome = dispatcher.dispatch(
                attempt_id="attempt-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T01:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport_send,
                sender_check=hostile_sender_check,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertIn("sender_fence_rejected:PermissionError", outcome.reason)
            self.assertEqual(hostile_calls, [])
            self.assertEqual(wire_calls, [])
            events = JournalStore.load_events(
                journal,
                "submission_attempt",
                dispatcher._RecoveryIssuedDispatcher__dispatcher._aggregate_id(
                    "attempt-1"
                ),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn("SubmissionSending", [event["event_type"] for event in events])

    def test_process_local_ready_flags_without_journal_reconciliation_cannot_send(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            recovery = self._recovery(journal)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )

            # Process-local flags are not financial authority. A production-issued
            # sender must still recover a current owner-bound AccountReconciled
            # checkpoint from the canonical JournalStore at the final barrier.
            recovery.provider_reconciled = True
            recovery.reason_codes.clear()
            recovery.state = HostState.READY
            wire_calls = []

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"ok": True}

            outcome = dispatcher.dispatch(
                attempt_id="attempt-forged-ready",
                intent_id="intent-forged-ready",
                intent_hash="intent-hash-forged-ready",
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T01:00:01Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport_send,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertIn("sender_fence_rejected:PermissionError", outcome.reason)
            self.assertEqual(wire_calls, [])
            events = JournalStore.load_events_by_aggregate_type(
                journal,
                "submission_attempt",
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in events],
            )

    def test_durable_mutation_after_reconciliation_check_blocks_final_send_cas(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            recovery = self._recovery(journal)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            self._mark_ready(recovery, journal)

            authority_calls = 0
            wire_calls = []

            def authority_check(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                if authority_calls == 2:
                    payload = {"reason": "concurrent-financial-authority-change"}
                    JournalStore.append_event(
                        journal,
                        {
                            "event_id": str(uuid4()),
                            "event_type": "ConcurrentFinancialAuthorityChanged",
                            "aggregate_type": "financial_control",
                            "aggregate_id": "PAPER:acct",
                            "aggregate_version": "1",
                            "payload": payload,
                            "payload_hash": payload_digest(payload),
                            "committed_at": "2026-10-04T01:00:00Z",
                        },
                    )
                return True, "allowed"

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"ok": True}

            outcome = dispatcher.dispatch(
                attempt_id="attempt-cas-race",
                intent_id="intent-cas-race",
                intent_hash="intent-hash-cas-race",
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T01:00:00Z",
                authority_check=authority_check,
                transport_send=transport_send,
            )

            self.assertEqual(authority_calls, 2)
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "journal_changed_during_final_send_validation",
            )
            self.assertEqual(wire_calls, [])
            inner = dispatcher._RecoveryIssuedDispatcher__dispatcher
            events = JournalStore.load_events(
                journal,
                "submission_attempt",
                inner._aggregate_id("attempt-cas-race"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in events],
            )

    def test_same_process_unknown_reopens_reconciliation_gate_before_next_send(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            recovery = self._recovery(journal)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            self._mark_ready(recovery, journal)

            first_wire = []

            def ambiguous_transport(_client_order_id, _request, final_guard):
                final_guard()
                first_wire.append("wire")
                raise RuntimeError("ambiguous provider timeout")

            first = dispatcher.dispatch(
                attempt_id="attempt-unknown-1",
                intent_id="intent-unknown-1",
                intent_hash="intent-hash-unknown-1",
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T01:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=ambiguous_transport,
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(first_wire, ["wire"])
            # GuardedDispatcher owns durable send chronology; the production
            # facade must immediately reflect the durable ambiguity into
            # recovery readiness, before any later order is attempted.
            self.assertIs(recovery.state, HostState.DEGRADED)
            self.assertFalse(recovery.provider_reconciled)
            self.assertIn("attempt-unknown-1", recovery.unresolved_attempts)

            second_wire = []

            def second_transport(_client_order_id, _request, final_guard):
                final_guard()
                second_wire.append("wire")
                return {"ok": True}

            second = dispatcher.dispatch(
                attempt_id="attempt-unknown-2",
                intent_id="intent-unknown-2",
                intent_hash="intent-hash-unknown-2",
                provider="BYBIT",
                request={"symbol": "ETHUSDT"},
                now="2026-10-04T01:00:01Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=second_transport,
            )
            self.assertEqual(second.status, "BLOCKED")
            self.assertIn("sender_fence_rejected:PermissionError", second.reason)
            self.assertEqual(second_wire, [])
            self.assertIs(recovery.state, HostState.DEGRADED)
            self.assertFalse(recovery.provider_reconciled)
            self.assertIn("attempt-unknown-1", recovery.unresolved_attempts)
            inner = dispatcher._RecoveryIssuedDispatcher__dispatcher
            first_events = JournalStore.load_events(
                journal,
                "submission_attempt",
                inner._aggregate_id("attempt-unknown-1"),
            )
            second_events = JournalStore.load_events(
                journal,
                "submission_attempt",
                inner._aggregate_id("attempt-unknown-2"),
            )
            self.assertEqual(
                [event["event_type"] for event in first_events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertEqual(
                [event["event_type"] for event in second_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_final_guard_rereads_durable_owner_and_blocks_stale_issued_dispatcher(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            recovery = self._recovery(journal)
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )

            # Establish real owner-bound journal readiness first so this test
            # isolates stale durable owner authority rather than missing evidence.
            self._mark_ready(recovery, journal)
            successor_payload = {
                "owner_id": "host-b",
                "owner_epoch": "2",
            }
            JournalStore.append_event(
                journal,
                {
                    "event_id": str(uuid4()),
                    "event_type": "RecoveryOwnerChanged",
                    "aggregate_type": "recovery_owner",
                    "aggregate_id": "PAPER:acct",
                    "aggregate_version": "2",
                    "payload": successor_payload,
                    "payload_hash": payload_digest(successor_payload),
                    "committed_at": "2026-10-04T01:00:01Z",
                },
            )
            wire_calls = []
            hostile_calls = []

            def hostile_allow_all(_owner_id, _owner_epoch):
                hostile_calls.append("called")

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"ok": True}

            outcome = dispatcher.dispatch(
                attempt_id="attempt-stale",
                intent_id="intent-stale",
                intent_hash="intent-hash-stale",
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T01:00:02Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport_send,
                sender_check=hostile_allow_all,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertIn("sender_fence_rejected:PermissionError", outcome.reason)
            self.assertEqual(hostile_calls, [])
            self.assertEqual(wire_calls, [])

    def test_factory_requires_exact_recovery_store_and_scope(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            foreign = self._journal(directory, "foreign.sqlite3")
            recovery = self._recovery(journal)

            with self.assertRaisesRegex(
                PermissionError,
                "exact recovery owner store",
            ):
                build_recovery_issued_dispatcher(
                    recovery,
                    foreign,
                    environment="PAPER",
                    account_id="acct",
                )

            with self.assertRaisesRegex(
                PermissionError,
                "scope does not match",
            ):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id="other",
                )

    def test_factory_requires_current_durable_owner(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            recovery = RecoveryController(
                owner_store=journal,
                owner_scope="PAPER:acct",
            )
            with self.assertRaisesRegex(
                PermissionError,
                "no active durable owner",
            ):
                build_recovery_issued_dispatcher(
                    recovery,
                    journal,
                    environment="PAPER",
                    account_id="acct",
                )

    def test_facade_cannot_be_directly_minted_with_caller_token(self):
        with TemporaryDirectory() as directory:
            journal = self._journal(directory)
            recovery = self._recovery(journal)
            owner = recovery.owner
            self.assertIsInstance(owner, OwnerFence)
            with self.assertRaisesRegex(
                PermissionError,
                "minted by canonical composition",
            ):
                RecoveryIssuedDispatcher(
                    recovery=recovery,
                    store=journal,
                    environment="PAPER",
                    account_id="acct",
                    owner=owner,
                    prepared_lease_seconds=60,
                    issuance_token=object(),
                )


if __name__ == "__main__":
    unittest.main()
