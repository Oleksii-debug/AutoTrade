import unittest
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.sender_gate import journal_sender_gate


class RecoveryTakeoverAuthorityTests(unittest.TestCase):
    def test_product_durable_takeover_advances_epoch_without_caller_booleans(self):
        with TemporaryDirectory() as directory:
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
            owner = restarted.takeover_durable_owner("owner-b")

            self.assertEqual(owner.owner_id, "owner-b")
            self.assertEqual(owner.epoch, 2)
            self.assertEqual(restarted.state, HostState.RECOVERING)
            self.assertFalse(restarted.provider_reconciled)
            self.assertIn(
                "startup_reconciliation_required",
                restarted.reason_codes,
            )
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in restarted.durable_owner_chain()],
                [("owner-a", 1), ("owner-b", 2)],
            )

    def test_stale_process_fails_sender_validation_after_durable_takeover(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            old = first.start("owner-a")

            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            restarted.takeover_durable_owner("owner-b")

            with self.assertRaisesRegex(
                PermissionError,
                "Durable sender fence no longer belongs",
            ):
                first.validate_sender(old.owner_id, old.epoch)

    def test_takeover_cannot_reenter_sender_gate(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            first = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            first.start("owner-a")
            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )

            with journal_sender_gate(store):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "sender gate re-entry",
                ):
                    restarted.takeover_durable_owner("owner-b")

            self.assertEqual(
                [(item.owner_id, item.epoch) for item in restarted.durable_owner_chain()],
                [("owner-a", 1)],
            )

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
            path = f"{directory}/journal.sqlite3"
            first = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            owner = first.start("owner-a")
            self.assertEqual(owner.epoch, 1)

            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:acct",
            )
            with self.assertRaisesRegex(
                PermissionError,
                "independently authorized takeover",
            ):
                restarted.start("owner-b")

            self.assertIsNone(restarted.owner)
            chain = restarted.durable_owner_chain()
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in chain],
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

            # Even maximally favorable caller booleans cannot stand in for the
            # independent old-sender/process/session/credential fence and
            # accepted reconciliation authority required by the durable path.
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
            chain = controller.durable_owner_chain()
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in chain],
                [("owner-a", 1)],
            )


if __name__ == "__main__":
    unittest.main()
