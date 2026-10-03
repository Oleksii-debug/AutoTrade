from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import weakref

from mvp.autotrade_mvp.accounting import AccountingConflict
from mvp.autotrade_mvp import _provider_activity_accounting_impl as authority
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    require_exact_journal_store_authority,
)
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class DurableProviderEconomicBookBindingUnforgeabilityTests(unittest.TestCase):
    def test_imported_authority_registry_cannot_retarget_financial_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = DurableProviderEconomicBook(
                selected,
                provider_id="PROVIDER-A",
                account_id="acct-authority",
                environment="PAPER",
            )

            # Original composition state must not be reachable as a mutable
            # module-global registry/record. The verifier may return a detached
            # snapshot, but rewriting that snapshot cannot rewrite issuance truth.
            self.assertFalse(
                hasattr(authority, "_DURABLE_PROVIDER_ECONOMIC_BOOK_AUTHORITIES")
            )
            self.assertFalse(
                hasattr(authority, "_register_durable_provider_economic_book_authority")
            )
            self.assertFalse(
                hasattr(authority, "_update_durable_provider_economic_projection_authority")
            )

            detached = authority._require_durable_provider_economic_book_authority(book)
            detached.store = replacement
            detached.store_identity = require_exact_journal_store_authority(
                replacement,
                subject="adversarial detached replacement economic JournalStore",
            )

            # Mutating a detached verifier result cannot retarget the real book.
            book.refresh()
            self.assertIs(vars(book)["store"], selected)

            # Even if the caller also rewrites the visible instance store, the
            # closure-owned original binding still rejects the substitution.
            vars(book)["store"] = replacement
            with self.assertRaisesRegex(
                AccountingConflict,
                "JournalStore changed",
            ):
                book.refresh()

    def test_binding_weakref_exposes_no_erasable_callback(self):
        with TemporaryDirectory() as directory:
            selected = JournalStore(Path(directory) / "selected.sqlite3")
            book = DurableProviderEconomicBook(
                selected,
                provider_id="PROVIDER-A",
                account_id="acct-authority",
                environment="PAPER",
            )

            binding_refs = weakref.getweakrefs(book)
            self.assertTrue(binding_refs)
            self.assertTrue(
                all(ref.__callback__ is None for ref in binding_refs)
            )

            detached = authority._require_durable_provider_economic_book_authority(
                book
            )
            self.assertIs(detached.store, selected)
            book.refresh()

    def test_reinitialization_cannot_retarget_existing_financial_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = DurableProviderEconomicBook(
                selected,
                provider_id="PROVIDER-A",
                account_id="acct-authority",
                environment="PAPER",
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "already established",
            ):
                authority._initialize_durable_provider_economic_book(
                    book,
                    replacement,
                    provider_id="PROVIDER-A",
                    account_id="acct-authority",
                    environment="PAPER",
                )

            self.assertIs(vars(book)["store"], selected)
            book.refresh()


if __name__ == "__main__":
    unittest.main()
