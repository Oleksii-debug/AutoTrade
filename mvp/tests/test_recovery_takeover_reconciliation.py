from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.recovery_takeover import execute_durable_takeover
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"takeover-reconcile-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


def _snapshot(*, start: str, end: str) -> SnapshotConsistencyEvidence:
    return SnapshotConsistencyEvidence(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        mode="ATOMIC",
        query_started_at=start,
        query_completed_at=end,
    )


def _result(
    *,
    client_order_id: str = "client-initial",
    unknown_submissions=(),
    start: str = "2026-10-04T00:30:00Z",
    end: str = "2026-10-04T00:31:00Z",
):
    fill = ProviderFillEvidence.create(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        provider_execution_id="exec-1",
        client_order_id=client_order_id,
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time=start,
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
        unknown_submissions=list(unknown_submissions),
        snapshot_consistency=_snapshot(start=start, end=end),
        coverage_start=start,
        coverage_end=end,
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="paper-1",
    )


class TakeoverReconciliationResolutionTests(unittest.TestCase):
    def test_new_owner_bound_checkpoint_can_terminally_resolve_recovered_unknown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:paper-1",
            )
            controller.start("host-a")
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="takeover-ready",
                result=_result(),
                observed_at="2026-10-04T00:31:00Z",
                host_id="host-a",
                owner_epoch="1",
            )
            controller.record_reconciliation_checkpoint(
                reconciliation_id="takeover-ready",
                provider_id="SIMULATED",
                account_id="paper-1",
                environment="PAPER",
            )
            self.assertEqual(controller.state, HostState.READY)

            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="paper-1",
                owner_token="host-a",
                owner_epoch=1,
            )

            def ambiguous_transport(_client_id, _request, final_guard):
                final_guard()
                raise TimeoutError("provider reply lost")

            outcome = dispatcher.dispatch(
                attempt_id="attempt-resolved-before-takeover",
                intent_id="intent-resolved-before-takeover",
                intent_hash="intent-hash-resolved-before-takeover",
                provider="SIMULATED",
                request={},
                now="2026-10-04T00:32:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=ambiguous_transport,
                sender_check=controller.validate_sender,
            )
            self.assertEqual(outcome.status, "UNKNOWN")

            unknown = UnknownSubmission.create(
                attempt_id="attempt-resolved-before-takeover",
                intent_id="intent-resolved-before-takeover",
                provider_id="SIMULATED",
                account_id="paper-1",
                environment="PAPER",
                client_order_id=outcome.client_order_id,
                started_at="2026-10-04T00:32:00Z",
            )
            resolved = _result(
                client_order_id=outcome.client_order_id,
                unknown_submissions=(unknown,),
                start="2026-10-04T00:32:30Z",
                end="2026-10-04T00:33:00Z",
            )
            self.assertTrue(resolved.complete)
            self.assertEqual(
                resolved.submission_resolutions[0].outcome,
                "OBSERVED_EXECUTION",
            )
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="takeover-ready",
                result=resolved,
                observed_at="2026-10-04T00:33:00Z",
                host_id="host-a",
                owner_epoch="1",
            )

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
                reconciliation_id="takeover-ready",
                provider_id="SIMULATED",
            )

            self.assertEqual(result.target_owner.owner_id, "host-b")
            self.assertEqual(result.target_owner.epoch, 2)
            self.assertEqual(controller.unresolved_attempts, set())
            self.assertEqual(controller.state, HostState.RECOVERING)
            self.assertFalse(controller.provider_reconciled)


if __name__ == "__main__":
    unittest.main()
