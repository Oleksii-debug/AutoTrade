from pathlib import Path
from tempfile import TemporaryDirectory
import gc
import threading
import unittest
import weakref

from mvp.autotrade_mvp.persistence import (
    JournalStore,
    require_exact_journal_store_authority,
)
from mvp.autotrade_mvp import valuation_authority as authority
from mvp.autotrade_mvp.valuation_authority import (
    DurableValuationBook,
    ValuationConflict,
)


class ValuationBookBindingUnforgeabilityTests(unittest.TestCase):
    def test_binding_weakrefs_expose_no_callable_removal_callback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = DurableValuationBook(selected)

            callbacks = [
                reference.__callback__
                for reference in weakref.getweakrefs(book)
                if reference.__callback__ is not None
            ]
            self.assertEqual(callbacks, [])

            with self.assertRaisesRegex(
                ValuationConflict,
                "already initialized|already established",
            ):
                book.__init__(replacement)

            bound_store, _bound_identity = book._journal_store_authority()
            self.assertIs(bound_store, selected)
            self.assertIs(book.store, selected)

    def test_live_book_retains_selected_store_after_external_reference_release(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            store_ref = weakref.ref(selected)
            book = DurableValuationBook(selected)

            del selected
            gc.collect()

            retained = store_ref()
            self.assertIsNotNone(retained)
            bound_store, _bound_identity = book._journal_store_authority()
            self.assertIs(bound_store, retained)
            self.assertIs(book.store, retained)

    def test_destroyed_book_does_not_leave_registry_retaining_store(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            store_ref = weakref.ref(selected)
            book = DurableValuationBook(selected)
            book_ref = weakref.ref(book)

            del selected
            gc.collect()
            self.assertIsNotNone(store_ref())

            del book
            gc.collect()
            self.assertIsNone(book_ref())
            self.assertIsNone(store_ref())

    def test_reinitialization_cannot_retarget_original_financial_store(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = DurableValuationBook(selected)
            selected_identity = require_exact_journal_store_authority(
                selected,
                subject="selected valuation JournalStore",
            )

            with self.assertRaisesRegex(
                ValuationConflict,
                "already initialized|already established",
            ):
                book.__init__(replacement)

            bound_store, bound_identity = book._journal_store_authority()
            self.assertIs(bound_store, selected)
            self.assertEqual(bound_identity, selected_identity)
            self.assertIs(book.store, selected)

    def test_importable_historical_binding_names_cannot_retarget_financial_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = DurableValuationBook(selected)
            replacement_identity = require_exact_journal_store_authority(
                replacement,
                subject="adversarial replacement valuation JournalStore",
            )

            # Recreate the historical importable module-global surfaces even if
            # production no longer defines them. Closure-owned binding authority
            # must be unaffected by these names and by matching diagnostic slots.
            authority._VALUATION_BOOK_BINDINGS = {
                id(book): (
                    weakref.ref(book),
                    weakref.ref(replacement),
                    replacement_identity,
                )
            }
            authority._VALUATION_BOOK_BINDINGS_LOCK = threading.RLock()
            try:
                book.store = replacement
                book.store_identity = replacement_identity
                book.store_identity_digest = authority._store_identity_digest(
                    replacement_identity
                )

                with self.assertRaises(ValuationConflict):
                    book._journal_store_authority()
            finally:
                del authority._VALUATION_BOOK_BINDINGS
                del authority._VALUATION_BOOK_BINDINGS_LOCK


if __name__ == "__main__":
    unittest.main()
