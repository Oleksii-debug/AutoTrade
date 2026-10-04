from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, OwnerFence, RecoveryController


class RecoveryTakeoverSourceAttachmentTests(unittest.TestCase):
    @staticmethod
    def _controller(directory: str) -> RecoveryController:
        return RecoveryController(
            owner_store=JournalStore(Path(directory) / "journal.sqlite3"),
            owner_scope="PAPER:acct",
        )

    def test_empty_owner_chain_cannot_attach_for_takeover(self) -> None:
        with TemporaryDirectory() as directory:
            recovery = self._controller(directory)
            with self.assertRaisesRegex(RuntimeError, "No durable owner exists"):
                recovery.attach_current_durable_owner_for_takeover()

    def test_source_attachment_is_independent_sender_and_admission_fence(self) -> None:
        with TemporaryDirectory() as directory:
            source = self._controller(directory)
            owner = source.start("host-old")

            recovery = self._controller(directory)
            self.assertEqual(
                recovery.attach_current_durable_owner_for_takeover(),
                owner,
            )
            self.assertTrue(recovery.takeover_source_only)

            # A current reconciliation can change ordinary readiness state, but it
            # must never turn the historical source owner back into a sender.
            recovery.state = HostState.READY
            recovery.provider_reconciled = True
            recovery.reason_codes.clear()

            with self.assertRaisesRegex(
                PermissionError,
                "Takeover source owner cannot regain sender authority",
            ):
                recovery.validate_sender(owner.owner_id, owner.epoch)
            with self.assertRaisesRegex(
                PermissionError,
                "Takeover source owner cannot regain admission authority",
            ):
                recovery.validate_admission(owner.epoch)
            with self.assertRaisesRegex(
                PermissionError,
                "has not advanced the source owner",
            ):
                recovery.activate_takeover_target_recovery()

    def test_only_exact_next_durable_owner_can_leave_source_only_mode(self) -> None:
        with TemporaryDirectory() as directory:
            source = self._controller(directory)
            old_owner = source.start("host-old")

            recovery = self._controller(directory)
            recovery.attach_current_durable_owner_for_takeover()

            wrong = OwnerFence(owner_id="host-new", epoch=old_owner.epoch + 2)
            recovery.owner = wrong
            with self.assertRaisesRegex(
                PermissionError,
                "owner epoch is not the next generation",
            ):
                recovery.activate_takeover_target_recovery()
            self.assertTrue(recovery.takeover_source_only)

            target = OwnerFence(owner_id="host-new", epoch=old_owner.epoch + 1)
            recovery._append_durable_owner(target)
            recovery.owner = target
            activated = recovery.activate_takeover_target_recovery()

            self.assertEqual(activated, target)
            self.assertFalse(recovery.takeover_source_only)
            self.assertIs(recovery.state, HostState.RECOVERING)
            self.assertFalse(recovery.provider_reconciled)
            self.assertIn("startup_reconciliation_required", recovery.reason_codes)

            # Advancing ownership is not readiness. A fresh target reconciliation
            # is still mandatory before sender validation can pass.
            with self.assertRaisesRegex(PermissionError, "not ready"):
                recovery.validate_sender(target.owner_id, target.epoch)


if __name__ == "__main__":
    unittest.main()
