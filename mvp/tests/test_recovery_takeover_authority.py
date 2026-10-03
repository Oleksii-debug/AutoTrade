import unittest
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.sender_gate import journal_sender_gate


class RecoveryTakeoverAuthorityTests(unittest.TestCase):
    @staticmethod
    def _restart_pair(directory: str):
        path = f"{directory}/journal.sqlite3"
        first = RecoveryController(
            owner_store=JournalStore(path),
            owner_scope="PAPER:acct",
        )
        first.start("owner-a")
        restarted = RecoveryController(
            owner_store=JournalStore(path),
            owner_scope="PAPER:acct",
        )
        return path, first, restarted

    def test_durable_takeover_without_independent_authority_is_rejected(self):
        with TemporaryDirectory() as directory:
            _path, _first, restarted = self._restart_pair(directory)
            with self.assertRaisesRegex(
                PermissionError,
                "independently issued external fence authority",
            ):
                restarted.takeover_durable_owner("owner-b")
            self.assertIsNone(restarted.owner)
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in restarted.durable_owner_chain()],
                [("owner-a", 1)],
            )

    def test_same_journal_sender_gate_is_mechanism_not_takeover_authority(self):
        with TemporaryDirectory() as directory:
            path, _first, restarted = self._restart_pair(directory)
            with journal_sender_gate(JournalStore(path)):
                with self.assertRaisesRegex(
                    PermissionError,
                    "independently issued external fence authority",
                ):
                    restarted.takeover_durable_owner("owner-b")
            self.assertIsNone(restarted.owner)
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in restarted.durable_owner_chain()],
                [("owner-a", 1)],
            )

    def test_denied_takeover_preserves_durable_owner_generation(self):
        with TemporaryDirectory() as directory:
            _path, first, restarted = self._restart_pair(directory)
            current = first.owner
            self.assertIsNotNone(current)
            with self.assertRaises(PermissionError):
                restarted.takeover_durable_owner("owner-b")
            self.assertEqual(restarted.durable_owner_chain(), (current,))
            self.assertIsNone(restarted.owner)

    def test_takeover_requires_existing_durable_owner(self):
        with TemporaryDirectory() as directory:
            controller = RecoveryController(
                owner_store=JournalStore(f"{directory}/journal.sqlite3"),
                owner_scope="PAPER:acct",
            )
            with self.assertRaisesRegex(
                PermissionError,
                "initial ownership must use start",
            ):
                controller.takeover_durable_owner("owner-b")
            self.assertIsNone(controller.owner)

    def test_recovery_subclass_cannot_issue_durable_takeover(self):
        class ForgedRecovery(RecoveryController):
            pass

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            first.start("owner-a")
            forged = ForgedRecovery(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            with self.assertRaisesRegex(
                TypeError,
                "exact RecoveryController",
            ):
                forged.takeover_durable_owner("owner-b")

    def test_restart_cannot_mint_next_durable_owner_epoch(self):
        with TemporaryDirectory() as directory:
            _path, _first, restarted = self._restart_pair(directory)
            with self.assertRaisesRegex(
                PermissionError,
                "independently authorized takeover",
            ):
                restarted.start("owner-b")
            self.assertIsNone(restarted.owner)
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in restarted.durable_owner_chain()],
                [("owner-a", 1)],
            )

    def test_legacy_boolean_takeover_cannot_advance_durable_owner(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            controller = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="LIVE:acct",
            )
            controller.start("owner-a")
            controller.provider_reconciled = True
            with self.assertRaisesRegex(
                PermissionError,
                "independently issued takeover evidence",
            ):
                controller.transfer_owner(
                    new_owner_id="owner-b",
                    old_sender_fenced=True,
                    reconciled=True,
                )
            self.assertEqual(controller.owner.owner_id, "owner-a")
            self.assertEqual(controller.owner.epoch, 1)
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in controller.durable_owner_chain()],
                [("owner-a", 1)],
            )


if __name__ == "__main__":
    unittest.main()
