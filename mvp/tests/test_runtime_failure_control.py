import unittest

from mvp.autotrade_mvp.recovery import (
    HostState,
    OutboundAttempt,
    RecoveryController,
    SendPhase,
)


class RuntimeRecoveryTests(unittest.TestCase):
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

    def test_absence_requires_independent_evidence_before_retry(self):
        attempt = OutboundAttempt("a1", "intent-1", 1)
        attempt.persist()
        attempt.mark_send_started("journal:send-started")
        with self.assertRaises(ValueError):
            attempt.prove_absent(["orders:none"])
        attempt.prove_absent(["orders:none", "history:none"])
        self.assertEqual(attempt.phase, SendPhase.PROVEN_ABSENT)
        self.assertEqual(attempt.retry_disposition, "SAFE_WITH_NEW_ADMISSION")

    def test_full_disk_blocks_new_financial_admission(self):
        controller, owner = self._ready()
        controller.set_storage_writable(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaisesRegex(PermissionError, "ready"):
            controller.validate_admission(owner.epoch)
        controller.set_storage_writable(True)
        self.assertEqual(controller.state, HostState.READY)

    def test_clock_jump_blocks_until_clock_is_requalified(self):
        controller, owner = self._ready()
        controller.set_clock_trusted(False)
        self.assertEqual(controller.state, HostState.BLOCKED)
        with self.assertRaises(PermissionError):
            controller.validate_sender(owner.owner_id, owner.epoch)
        controller.set_clock_trusted(True)
        self.assertEqual(controller.state, HostState.READY)

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
