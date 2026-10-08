from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.recovery_takeover import execute_durable_takeover
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"takeover-stale-sender-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        prefix = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(prefix):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(prefix):][::-1]


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
        local_execution_ids=[],
        provider_fills=[],
        snapshot_consistency=snapshot,
        coverage_start="2026-10-04T00:30:00Z",
        coverage_end="2026-10-04T00:31:00Z",
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="paper-1",
    )


class CompletedTakeoverStaleSenderTests(unittest.TestCase):
    def test_completed_takeover_reopens_gate_but_old_shared_journal_sender_emits_zero_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:paper-1",
            )
            controller.start("host-a")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="ready",
                result=_reconciliation(),
                observed_at="2026-10-04T00:31:00Z",
                host_id="host-a",
                owner_epoch="1",
            )
            controller.record_reconciliation_checkpoint(
                reconciliation_id="ready",
                provider_id="SIMULATED",
                account_id="paper-1",
                environment="PAPER",
            )
            self.assertEqual(checkpoint["event_type"], "AccountReconciled")

            vault = ProtectedCredentialVault(
                root / "credentials.json",
                protector=DeterministicProtector(),
            )
            handle = vault.register(
                handle_id="trade-credential",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="secret-v1",
            )
            result = execute_durable_takeover(
                controller,
                new_owner_id="host-b",
                vault=vault,
                handle=handle,
                execution_identity="windows-user-1",
                reconciliation_id="ready",
                provider_id="SIMULATED",
            )
            self.assertEqual(
                (result.target_owner.owner_id, result.target_owner.epoch),
                ("host-b", 2),
            )

            stale_dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="paper-1",
                owner_token="host-a",
                owner_epoch=1,
            )
            wire_calls: list[str] = []

            def transport(client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append(client_order_id)
                return {"status": "accepted"}

            outcome = stale_dispatcher.dispatch(
                attempt_id="stale-after-takeover",
                intent_id="intent-stale-after-takeover",
                intent_hash="hash-stale-after-takeover",
                provider="SIMULATED",
                request={},
                now="2026-10-04T00:32:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=controller.validate_sender,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(wire_calls, [])
            aggregate_id = submission_attempt_aggregate_id(
                environment="PAPER",
                account_id="paper-1",
                attempt_id="stale-after-takeover",
            )
            attempt = store.load_events("submission_attempt", aggregate_id)
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in attempt],
            )


if __name__ == "__main__":
    unittest.main()
