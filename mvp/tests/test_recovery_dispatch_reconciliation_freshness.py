from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery_dispatch import build_recovery_issued_dispatcher
from mvp.tests.test_reconciliation_journal import reconciliation


class RecoveryDispatchReconciliationFreshnessTests(unittest.TestCase):
    def _mark_ready(
        self,
        recovery: RecoveryController,
        journal: JournalStore,
        *,
        reconciliation_id: str,
        observed_at: str,
    ) -> None:
        owner = recovery.owner
        self.assertIsNotNone(owner)
        result = reconciliation(
            provider_id="BYBIT",
            account_id="acct",
            environment="PAPER",
        )
        record_reconciliation_checkpoint(
            journal,
            reconciliation_id=reconciliation_id,
            result=result,
            observed_at=observed_at,
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        recovery.record_reconciliation_checkpoint(
            reconciliation_id=reconciliation_id,
            provider_id="BYBIT",
            account_id="acct",
            environment="PAPER",
        )
        self.assertIs(recovery.state, HostState.READY)

    def test_effectful_send_requires_newer_reconciliation_before_next_wire(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            recovery = RecoveryController(
                owner_store=journal,
                owner_scope="PAPER:acct",
            )
            recovery.start("host-a")
            dispatcher = build_recovery_issued_dispatcher(
                recovery,
                journal,
                environment="PAPER",
                account_id="acct",
            )
            self._mark_ready(
                recovery,
                journal,
                reconciliation_id="ready-before-first-send",
                observed_at="2026-10-04T00:59:59Z",
            )

            first_wire: list[str] = []

            def first_transport(_client_order_id, _request, final_guard):
                final_guard()
                first_wire.append("wire")
                return {"ok": True}

            first = dispatcher.dispatch(
                attempt_id="attempt-first",
                intent_id="intent-first",
                intent_hash="sha256:" + "1" * 64,
                provider="BYBIT",
                request={"symbol": "BTCUSDT"},
                now="2026-10-04T01:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=first_transport,
            )
            self.assertEqual(first.status, "SENT")
            self.assertEqual(first_wire, ["wire"])
            self.assertIs(recovery.state, HostState.READY)
            self.assertTrue(recovery.provider_reconciled)

            with self.assertRaisesRegex(
                PermissionError,
                "durable reconciliation predates latest durable send state",
            ):
                dispatcher._require_durable_reconciliation_authority("BYBIT")

            second_wire: list[str] = []

            def second_transport(_client_order_id, _request, final_guard):
                final_guard()
                second_wire.append("wire")
                return {"ok": True}

            second = dispatcher.dispatch(
                attempt_id="attempt-stale-reconciliation",
                intent_id="intent-stale-reconciliation",
                intent_hash="sha256:" + "2" * 64,
                provider="BYBIT",
                request={"symbol": "ETHUSDT"},
                now="2026-10-04T01:00:01Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=second_transport,
            )
            self.assertEqual(second.status, "BLOCKED")
            self.assertEqual(
                second.reason,
                "sender_fence_rejected:PermissionError",
            )
            self.assertEqual(second_wire, [])
            inner = dispatcher._RecoveryIssuedDispatcher__dispatcher
            second_attempt_events = JournalStore.load_events(
                journal,
                "submission_attempt",
                inner._aggregate_id("attempt-stale-reconciliation"),
            )
            self.assertEqual(
                [event["event_type"] for event in second_attempt_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in second_attempt_events],
            )

            self._mark_ready(
                recovery,
                journal,
                reconciliation_id="ready-after-first-send",
                observed_at="2026-10-04T01:00:02Z",
            )

            third_wire: list[str] = []

            def third_transport(_client_order_id, _request, final_guard):
                final_guard()
                third_wire.append("wire")
                return {"ok": True}

            third = dispatcher.dispatch(
                attempt_id="attempt-after-rereconciliation",
                intent_id="intent-after-rereconciliation",
                intent_hash="sha256:" + "3" * 64,
                provider="BYBIT",
                request={"symbol": "SOLUSDT"},
                now="2026-10-04T01:00:03Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=third_transport,
            )
            self.assertEqual(third.status, "SENT")
            self.assertEqual(third_wire, ["wire"])


if __name__ == "__main__":
    unittest.main()
