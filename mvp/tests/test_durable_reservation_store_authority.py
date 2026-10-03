from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import durable_reservations as reservation_authority
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import ReservationConflict


class DurableReservationStoreAuthorityTests(unittest.TestCase):
    def _book(self, store: JournalStore) -> DurableReservationBook:
        return DurableReservationBook(
            store,
            environment="PAPER",
            account_id="acct-reservation-authority",
        )

    def test_journal_store_subclass_is_not_reservation_authority(self):
        with TemporaryDirectory() as directory:
            class HostileStore(JournalStore):
                pass

            store = HostileStore(Path(directory) / "hostile.sqlite3")
            with self.assertRaises(TypeError):
                self._book(store)

    def test_instance_method_shadow_cannot_replace_reservation_replay(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                return []

            vars(book)["_events"] = hostile
            with self.assertRaisesRegex(
                ReservationConflict,
                "instance state is shadowed",
            ):
                book._reload()
            self.assertEqual(called, [])

    def test_selected_journal_store_method_shadow_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                return []

            vars(store)["load_events"] = hostile
            with self.assertRaisesRegex(
                ReservationConflict,
                "JournalStore authority changed",
            ):
                book._reload()
            self.assertEqual(called, [])

    def test_scope_retarget_fails_before_reservation_replay(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            object.__setattr__(book, "account_id", "different-account")
            with self.assertRaisesRegex(
                ReservationConflict,
                "scope changed after construction",
            ):
                book._reload()

    def test_imported_module_has_no_raw_reservation_registrar(self):
        self.assertFalse(
            hasattr(
                reservation_authority,
                "_register_reservation_store_binding",
            )
        )

    def test_reinitialization_cannot_retarget_reservation_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = self._book(selected)

            with self.assertRaisesRegex(
                ReservationConflict,
                "already established",
            ):
                reservation_authority._initialize_reservation_store_binding(
                    book,
                    replacement,
                    environment="PAPER",
                    account_id="acct-reservation-authority",
                )

            self.assertIs(vars(book)["store"], selected)
            book._reload()

    def test_caller_cannot_retarget_reservation_history_after_construction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = self._book(selected)

            # Reservation authority must remain bound to the originally selected
            # physical JournalStore generation. Caller-writable instance state
            # cannot redefine which durable history owns reserved capital.
            vars(book)["store"] = replacement

            with self.assertRaises(ReservationConflict):
                book._reload()


if __name__ == "__main__":
    unittest.main()
