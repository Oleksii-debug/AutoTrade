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


class DeterministicProtector:
    PREFIX = b"takeover-anchor-rebinding-v1:"

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
        provider_execution_id="exec-anchor-rebind",
        client_order_id="client-anchor-rebind",
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
        local_execution_ids=["exec-anchor-rebind"],
        provider_fills=[fill],
        snapshot_consistency=snapshot,
        coverage_start="2026-10-04T00:30:00Z",
        coverage_end="2026-10-04T00:31:00Z",
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="paper-1",
    )


class RecoveryTakeoverCredentialAnchorRebindingAuthorityTests(unittest.TestCase):
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

    def test_public_credential_anchor_verifier_rebind_is_never_executed(self) -> None:
        installed = takeover_module.require_current_trade_credential_transition_anchor
        calls = []

        def rebound_verifier(store, vault, receipt):
            calls.append(receipt.receipt_id)
            return installed(store, vault, receipt)

        with patch.object(
            takeover_module,
            "require_current_trade_credential_transition_anchor",
            new=rebound_verifier,
        ):
            result = self._execute()

        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(calls, [])

    def test_public_takeover_authority_window_rebind_is_never_executed(self) -> None:
        installed = takeover_module.takeover_authority_window
        calls = []

        def rebound_window(*args, **kwargs):
            calls.append((args, kwargs))
            return installed(*args, **kwargs)

        with patch.object(
            takeover_module,
            "takeover_authority_window",
            new=rebound_window,
        ):
            result = self._execute()

        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(
            calls,
            [],
            "takeover exclusion must not dispatch through a rebound public window",
        )


if __name__ == "__main__":
    unittest.main()
