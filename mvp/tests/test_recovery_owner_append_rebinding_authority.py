from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import recovery_takeover as takeover_module
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import OwnerFence, RecoveryController
from mvp.autotrade_mvp.recovery_takeover import (
    DurableTakeoverError,
    execute_durable_takeover,
)
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"owner-append-rebinding-test-v1:"

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


class RecoveryOwnerAppendRebindingAuthorityTests(unittest.TestCase):
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

    def _anchor_injector(self, *, event_id: str, aggregate_id: str):
        original_append = JournalStore.append_event
        original_require_anchor = (
            takeover_module.require_current_trade_credential_transition_anchor
        )
        injected = False

        def require_anchor_and_inject(store, vault, receipt):
            nonlocal injected
            anchor = original_require_anchor(store, vault, receipt)
            if not injected:
                injected = True
                payload = {"reason": aggregate_id}
                original_append(
                    store,
                    {
                        "event_id": event_id,
                        "event_type": "OwnerCutRaceProbe",
                        "aggregate_type": "test_probe",
                        "aggregate_id": aggregate_id,
                        "aggregate_version": "1",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": "2026-10-04T20:00:00Z",
                    },
                )
            return anchor

        return require_anchor_and_inject

    def test_rebound_owner_chain_read_cannot_hide_durable_owner(self) -> None:
        with patch.object(JournalStore, "load_events", return_value=[]):
            self.assertEqual(
                self.controller.durable_owner_chain(),
                (OwnerFence("host-a", 1),),
            )

    def test_rebound_append_event_cannot_strip_owner_commit_journal_cut(self) -> None:
        original_append = JournalStore.append_event

        def append_event_without_owner_cut(store, envelope, *args, **kwargs):
            if envelope.get("event_type") == "RecoveryOwnerChanged":
                kwargs.pop("expected_journal_sequence", None)
            return original_append(store, envelope, *args, **kwargs)

        with patch.object(
            JournalStore,
            "append_event",
            new=append_event_without_owner_cut,
        ), patch(
            "mvp.autotrade_mvp.recovery_takeover.require_current_trade_credential_transition_anchor",
            new=self._anchor_injector(
                event_id="owner-append-rebinding-race-1",
                aggregate_id="owner-append-rebinding-race",
            ),
        ):
            with self.assertRaisesRegex(
                DurableTakeoverError,
                "journal changed during takeover owner validation",
            ):
                self._takeover()

        self.assertEqual(
            [
                (owner.owner_id, owner.epoch)
                for owner in self.controller.durable_owner_chain()
            ],
            [("host-a", 1)],
        )

    def test_rebound_current_sequence_cannot_preapprove_one_intervening_event(self) -> None:
        original_current = JournalStore.current_journal_sequence

        def forged_next_sequence(store):
            return original_current(store) + 1

        with patch.object(
            JournalStore,
            "current_journal_sequence",
            new=forged_next_sequence,
        ), patch(
            "mvp.autotrade_mvp.recovery_takeover.require_current_trade_credential_transition_anchor",
            new=self._anchor_injector(
                event_id="owner-cursor-rebinding-race-1",
                aggregate_id="owner-cursor-rebinding-race",
            ),
        ):
            with self.assertRaisesRegex(
                DurableTakeoverError,
                "journal changed during takeover owner validation",
            ):
                self._takeover()

        self.assertEqual(
            [
                (owner.owner_id, owner.epoch)
                for owner in self.controller.durable_owner_chain()
            ],
            [("host-a", 1)],
        )


if __name__ == "__main__":
    unittest.main()
