from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController


class RecoveryOwnerStoreDowngradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(Path(self.directory.name) / "journal.sqlite3")
        self.recovery = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:acct",
        )
        self.recovery.start("host-a")

    def _remove_bound_store(self) -> None:
        # Construction-time path/identity remain pinned. Removing only the
        # mutable object reference must therefore be treated as authority
        # corruption, never as a downgrade to memory-only recovery semantics.
        self.recovery._owner_store = None

    def test_current_owner_validation_rejects_bound_store_removal(self) -> None:
        self._remove_bound_store()

        with self.assertRaisesRegex(
            PermissionError,
            "recovery owner journal authority changed",
        ):
            self.recovery._require_current_durable_owner()

    def test_scoped_unknown_recovery_rejects_bound_store_removal(self) -> None:
        self._remove_bound_store()

        with self.assertRaisesRegex(
            PermissionError,
            "recovery owner journal authority changed",
        ):
            self.recovery._recover_scoped_submission_uncertainty_from_owner_scope()

    def test_record_reconciliation_cannot_downgrade_durable_controller(self) -> None:
        self._remove_bound_store()

        with self.assertRaisesRegex(
            PermissionError,
            "recovery owner journal authority changed",
        ):
            self.recovery.record_reconciliation(consistent=True)

    def test_transfer_owner_cannot_downgrade_to_memory_only_path(self) -> None:
        self._remove_bound_store()

        with self.assertRaisesRegex(
            PermissionError,
            "recovery owner journal authority changed",
        ):
            self.recovery.transfer_owner(
                new_owner_id="host-b",
                old_sender_fenced=True,
                reconciled=True,
            )


if __name__ == "__main__":
    unittest.main()
