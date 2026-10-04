from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp import recovery_takeover as takeover
from mvp.autotrade_mvp.sender_authority import (
    SenderAuthorityError,
    sender_authority_window,
)
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"takeover-lock-order-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


def _ready_reconciliation():
    snapshot = SnapshotConsistencyEvidence(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        mode="ATOMIC",
        query_started_at="2026-10-04T00:30:00Z",
        query_completed_at="2026-10-04T00:31:00Z",
    )
    fill = ProviderFillEvidence.create(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        provider_execution_id="exec-1",
        client_order_id="client-1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time="2026-10-04T00:30:30Z",
    )
    return reconcile_account(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        local_cash={"USD": "900"},
        provider_cash={"USD": "900"},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["exec-1"],
        provider_fills=[fill],
        snapshot_consistency=snapshot,
        coverage_start="2026-10-04T00:30:00Z",
        coverage_end="2026-10-04T00:31:00Z",
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="paper-1",
    )


class TakeoverLockOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = JournalStore(root / "journal.sqlite3")
        self.controller = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:paper-1",
        )
        self.controller.start("host-a")
        record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="takeover-ready",
            result=_ready_reconciliation(),
            observed_at="2026-10-04T00:31:00Z",
            host_id="host-a",
            owner_epoch="1",
        )
        self.controller.record_reconciliation_checkpoint(
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
        )
        self.assertEqual(self.controller.state, HostState.READY)
        self.vault = ProtectedCredentialVault(
            root / "credentials.json",
            protector=DeterministicProtector(),
        )
        self.handle = self.vault.register(
            handle_id="trade-credential",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="secret-v1",
        )

    def _takeover(self):
        return takeover.execute_durable_takeover(
            self.controller,
            new_owner_id="host-b",
            vault=self.vault,
            handle=self.handle,
            execution_identity="windows-user-1",
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
        )

    def test_old_sender_holding_vault_lease_can_reach_frozen_gate_without_deadlock(self):
        lease_acquired = Event()
        freeze_started = Event()
        allow_old_sender_to_guard = Event()
        old_sender_rejected = Event()
        original_append = takeover._append_takeover_event

        def observing_append(*args, **kwargs):
            event = original_append(*args, **kwargs)
            if kwargs.get("event_type") == "RecoveryTakeoverStarted":
                freeze_started.set()
            return event

        def old_sender():
            with self.vault.lease(
                self.handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ):
                lease_acquired.set()
                self.assertTrue(allow_old_sender_to_guard.wait(timeout=5))
                with self.assertRaisesRegex(
                    SenderAuthorityError, "pending durable takeover"
                ):
                    with sender_authority_window(
                        self.store,
                        owner_scope="PAPER:paper-1",
                    ):
                        self.fail("old sender must not cross durable freeze")
                old_sender_rejected.set()

        with patch.object(
            takeover,
            "_append_takeover_event",
            new=observing_append,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                old_future = pool.submit(old_sender)
                self.assertTrue(lease_acquired.wait(timeout=5))
                takeover_future = pool.submit(self._takeover)
                self.assertTrue(
                    freeze_started.wait(timeout=5),
                    "takeover must persist freeze without waiting for vault lease",
                )
                allow_old_sender_to_guard.set()
                self.assertTrue(
                    old_sender_rejected.wait(timeout=5),
                    "old sender must observe freeze and release credential lease",
                )
                old_future.result(timeout=5)
                result = takeover_future.result(timeout=5)

        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(result.target_owner.epoch, 2)
        self.assertEqual(self.controller.state, HostState.RECOVERING)
        self.assertEqual(
            [
                event["event_type"]
                for event in self.store.load_events_by_aggregate_type(
                    "recovery_takeover"
                )
            ],
            [
                "RecoveryTakeoverStarted",
                "RecoveryTakeoverEvidenceIssued",
                "RecoveryTakeoverOwnerCommitted",
            ],
        )
        with self.assertRaisesRegex(PermissionError, "unavailable"):
            self.vault.resolve(
                self.handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_reconciliation_older_than_confirmed_send_cannot_start_takeover(self):
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="host-a",
            owner_epoch=1,
        )

        def transport(_client_id, _request, final_guard):
            final_guard()
            return {"provider_order_id": "provider-1"}

        outcome = dispatcher.dispatch(
            attempt_id="sent-after-checkpoint",
            intent_id="intent-after-checkpoint",
            intent_hash="hash-after-checkpoint",
            provider="SIMULATED",
            request={},
            now="2026-10-04T00:32:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=transport,
            sender_check=self.controller.validate_sender,
        )
        self.assertEqual(outcome.status, "SENT")

        with self.assertRaisesRegex(
            takeover.DurableTakeoverError,
            "reconciliation predates latest durable send state",
        ):
            self._takeover()

        self.assertEqual(
            self.store.load_events_by_aggregate_type("recovery_takeover"),
            [],
        )
        self.assertEqual(
            self.vault.resolve(
                self.handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "secret-v1",
        )


if __name__ == "__main__":
    unittest.main()
