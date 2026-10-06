from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autotrade_mvp.persistence import JournalStore
from autotrade_mvp.recovery import RecoveryController


class RecoveryStoreAuthorityTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "journal.sqlite3")

    def test_journalstore_subclass_cannot_be_recovery_authority(self) -> None:
        class HostileStore(JournalStore):
            pass
        forged = object.__new__(HostileStore)
        with self.assertRaises(TypeError):
            RecoveryController(owner_store=forged, owner_scope="PAPER:acct")

    def test_selected_physical_identity_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            controller = RecoveryController(owner_store=store, owner_scope="PAPER:acct")
            self.assertEqual(controller.durable_owner_store_identity, store.store_identity)
            self.assertEqual(controller.durable_owner_store_path, Path(store.store_identity.canonical_path))

    def test_instance_method_shadow_fails_before_recovery_read(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            controller = RecoveryController(owner_store=store, owner_scope="PAPER:acct")
            store.load_events = lambda *args, **kwargs: []
            with self.assertRaises(TypeError):
                controller.durable_owner_chain()

    def test_lost_physical_identity_fails_before_recovery_read(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            controller = RecoveryController(owner_store=store, owner_scope="PAPER:acct")
            store._store_identity = None
            with self.assertRaises(RuntimeError):
                controller.durable_owner_chain()


if __name__ == "__main__":
    unittest.main()
