from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_mvp.persistence import JournalStore
from autotrade_mvp.recovery import RecoveryController
from autotrade_mvp.sender_gate import journal_sender_gate


class RecoverySenderGateCurrentTests(unittest.TestCase):
    def test_sender_gate_reentry_is_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with journal_sender_gate(store):
                with self.assertRaisesRegex(RuntimeError, "re-entry"):
                    with journal_sender_gate(store):
                        self.fail("nested sender gate unexpectedly entered")

    def test_recovery_start_uses_same_journal_sender_gate(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:sender-gate-account",
            )
            with journal_sender_gate(store):
                with self.assertRaisesRegex(RuntimeError, "re-entry"):
                    controller.start("host-a")
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "recovery_owner",
                    "PAPER:sender-gate-account",
                ),
                [],
            )

    def test_sender_gate_rejects_journalstore_subclass(self) -> None:
        class HostileStore(JournalStore):
            pass

        forged = object.__new__(HostileStore)
        with self.assertRaises(TypeError):
            with journal_sender_gate(forged):
                self.fail("hostile JournalStore subclass entered sender gate")


if __name__ == "__main__":
    unittest.main()
