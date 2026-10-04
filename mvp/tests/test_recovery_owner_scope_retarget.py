from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import OwnerFence, RecoveryController


class RecoveryOwnerScopeRetargetTests(unittest.TestCase):
    def test_same_fence_values_in_another_scope_cannot_retarget_durable_owner_authority(self):
        """Store identity alone cannot bind which owner aggregate the controller owns."""

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:account-a",
            )
            second = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:account-b",
            )

            self.assertEqual(first.start("shared-host"), OwnerFence("shared-host", 1))
            self.assertEqual(second.start("shared-host"), OwnerFence("shared-host", 1))

            # Both aggregates intentionally have the same owner_id/epoch values.
            # Retargeting only the mutable scope therefore cannot be detected by
            # comparing the returned OwnerFence to first.owner; the controller
            # must retain and revalidate its construction-time scope authority.
            object.__setattr__(first, "_owner_scope", "PAPER:account-b")

            with self.assertRaisesRegex(
                PermissionError,
                "scope|authority|changed",
            ):
                first.durable_owner_chain()
            with self.assertRaisesRegex(
                PermissionError,
                "scope|authority|changed",
            ):
                first._require_current_durable_owner()


if __name__ == "__main__":
    unittest.main()
