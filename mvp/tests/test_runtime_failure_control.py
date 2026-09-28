import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.tests.test_reconciliation_journal import reconciliation
from mvp.autotrade_mvp.recovery import (
    HostState,
    OutboundAttempt,
    RecoveryController,
    SendPhase,
)


class DurableReconciliationAuthorityTests(unittest.TestCase):
    def test_durable_controller_rejects_caller_consistency_boolean(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:test-account",
            )
            controller.start("host-a")
            with self.assertRaisesRegex(
                PermissionError,
                "journal-issued reconciliation checkpoint",
            ):
                controller.record_reconciliation(consistent=True)
            self.assertEqual(controller.state, HostState.RECOVERING)
            self.assertFalse(controller.provider_reconciled)

    def test_latest_owner_bound_checkpoint_is_durable_readiness_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:test-account",
            )
            owner = controller.start("host-a")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="runtime-readiness",
                result=reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )

            evidence = controller.record_reconciliation_checkpoint(
                reconciliation_id="runtime-readiness",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )
            self.assertEqual(controller.state, HostState.READY)
            self.assertTrue(controller.provider_reconciled)
            self.assertEqual(evidence["event_id"], checkpoint["event_id"])
            self.assertEqual(evidence["payload_hash"], checkpoint["payload_hash"])
            self.assertEqual(
                evidence["journal_sequence"],
                checkpoint["journal_sequence"],
            )

            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:test-account",
            )
            new_owner = restarted.start("host-b")
            self.assertGreater(new_owner.epoch, owner.epoch)
            with self.assertRaisesRegex(
                PermissionError,
                "bound to this recovery owner",
            ):
                restarted.record_reconciliation_checkpoint(
                    reconciliation_id="runtime-readiness",
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                )
            self.assertEqual(restarted.state, HostState.RECOVERING)
            self.assertFalse(restarted.provider_reconciled)

    def test_bybit_recovery_readiness_requires_exact_provider_environment(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:bybit-account",
            )
            owner = controller.start("host-bybit")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="bybit-runtime-readiness",
                result=reconciliation(
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                    provider_environment="TESTNET",
                ),
                observed_at="2026-09-24T19:00:00Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )

            with self.assertRaisesRegex(
                PermissionError,
                "requires explicit provider_environment",
            ):
                controller.record_reconciliation_checkpoint(
                    reconciliation_id="bybit-runtime-readiness",
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                )
            self.assertEqual(controller.state, HostState.RECOVERING)
            self.assertFalse(controller.provider_reconciled)

            with self.assertRaisesRegex(
                PermissionError,
                "bound to this recovery owner",
            ):
                controller.record_reconciliation_checkpoint(
                    reconciliation_id="bybit-runtime-readiness",
                    provider_id="BYBIT",
                    account_id="bybit-account",
                    environment="PAPER",
                    provider_environment="DEMO",
                )
            self.assertEqual(controller.state, HostState.RECOVERING)
            self.assertFalse(controller.provider_reconciled)

            evidence = controller.record_reconciliation_checkpoint(
                reconciliation_id="bybit-runtime-readiness",
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            self.assertEqual(controller.state, HostState.READY)
            self.assertTrue(controller.provider_reconciled)
            self.assertEqual(evidence["event_id"], checkpoint["event_id"])
            self.assertEqual(evidence["provider_environment"], "TESTNET")

    def test_invalid_checkpoint_identity_cannot_mutate_controller_ready(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:test-account",
            )
            owner = controller.start("host-a")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="malformed-readiness",
                result=reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )
            malformed = dict(checkpoint)
            malformed["event_id"] = ""

            with patch(
                "mvp.autotrade_mvp.recovery."
                "load_reconciliation_checkpoint_for_readiness",
                return_value=malformed,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "durable identity is invalid",
                ):
                    controller.record_reconciliation_checkpoint(
                        reconciliation_id="malformed-readiness",
                        provider_id="TEST_PROVIDER",
                        account_id="test-account",
                        environment="PAPER",
                    )

            self.assertFalse(controller.provider_reconciled)
            self.assertEqual(controller.state, HostState.RECOVERING)
            self.assertIn(
                "startup_reconciliation_required",
                controller.reason_codes,
            )


