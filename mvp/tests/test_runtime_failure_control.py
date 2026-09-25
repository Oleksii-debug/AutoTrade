from contextlib import closing
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
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

    def test_recovery_boolean_contracts_reject_truthy_strings(self):
        controller = RecoveryController()
        owner = controller.start("host-a")

        with self.assertRaisesRegex(TypeError, "consistent must be boolean"):
            controller.record_reconciliation(consistent="false")
        self.assertEqual(controller.state, HostState.RECOVERING)

        controller.record_reconciliation(consistent=True)
        self.assertEqual(controller.state, HostState.READY)

        with self.assertRaisesRegex(TypeError, "writable must be boolean"):
            controller.set_storage_writable("false")
        with self.assertRaisesRegex(TypeError, "trusted must be boolean"):
            controller.set_clock_trusted("false")
        self.assertEqual(controller.state, HostState.READY)

        with self.assertRaisesRegex(TypeError, "old_sender_fenced must be boolean"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced="true",
                reconciled=True,
            )
        with self.assertRaisesRegex(TypeError, "reconciled must be boolean"):
            controller.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled="true",
            )
        self.assertEqual(controller.owner, owner)

    def test_boolean_cannot_impersonate_owner_epoch_one(self):
        controller, owner = self._ready()
        self.assertEqual(owner.epoch, 1)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            controller.validate_sender(owner.owner_id, True)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            controller.validate_admission(True)

    def test_transfer_owner_identity_is_canonical_before_comparison(self):
        controller, owner = self._ready()
        with self.assertRaisesRegex(ValueError, "differ"):
            controller.transfer_owner(
                new_owner_id=" host-a ",
                old_sender_fenced=True,
                reconciled=True,
            )
        self.assertEqual(controller.owner, owner)

    def test_durable_owner_epoch_survives_restart_and_fences_old_process(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="paper-account",
            )
            first_owner = first.start("host-a")
            first.record_reconciliation(consistent=True)
            first.validate_sender(first_owner.owner_id, first_owner.epoch)
            self.assertEqual(first_owner.epoch, 1)

            second = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="paper-account",
            )
            second_owner = second.start("host-b")
            self.assertEqual(second_owner.epoch, 2)
            self.assertEqual(second.state, HostState.RECOVERING)

            with self.assertRaisesRegex(PermissionError, "Durable sender fence"):
                first.validate_sender(first_owner.owner_id, first_owner.epoch)
            with self.assertRaisesRegex(PermissionError, "Durable sender fence"):
                first.validate_admission(first_owner.epoch)
            with self.assertRaisesRegex(PermissionError, "Durable sender fence"):
                first.record_reconciliation(consistent=True)

            second.record_reconciliation(consistent=True)
            second.validate_sender(second_owner.owner_id, second_owner.epoch)

            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="paper-account",
            )
            third_owner = restarted.start("host-c")
            self.assertEqual(third_owner.epoch, 3)

    def test_durable_owner_transfer_is_visible_to_other_controller(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            first = RecoveryController(
                owner_store=store,
                owner_scope="paper-account",
            )
            first_owner = first.start("host-a")
            first.record_reconciliation(consistent=True)

            observer = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="other-scope",
            )
            observer_owner = observer.start("independent-host")
            observer.record_reconciliation(consistent=True)

            transferred = first.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled=True,
            )
            self.assertEqual(transferred.epoch, first_owner.epoch + 1)
            self.assertEqual(observer_owner.epoch, 1)
            observer.validate_sender(observer_owner.owner_id, observer_owner.epoch)

            durable_events = store.load_events("recovery_owner", "paper-account")
            self.assertEqual(
                [event["aggregate_version"] for event in durable_events],
                [1, 2],
            )
            self.assertEqual(
                [event["payload"]["owner_id"] for event in durable_events],
                ["host-a", "host-b"],
            )

    def test_tampered_older_owner_generation_blocks_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="paper-account",
            )
            first.start("host-a")
            first.record_reconciliation(consistent=True)
            first.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled=True,
            )

            with closing(sqlite3.connect(path)) as connection:
                row = connection.execute(
                    "SELECT event_id, payload_json FROM events "
                    "WHERE aggregate_type = 'recovery_owner' "
                    "AND aggregate_version = 1"
                ).fetchone()
                self.assertIsNotNone(row)
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE event_id = ?",
                    ('{"owner_epoch":"1","owner_id":"tampered-host"}', row[0]),
                )
                connection.commit()

            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="paper-account",
            )
            with self.assertRaisesRegex(ValueError, "payload hash"):
                restarted.start("host-c")

    def test_tampered_durable_owner_record_blocks_sender_validation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            controller = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="paper-account",
            )
            owner = controller.start("host-a")
            controller.record_reconciliation(consistent=True)

            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET payload_json = ? "
                    "WHERE aggregate_type = 'recovery_owner'",
                    ('{"owner_epoch":"1","owner_id":"attacker"}',),
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "payload hash"):
                controller.validate_sender(owner.owner_id, owner.epoch)

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
