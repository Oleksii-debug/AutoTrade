from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import (
    activate_recovery_takeover_target,
    mark_recovery_takeover_source,
)
from mvp.autotrade_mvp.recovery_takeover import (
    DurableTakeoverError,
    execute_durable_takeover,
    require_committed_durable_takeover,
)
from mvp.autotrade_mvp.sender_authority import (
    SenderAuthorityError,
    sender_authority_window,
)
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"takeover-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


def _snapshot():
    return SnapshotConsistencyEvidence(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        mode="ATOMIC",
        query_started_at="2026-10-04T00:30:00Z",
        query_completed_at="2026-10-04T00:31:00Z",
    )


def _fill():
    return ProviderFillEvidence.create(
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


def _reconciliation():
    return reconcile_account(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        local_cash={"USD": "900"},
        provider_cash={"USD": "900"},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=[],
        provider_fills=[]
        snapshot_consistency=_snapshot(),
        coverage_start="2026-10-04T00:30:00Z",
        coverage_end="2026-10-04T00:31:00Z",
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="paper-1",
    )


class DurableRecoveryTakeoverTests(unittest.TestCase):
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
        self._record_ready_checkpoint()

    def _record_ready_checkpoint(self) -> None:
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
        self.assertEqual(self.controller.state, HostState.READY)

    def _takeover(self, controller=None, *, new_owner_id="host-b"):
        return execute_durable_takeover(
            self.controller if controller is None else controller,
            new_owner_id=new_owner_id,
            vault=self.vault,
            handle=self.handle,
            execution_identity="windows-user-1",
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
        )

    def _takeover_events(self):
        return self.store.load_events_by_aggregate_type("recovery_takeover")

    def _assert_old_credential_revoked(self) -> None:
        with self.assertRaisesRegex(PermissionError, "unavailable"):
            self.vault.resolve(
                self.handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_takeover_revokes_old_generation_commits_owner_and_requires_fresh_reconciliation(self):
        result = self._takeover()

        self.assertEqual(result.source_owner.owner_id, "host-a")
        self.assertEqual(result.source_owner.epoch, 1)
        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(result.target_owner.epoch, 2)
        self.assertEqual(self.controller.owner, result.target_owner)
        self.assertEqual(self.controller.state, HostState.RECOVERING)
        self.assertFalse(self.controller.provider_reconciled)
        self.assertIn("startup_reconciliation_required", self.controller.reason_codes)
        self._assert_old_credential_revoked()
        self.assertEqual(
            [event["event_type"] for event in self._takeover_events()],
            [
                "RecoveryTakeoverStarted",
                "RecoveryTakeoverEvidenceIssued",
                "RecoveryTakeoverOwnerCommitted",
            ],
        )
        self.assertEqual(
            [(owner.owner_id, owner.epoch) for owner in self.controller.durable_owner_chain()],
            [("host-a", 1), ("host-b", 2)],
        )
        # A valid completed transition may release the process-shared gate, but
        # the successor still cannot send until its own reconciliation is READY.
        with sender_authority_window(self.store, owner_scope="PAPER:paper-1"):
            pass
        with self.assertRaisesRegex(PermissionError, "Host is not ready"):
            self.controller.validate_sender("host-b", 2)

    def test_sender_activation_proof_requires_issuer_sealed_complete_takeover(self):
        result = self._takeover()
        self.assertEqual(
            require_committed_durable_takeover(
                self.controller, result=result, vault=self.vault
            ),
            result.target_owner,
        )
        with self.assertRaises(DurableTakeoverError):
            require_committed_durable_takeover(
                self.controller,
                result=replace(result, completion_event_id="forged-event-id"),
                vault=self.vault,
            )

    def test_owner_journal_advance_without_issuer_cannot_fake_completion(self):
        source = self.controller.owner
        target = type(source)("host-b", source.epoch + 1)
        self.controller._append_durable_owner(target)
        self.controller.owner = target
        from mvp.autotrade_mvp.recovery_takeover import DurableTakeoverResult
        fake = DurableTakeoverResult(
            takeover_id="recovery-takeover/sha256:" + "0" * 64,
            source_owner=source,
            target_owner=target,
            credential_transition_receipt_id="fake",
            takeover_evidence_event_id="fake",
            recovery_owner_event_id="fake",
            completion_event_id="fake",
        )
        with self.assertRaisesRegex(
            DurableTakeoverError, "completion evidence is missing"
        ):
            require_committed_durable_takeover(
                self.controller, result=fake, vault=self.vault
            )
        self.assertTrue(self.vault.resolve(
            self.handle,
            execution_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
        ))

    def test_production_activation_accepts_issuer_verified_takeover_only(self):
        source = self.controller.owner
        mark_recovery_takeover_source(self.controller, source)
        result = self._takeover()
        activated = activate_recovery_takeover_target(
            self.controller,
            source=source,
            target=result.target_owner,
            takeover=result,
            vault=self.vault,
        )
        self.assertEqual(activated, result.target_owner)
        self.assertNotIn("takeover_source_only", self.controller.reason_codes)
        self.assertEqual(self.controller.state, HostState.RECOVERING)
        self._assert_old_credential_revoked()

    def test_manual_advance_cannot_clear_production_takeover_source(self):
        from mvp.autotrade_mvp.recovery_takeover import DurableTakeoverResult
        source = self.controller.owner
        mark_recovery_takeover_source(self.controller, source)
        target = type(source)("host-b", source.epoch + 1)
        self.controller._append_durable_owner(target)
        self.controller.owner = target
        forged = DurableTakeoverResult(
            takeover_id="recovery-takeover/sha256:" + "0" * 64,
            source_owner=source,
            target_owner=target,
            credential_transition_receipt_id="fake",
            takeover_evidence_event_id="fake",
            recovery_owner_event_id="fake",
            completion_event_id="fake",
        )
        with self.assertRaises(DurableTakeoverError):
            activate_recovery_takeover_target(
                self.controller,
                source=source,
                target=target,
                takeover=forged,
                vault=self.vault,
            )
        self.assertIn("takeover_source_only", self.controller.reason_codes)

    def test_direct_restart_start_cannot_mint_next_durable_owner_epoch(self):
        restarted = RecoveryController(
            owner_store=JournalStore(self.store.path),
            owner_scope="PAPER:paper-1",
        )

        with self.assertRaisesRegex(
            PermissionError,
            "Existing durable owner requires explicit takeover evidence",
        ):
            restarted.start("host-a")

        self.assertEqual(
            [(owner.owner_id, owner.epoch) for owner in restarted.durable_owner_chain()],
            [("host-a", 1)],
        )
        self.assertIsNone(restarted.owner)

    def test_same_host_restart_takeover_advances_epoch_without_owner_rename(self):
        result = self._takeover(new_owner_id="host-a")

        self.assertEqual(
            (result.source_owner.owner_id, result.source_owner.epoch),
            ("host-a", 1),
        )
        self.assertEqual(
            (result.target_owner.owner_id, result.target_owner.epoch),
            ("host-a", 2),
        )
        self.assertEqual(self.controller.owner, result.target_owner)
        self.assertEqual(
            [(owner.owner_id, owner.epoch) for owner in self.controller.durable_owner_chain()],
            [("host-a", 1), ("host-a", 2)],
        )
        self.assertEqual(self.controller.state, HostState.RECOVERING)
        self.assertFalse(self.controller.provider_reconciled)
        self._assert_old_credential_revoked()
        with self.assertRaisesRegex(PermissionError, "Host is not ready"):
            self.controller.validate_sender("host-a", 2)

    def test_crash_after_started_event_blocks_old_sender_and_resumes(self):
        with patch(
            "mvp.autotrade_mvp.recovery_takeover.revoke_trade_credential_with_receipt",
            side_effect=RuntimeError("simulated crash before credential transition"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self._takeover()

        self.assertEqual(
            [event["event_type"] for event in self._takeover_events()],
            ["RecoveryTakeoverStarted"],
        )
        with self.assertRaisesRegex(SenderAuthorityError, "pending durable takeover"):
            with sender_authority_window(
                self.store,
                owner_scope="PAPER:paper-1",
            ):
                self.fail("old sender gate must not reopen during pending takeover")

        result = self._takeover()
        self.assertEqual(result.target_owner.owner_id, "host-b")
        self._assert_old_credential_revoked()

    def test_crash_after_credential_revocation_resumes_without_reactivating_old_generation(self):
        original_append = JournalStore.append_event

        def crash_before_evidence(store, event):
            if event.get("event_type") == "RecoveryTakeoverEvidenceIssued":
                raise RuntimeError("simulated crash after credential revocation")
            return original_append(store, event)

        with patch(
            "mvp.autotrade_mvp.recovery_takeover.JournalStore.append_event",
            new=crash_before_evidence,
        ):
            with self.assertRaisesRegex(RuntimeError, "after credential revocation"):
                self._takeover()

        self._assert_old_credential_revoked()
        self.assertEqual(
            [event["event_type"] for event in self._takeover_events()],
            ["RecoveryTakeoverStarted"],
        )
        with self.assertRaises(SenderAuthorityError):
            with sender_authority_window(self.store, owner_scope="PAPER:paper-1"):
                self.fail("sender authority must remain fenced")

        result = self._takeover()
        self.assertEqual(result.target_owner.epoch, 2)
        self._assert_old_credential_revoked()

    def test_crash_after_evidence_keeps_source_owner_fenced_until_resume(self):
        with patch.object(
            self.controller,
            "_append_durable_owner",
            side_effect=RuntimeError("simulated crash after takeover evidence"),
        ):
            with self.assertRaisesRegex(RuntimeError, "after takeover evidence"):
                self._takeover()

        self.assertEqual(
            [event["event_type"] for event in self._takeover_events()],
            ["RecoveryTakeoverStarted", "RecoveryTakeoverEvidenceIssued"],
        )
        self.assertEqual(self.controller._latest_durable_owner().owner_id, "host-a")
        self._assert_old_credential_revoked()
        with self.assertRaises(SenderAuthorityError):
            with sender_authority_window(self.store, owner_scope="PAPER:paper-1"):
                self.fail("source sender must remain fenced after issued evidence")

        result = self._takeover()
        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(self.controller.state, HostState.RECOVERING)

    def test_crash_after_owner_commit_is_resumable_by_fresh_controller(self):
        original_append = JournalStore.append_event

        def crash_before_completion(store, event):
            if event.get("event_type") == "RecoveryTakeoverOwnerCommitted":
                raise RuntimeError("simulated crash after owner commit")
            return original_append(store, event)

        with patch(
            "mvp.autotrade_mvp.recovery_takeover.JournalStore.append_event",
            new=crash_before_completion,
        ):
            with self.assertRaisesRegex(RuntimeError, "after owner commit"):
                self._takeover()

        self.assertEqual(self.controller._latest_durable_owner().owner_id, "host-b")
        self.assertEqual(
            [event["event_type"] for event in self._takeover_events()],
            ["RecoveryTakeoverStarted", "RecoveryTakeoverEvidenceIssued"],
        )
        with self.assertRaises(SenderAuthorityError):
            with sender_authority_window(self.store, owner_scope="PAPER:paper-1"):
                self.fail("new sender must wait for takeover completion and reconciliation")

        restarted = RecoveryController(
            owner_store=JournalStore(self.store.path),
            owner_scope="PAPER:paper-1",
        )
        result = self._takeover(controller=restarted)
        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(restarted.owner, result.target_owner)
        self.assertEqual(restarted.state, HostState.RECOVERING)
        self.assertEqual(
            [event["event_type"] for event in self._takeover_events()],
            [
                "RecoveryTakeoverStarted",
                "RecoveryTakeoverEvidenceIssued",
                "RecoveryTakeoverOwnerCommitted",
            ],
        )

    def test_unresolved_provider_send_blocks_takeover_before_credential_transition(self):
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="host-a",
            owner_epoch=1,
        )

        def transport(_client_id, _request, final_guard):
            final_guard()
            raise TimeoutError("provider reply lost")

        outcome = dispatcher.dispatch(
            attempt_id="ambiguous-before-takeover",
            intent_id="intent-ambiguous",
            intent_hash="hash-ambiguous",
            provider="SIMULATED",
            request={},
            now="2026-10-04T00:32:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=transport,
            sender_check=self.controller.validate_sender,
        )
        self.assertEqual(outcome.status, "UNKNOWN")

        with self.assertRaisesRegex(DurableTakeoverError, "unresolved"):
            self._takeover()
        self.assertEqual(self._takeover_events(), [])
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

    def test_legacy_boolean_transfer_cannot_bypass_issued_takeover(self):
        with self.assertRaisesRegex(PermissionError, "independently issued takeover evidence"):
            self.controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled=True,
            )
        self.assertEqual(self.controller.owner.owner_id, "host-a")
        self.assertEqual(self.controller.owner.epoch, 1)
        self.assertEqual(self._takeover_events(), [])

    def test_competing_target_cannot_hijack_pending_takeover(self):
        with patch(
            "mvp.autotrade_mvp.recovery_takeover.revoke_trade_credential_with_receipt",
            side_effect=RuntimeError("stop after started"),
        ):
            with self.assertRaises(RuntimeError):
                self._takeover(new_owner_id="host-b")

        with self.assertRaisesRegex(DurableTakeoverError, "target_owner_id"):
            self._takeover(new_owner_id="host-c")
        self.assertEqual(
            [event["event_type"] for event in self._takeover_events()],
            ["RecoveryTakeoverStarted"],
        )
        self.assertEqual(self.controller._latest_durable_owner().owner_id, "host-a")

    def test_forged_completion_without_named_owner_commit_does_not_reopen_sender_gate(self):
        with patch(
            "mvp.autotrade_mvp.recovery_takeover.revoke_trade_credential_with_receipt",
            side_effect=RuntimeError("stop after started"),
        ):
            with self.assertRaises(RuntimeError):
                self._takeover()

        started = self._takeover_events()[0]
        started_payload = started["payload"]
        receipt_id = "credential-transition/sha256:" + "0" * 64
        evidence_payload = {
            "takeover_id": started_payload["takeover_id"],
            "owner_scope": started_payload["owner_scope"],
            "source_owner_id": started_payload["source_owner_id"],
            "source_owner_epoch": started_payload["source_owner_epoch"],
            "target_owner_id": started_payload["target_owner_id"],
            "target_owner_epoch": started_payload["target_owner_epoch"],
            "credential_transition_receipt_id": receipt_id,
        }
        evidence = {
            "event_id": "forged-takeover-evidence",
            "event_type": "RecoveryTakeoverEvidenceIssued",
            "aggregate_type": "recovery_takeover",
            "aggregate_id": started["aggregate_id"],
            "aggregate_version": "2",
            "payload": evidence_payload,
            "payload_hash": payload_digest(evidence_payload),
            "committed_at": "2026-10-04T00:33:00Z",
        }
        self.store.append_event(evidence)
        completion_payload = {
            "takeover_id": started_payload["takeover_id"],
            "owner_scope": started_payload["owner_scope"],
            "source_owner_id": started_payload["source_owner_id"],
            "source_owner_epoch": started_payload["source_owner_epoch"],
            "target_owner_id": started_payload["target_owner_id"],
            "target_owner_epoch": started_payload["target_owner_epoch"],
            "takeover_evidence_event_id": evidence["event_id"],
            "credential_transition_receipt_id": receipt_id,
            "recovery_owner_event_id": "missing-owner-event",
            "recovery_owner_payload_hash": "sha256:" + "0" * 64,
            "recovery_owner_journal_sequence": 999999,
        }
        completion = {
            "event_id": "forged-takeover-completion",
            "event_type": "RecoveryTakeoverOwnerCommitted",
            "aggregate_type": "recovery_takeover",
            "aggregate_id": started["aggregate_id"],
            "aggregate_version": "3",
            "payload": completion_payload,
            "payload_hash": payload_digest(completion_payload),
            "committed_at": "2026-10-04T00:33:01Z",
        }
        self.store.append_event(completion)

        with self.assertRaisesRegex(SenderAuthorityError, "target owner event is missing"):
            with sender_authority_window(self.store, owner_scope="PAPER:paper-1"):
                self.fail("forged completion must never reopen sender authority")
        self.assertEqual(self.controller._latest_durable_owner().owner_id, "host-a")

    def test_missing_reconciliation_fails_before_durable_takeover_or_revoke(self):
        root = Path(self.directory.name)
        store = JournalStore(root / "unreconciled.sqlite3")
        controller = RecoveryController(
            owner_store=store,
            owner_scope="PAPER:paper-2",
        )
        controller.start("host-a")
        vault = ProtectedCredentialVault(
            root / "unreconciled-credentials.json",
            protector=DeterministicProtector(),
        )
        handle = vault.register(
            handle_id="unreconciled-trade",
            owner_identity="windows-user-2",
            account_id="paper-2",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="secret-v1",
        )

        with self.assertRaisesRegex(PermissionError, "No current reconciliation checkpoint"):
            execute_durable_takeover(
                controller,
                new_owner_id="host-b",
                vault=vault,
                handle=handle,
                execution_identity="windows-user-2",
                reconciliation_id="missing",
                provider_id="SIMULATED",
            )
        self.assertEqual(store.load_events_by_aggregate_type("recovery_takeover"), [])
        self.assertEqual(
            vault.resolve(
                handle,
                execution_identity="windows-user-2",
                account_id="paper-2",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "secret-v1",
        )


if __name__ == "__main__":
    unittest.main()