class RuntimeRecoveryTests(unittest.TestCase):
    def _record_durable_ready(self, controller, *, reconciliation_id="runtime-readiness"):
        owner = controller.owner
        self.assertIsNotNone(owner)
        store_path = controller.durable_owner_store_path
        self.assertIsNotNone(store_path)
        store = JournalStore(store_path)
        result = reconciliation(
            account_id=controller.owner_scope.split(":", 1)[1],
            environment=controller.owner_scope.split(":", 1)[0],
        )
        record_reconciliation_checkpoint(
            store,
            reconciliation_id=reconciliation_id,
            result=result,
            observed_at="2026-09-24T19:00:00Z",
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        controller.record_reconciliation_checkpoint(
            reconciliation_id=reconciliation_id,
            provider_id=result.provider_id,
            account_id=result.account_id,
            environment=result.environment,
        )

    def _ready(self):
        controller = RecoveryController()
        owner = controller.start("host-a")
        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        return controller, owner

    def test_start_never_reports_ready_before_reconciliation(self):
        controller = RecoveryController()
        controller.start("host-a")
        self.assertEqual(controller.state, HostState.RECOVERING)
        with self.assertRaises(PermissionError):
            controller.validate_admission(1)

    def test_crash_before_send_is_retryable_only_with_new_admission(self):
        attempt = OutboundAttempt("a1", "intent-1", 1)
        attempt.persist()
        self.assertEqual(attempt.phase, SendPhase.DURABLE)
        self.assertEqual(attempt.retry_disposition, "SAFE_WITH_NEW_ADMISSION")

    def test_crash_after_send_before_ack_requires_reconciliation(self):
        controller, owner = self._ready()
        attempt = OutboundAttempt("a1", "intent-1", owner.epoch)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        controller.note_unknown_send(attempt)
        self.assertEqual(attempt.retry_disposition, "RECONCILE_FIRST")
        self.assertEqual(controller.state, HostState.DEGRADED)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

    def test_unknown_attempt_can_only_clear_with_external_resolution(self):
        controller, owner = self._ready()
        attempt = OutboundAttempt("a1", "intent-1", owner.epoch)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        controller.note_unknown_send(attempt)
        with self.assertRaises(ValueError):
            controller.resolve_attempt(attempt)
        attempt.acknowledge("provider-7", "provider:ack")
        controller.resolve_attempt(attempt)
        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        self.assertEqual(attempt.retry_disposition, "NEVER")

    def test_generic_reconciliation_cannot_erase_known_unknown_attempt(self):
        controller, owner = self._ready()
        attempt = OutboundAttempt("a1", "intent-1", owner.epoch)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        controller.note_unknown_send(attempt)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.unresolved_attempts, {"a1"})
        self.assertEqual(controller.state, HostState.DEGRADED)

        controller.record_reconciliation(
            consistent=True,
            uncertainty=["unrelated-provider-gap"],
        )
        self.assertEqual(
            controller.unresolved_attempts,
            {"a1", "unrelated-provider-gap"},
        )
        self.assertEqual(controller.state, HostState.DEGRADED)

        attempt.acknowledge("provider-7", "provider:ack")
        controller.resolve_attempt(attempt)
        self.assertEqual(controller.unresolved_attempts, {"unrelated-provider-gap"})

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.unresolved_attempts, set())
        self.assertEqual(controller.state, HostState.READY)

    def test_different_attempt_cannot_clear_sticky_unknown_by_reusing_id(self):
        controller, owner = self._ready()
        original = OutboundAttempt("a1", "intent-original", owner.epoch)
        original.persist()
        original.mark_send_started("journal:original-send")
        controller.note_unknown_send(original)

        forged = OutboundAttempt("a1", "intent-other", owner.epoch)
        forged.persist()
        forged.mark_send_started("journal:forged-send")
        forged.acknowledge("provider-forged", "provider:forged-ack")
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            controller.resolve_attempt(forged)
        self.assertEqual(controller.unresolved_attempts, {"a1"})
        self.assertEqual(controller.state, HostState.DEGRADED)

        same_identity_wrong_evidence = OutboundAttempt(
            "a1",
            "intent-original",
            owner.epoch,
        )
        same_identity_wrong_evidence.persist()
        same_identity_wrong_evidence.mark_send_started("journal:different-send")
        same_identity_wrong_evidence.reject("provider:rejected")
        with self.assertRaisesRegex(ValueError, "original send evidence"):
            controller.resolve_attempt(same_identity_wrong_evidence)
        self.assertEqual(controller.unresolved_attempts, {"a1"})

    def test_attempt_identity_and_reconciliation_uncertainty_are_strict(self):
        for kwargs in (
            {"attempt_id": "", "intent_id": "i1", "owner_epoch": 1},
            {"attempt_id": "a1", "intent_id": "", "owner_epoch": 1},
            {"attempt_id": "a1", "intent_id": "i1", "owner_epoch": True},
            {"attempt_id": "a1", "intent_id": "i1", "owner_epoch": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                OutboundAttempt(**kwargs)

        controller = RecoveryController()
        controller.start("host-a")
        for uncertainty in ((1,), ("",), (None,)):
            with self.subTest(uncertainty=uncertainty), self.assertRaisesRegex(
                ValueError,
                "uncertainty identities",
            ):
                controller.record_reconciliation(
                    consistent=True,
                    uncertainty=uncertainty,
                )
        self.assertEqual(controller.state, HostState.RECOVERING)

    def test_absence_requires_independent_evidence_before_retry(self):
        attempt = OutboundAttempt("a1", "intent-1", 1)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        with self.assertRaises(ValueError):
            attempt.prove_absent(["orders:none"])
        with self.assertRaisesRegex(ValueError, "independent evidence"):
            attempt.prove_absent(["orders:none", "orders:none"])
        self.assertEqual(attempt.phase, SendPhase.SENT_UNKNOWN)
        self.assertEqual(attempt.retry_disposition, "RECONCILE_FIRST")
        attempt.prove_absent(["orders:none", "history:none"])
        self.assertEqual(attempt.phase, SendPhase.PROVEN_ABSENT)
        self.assertEqual(attempt.retry_disposition, "SAFE_WITH_NEW_ADMISSION")

    def test_full_disk_blocks_new_financial_admission_until_reconciled_after_restore(self):
        controller, owner = self._ready()
        controller.set_storage_writable(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(PermissionError, "ready"):
            controller.validate_admission(owner.epoch)
        with self.assertRaisesRegex(PermissionError, "durable journal"):
            controller.record_reconciliation(consistent=True)

        controller.set_storage_writable(True)
        self.assertEqual(controller.state, HostState.RECOVERING)
        with self.assertRaisesRegex(PermissionError, "ready"):
            controller.validate_admission(owner.epoch)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        controller.validate_admission(owner.epoch)

    def test_storage_writable_flag_requires_real_boolean(self):
        controller, _ = self._ready()
        with self.assertRaises(TypeError):
            controller.set_storage_writable("true")

    def test_reconciliation_consistency_requires_real_boolean(self):
        controller = RecoveryController()
        controller.start("host-a")
        with self.assertRaisesRegex(TypeError, "consistent"):
            controller.record_reconciliation(consistent="false")
        self.assertEqual(controller.state, HostState.RECOVERING)

    def test_clock_trust_requires_real_boolean_and_cannot_truthiness_bypass_block(self):
        controller, owner = self._ready()
        controller.set_clock_trusted(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(TypeError, "trusted"):
            controller.set_clock_trusted("false")
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(PermissionError, "ready"):
            controller.validate_admission(owner.epoch)

    def test_owner_transfer_authority_flags_require_real_booleans(self):
        controller, _ = self._ready()
        with self.assertRaisesRegex(TypeError, "old_sender_fenced"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced="true",
                reconciled=True,
            )
        with self.assertRaisesRegex(TypeError, "reconciled"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled="true",
            )
        self.assertEqual(controller.owner.owner_id, "host-a")
        self.assertEqual(controller.owner.epoch, 1)

    def test_clock_jump_blocks_until_clock_is_requalified_and_reconciled(self):
        controller, owner = self._ready()
        controller.set_clock_trusted(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        self.assertFalse(controller.provider_reconciled)
        self.assertIn("clock_requalification_required", controller.reason_codes)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

        controller.set_clock_trusted(True)
        self.assertEqual(controller.state, HostState.RECOVERING)
        self.assertIn("clock_requalification_required", controller.reason_codes)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)
        self.assertNotIn("clock_requalification_required", controller.reason_codes)
        controller.validate_sender(owner.owner_id, owner.epoch)

    def test_lease_expiry_does_not_create_a_new_sender(self):
        controller, owner = self._ready()
        controller.on_lease_expired()
        self.assertEqual(controller.owner, owner)
        self.assertEqual(controller.state, HostState.DEGRADED)
        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.DEGRADED)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

    def test_transfer_requires_old_sender_fencing_and_reconciliation(self):
        controller, owner = self._ready()
        with self.assertRaisesRegex(PermissionError, "fenced"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=False,
                reconciled=True,
            )
        new_owner = controller.transfer_owner(
            new_owner_id="host-b",
            old_sender_fenced=True,
            reconciled=True,
        )
        self.assertEqual(new_owner.epoch, owner.epoch + 1)
        self.assertEqual(controller.state, HostState.RECOVERING)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)

    def test_owner_transfer_cannot_self_assert_reconciliation(self):
        controller, _ = self._ready()
        controller.set_storage_writable(False)
        controller.set_storage_writable(True)
        self.assertFalse(controller.provider_reconciled)
        self.assertEqual(controller.state, HostState.RECOVERING)

        with self.assertRaisesRegex(
            PermissionError,
            "recorded current reconciliation",
        ):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled=True,
            )
        self.assertEqual(controller.owner.owner_id, "host-a")
        self.assertEqual(controller.owner.epoch, 1)

        controller.record_reconciliation(consistent=True)
        transferred = controller.transfer_owner(
            new_owner_id="host-b",
            old_sender_fenced=True,
            reconciled=True,
        )
        self.assertEqual(transferred.owner_id, "host-b")

    def test_new_owner_must_reconcile_again_before_sending(self):
        controller, _ = self._ready()
        new_owner = controller.transfer_owner(
            new_owner_id="host-b",
            old_sender_fenced=True,
            reconciled=True,
        )
        with self.assertRaises(PermissionError):
            controller.validate_sender(new_owner.owner_id, new_owner.epoch)
        controller.record_reconciliation(consistent=True)
        controller.validate_sender(new_owner.owner_id, new_owner.epoch)

    def test_durable_owner_epoch_survives_restart_and_fences_old_process(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:paper-account",
            )
            owner_one = first.start("host-a")
            self._record_durable_ready(first)
            first.validate_sender(owner_one.owner_id, owner_one.epoch)
            self.assertEqual(owner_one.epoch, 1)

            second = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:paper-account",
            )
            owner_two = second.start("host-b")
            self.assertEqual(owner_two.epoch, 2)
            self.assertEqual(second.state, HostState.RECOVERING)

            with self.assertRaisesRegex(PermissionError, "Durable sender fence"):
                first.validate_sender(owner_one.owner_id, owner_one.epoch)
            with self.assertRaisesRegex(PermissionError, "Durable sender fence"):
                first.validate_admission(owner_one.epoch)
            with self.assertRaisesRegex(PermissionError, "Durable sender fence"):
                self._record_durable_ready(first)

            self._record_durable_ready(second)
            second.validate_sender(owner_two.owner_id, owner_two.epoch)

            third = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:paper-account",
            )
            owner_three = third.start("host-c")
            self.assertEqual(owner_three.epoch, 3)
            self.assertEqual(third.state, HostState.RECOVERING)

    def test_durable_owner_scopes_are_independent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            shared = JournalStore(path)
            paper = RecoveryController(
                owner_store=shared,
                owner_scope="PAPER:acct",
            )
            live = RecoveryController(
                owner_store=shared,
                owner_scope="LIVE:acct",
            )
            paper_owner = paper.start("paper-host")
            live_owner = live.start("live-host")
            self.assertEqual(paper_owner.epoch, 1)
            self.assertEqual(live_owner.epoch, 1)

            self._record_durable_ready(paper)
            transferred = paper.transfer_owner(
                new_owner_id="paper-host-2",
                old_sender_fenced=True,
                reconciled=True,
            )
            self.assertEqual(transferred.epoch, 2)
            self._record_durable_ready(live)
            live.validate_sender(live_owner.owner_id, live_owner.epoch)

    def test_durable_transfer_fences_an_observer_of_old_generation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            owner = first.start("host-a")
            self._record_durable_ready(first)

            stale = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            stale.owner = owner
            stale.provider_reconciled = True
            stale.reason_codes.clear()
            stale.state = HostState.READY

            transferred = first.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled=True,
            )
            self.assertEqual(transferred.epoch, 2)
            with self.assertRaisesRegex(PermissionError, "Durable sender fence"):
                stale.validate_sender(owner.owner_id, owner.epoch)

    def test_owner_identity_is_normalized_before_start_and_transfer(self):
        controller = RecoveryController()
        owner = controller.start(" host-a ")
        self.assertEqual(owner.owner_id, "host-a")
        controller.record_reconciliation(consistent=True)
        with self.assertRaisesRegex(ValueError, "differ"):
            controller.transfer_owner(
                new_owner_id=" host-a ",
                old_sender_fenced=True,
                reconciled=True,
            )

    def test_boolean_cannot_impersonate_owner_epoch_one(self):
        controller, owner = self._ready()
        self.assertEqual(owner.epoch, 1)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            controller.validate_sender(owner.owner_id, True)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            controller.validate_admission(True)

    def test_corrupt_owner_journal_blocks_restart_and_sender_validation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            controller = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            owner = controller.start("host-a")
            self._record_durable_ready(controller)

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE events SET payload_json = ? "
                    "WHERE aggregate_type = 'recovery_owner'",
                    ('{"owner_epoch":"1","owner_id":"attacker"}',),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "payload hash"):
                controller.validate_sender(owner.owner_id, owner.epoch)
            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            with self.assertRaisesRegex(ValueError, "payload hash"):
                restarted.start("host-b")

    def test_provider_uncertainty_prevents_false_ready(self):
        controller = RecoveryController()
        controller.start("host-a")
        controller.record_reconciliation(
            consistent=True,
            uncertainty=["provider-order-state-unknown"],
        )
        self.assertEqual(controller.state, HostState.DEGRADED)
        self.assertIn("provider_uncertainty", controller.reason_codes)


if __name__ == "__main__":
    unittest.main()
