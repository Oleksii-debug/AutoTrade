from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
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
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.recovery_takeover import (
    DurableTakeoverError,
    execute_durable_takeover,
)
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"takeover-post-freeze-send-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


def _reconciliation():
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


class PostFreezeCompletedSendTests(unittest.TestCase):
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
        record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="takeover-ready",
            result=_reconciliation(),
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

    def _takeover(self):
        return execute_durable_takeover(
            self.controller,
            new_owner_id="host-b",
            vault=self.vault,
            handle=self.handle,
            execution_identity="windows-user-1",
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
        )

    def _append_completed_post_freeze_send(self) -> None:
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="host-a",
            owner_epoch=1,
        )
        attempt_id = "post-freeze-terminal-send"
        client_order_id = "post-freeze-client"
        now = "2026-10-04T00:32:00Z"
        dispatcher._append(
            attempt_id=attempt_id,
            event_type="SubmissionPrepared",
            version=1,
            payload={
                "attempt_id": attempt_id,
                "intent_id": "intent-post-freeze",
                "intent_hash": "intent-hash-post-freeze",
                "provider": "SIMULATED",
                "request_hash": "sha256:" + "0" * 64,
                "client_order_id": client_order_id,
                "environment": "PAPER",
                "account_id": "paper-1",
                "owner_token": "host-a",
                "owner_epoch": 1,
                "prepared_at": now,
            },
            now=now,
        )
        dispatcher._append(
            attempt_id=attempt_id,
            event_type="SubmissionSending",
            version=2,
            payload={
                "client_order_id": client_order_id,
                "owner_token": "host-a",
                "owner_epoch": 1,
            },
            now=now,
        )
        dispatcher._append(
            attempt_id=attempt_id,
            event_type="SubmissionSent",
            version=3,
            payload={
                "client_order_id": client_order_id,
                "response": {"accepted": True},
            },
            now=now,
        )

    def test_completed_send_after_takeover_freeze_permanently_blocks_owner_advance(self) -> None:
        with patch.object(
            self.controller,
            "_append_durable_owner",
            side_effect=RuntimeError("stop after takeover evidence"),
        ):
            with self.assertRaisesRegex(RuntimeError, "after takeover evidence"):
                self._takeover()

        self.assertEqual(
            [
                event["event_type"]
                for event in self.store.load_events_by_aggregate_type(
                    "recovery_takeover"
                )
            ],
            ["RecoveryTakeoverStarted", "RecoveryTakeoverEvidenceIssued"],
        )
        self._append_completed_post_freeze_send()

        with self.assertRaisesRegex(
            DurableTakeoverError,
            "durable submission state changed after takeover freeze",
        ):
            self._takeover()

        self.assertEqual(
            [(owner.owner_id, owner.epoch) for owner in self.controller.durable_owner_chain()],
            [("host-a", 1)],
        )
        self.assertEqual(
            [
                event["event_type"]
                for event in self.store.load_events_by_aggregate_type(
                    "recovery_takeover"
                )
            ],
            ["RecoveryTakeoverStarted", "RecoveryTakeoverEvidenceIssued"],
        )


if __name__ == "__main__":
    unittest.main()
