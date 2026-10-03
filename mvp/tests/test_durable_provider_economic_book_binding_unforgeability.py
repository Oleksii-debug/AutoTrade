from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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

            # The process-local registry and its mutable authority record are
            # importable caller-writable Python state. Retargeting the visible
            # store and that registry record together must not redefine which
            # durable financial history the book originally selected.
            binding = authority._DURABLE_PROVIDER_ECONOMIC_BOOK_AUTHORITIES[book]
            vars(book)["store"] = replacement
            binding.store = replacement
            binding.store_identity = require_exact_journal_store_authority(
                replacement,
                subject="adversarial replacement economic JournalStore",
            )

            with self.assertRaises(AccountingConflict):
                book.refresh()


if __name__ == "__main__":
    unittest.main()
