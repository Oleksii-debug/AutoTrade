from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import recovery_takeover as takeover_module
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


class _Protector:
    PREFIX = b"takeover-helper-rebinding-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected) :][::-1]


def _reconciliation():
    snapshot = SnapshotConsistencyEvidence(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        mode="ATOMIC",
        query_started_at="2026-10-05T01:10:00Z",
        query_completed_at="2026-10-05T01:11:00Z",
    )
    fill = ProviderFillEvidence.create(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        provider_execution_id="exec-helper-rebinding",
        client_order_id="client-helper-rebinding",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time="2026-10-05T01:10:30Z",
    )
    return reconcile_account(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        local_cash={"USD": "900"},
        provider_cash={"USD": "900"},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["exec-helper-rebinding"],
        provider_fills=[fill],
        snapshot_consistency=snapshot,
        coverage_start="2026-10-05T01:10:00Z",
        coverage_end="2026-10-05T01:11:00Z",
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="paper-1",
    )


class RecoveryTakeoverHelperRebindingAuthorityTests(unittest.TestCase):
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
            protector=_Protector(),
        )
        self.handle = self.vault.register(
            handle_id="trade-credential",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="test-value-v1",
        )
        record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="takeover-ready",
            result=_reconciliation(),
            observed_at="2026-10-05T01:11:00Z",
            host_id="host-a",
            owner_epoch="1",
        )
        self.controller.record_reconciliation_checkpoint(
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
        )

    def _execute(self):
        return execute_durable_takeover(
            self.controller,
            new_owner_id="host-b",
            vault=self.vault,
            handle=self.handle,
            execution_identity="windows-user-1",
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
        )

    def _assert_module_helper_rebind_is_ignored(self, attribute: str) -> None:
        calls = []

        def rebound(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(
                f"pre-call rebound takeover helper executed: {attribute}"
            )

        with patch.object(takeover_module, attribute, new=rebound):
            result = self._execute()

        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(calls, [])

    def _assert_controller_method_rebind_is_ignored(self, attribute: str) -> None:
        calls = []

        def rebound(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(
                f"pre-call rebound RecoveryController authority executed: {attribute}"
            )

        with patch.object(RecoveryController, attribute, new=rebound):
            result = self._execute()

        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(calls, [])

    def test_common_inputs_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_common_inputs")

    def test_pending_scope_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_pending_for_scope")

    def test_reconciliation_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_require_reconciliation")

    def test_effectful_sequence_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored(
            "_latest_effectful_submission_sequence"
        )

    def test_checkpoint_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_require_checkpoint_event")

    def test_vault_snapshot_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_vault_snapshot")

    def test_receipt_match_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_require_receipt_for_started")

    def test_seal_issuer_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_seal_evidence")

    def test_seal_verifier_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_verify_evidence_seal")

    def test_owner_event_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_owner_event")

    def test_controller_owner_binding_helper_rebind_before_call_is_never_executed(self) -> None:
        self._assert_module_helper_rebind_is_ignored("_bind_controller_owner")

    def test_controller_latest_owner_rebind_before_call_is_never_executed(self) -> None:
        self._assert_controller_method_rebind_is_ignored("_latest_durable_owner")

    def test_controller_append_owner_rebind_before_call_is_never_executed(self) -> None:
        self._assert_controller_method_rebind_is_ignored("_append_durable_owner")

    def test_controller_uncertainty_recovery_rebind_before_call_is_never_executed(self) -> None:
        self._assert_controller_method_rebind_is_ignored(
            "_recover_scoped_submission_uncertainty_from_owner_scope"
        )

    def test_controller_reconciliation_rebind_before_call_is_never_executed(self) -> None:
        self._assert_controller_method_rebind_is_ignored(
            "record_reconciliation_checkpoint"
        )


if __name__ == "__main__":
    unittest.main()
