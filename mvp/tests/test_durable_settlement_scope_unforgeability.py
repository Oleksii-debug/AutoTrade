from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import durable_settlement as settlement_authority
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.settlement import SettlementAccountScope, SettlementConflict
from research.autotrade_research.artifacts.store import ArtifactStore


class DurableSettlementScopeUnforgeabilityTests(unittest.TestCase):
    def _book(self, store: JournalStore, root: Path) -> DurableSettlementBook:
        return DurableSettlementBook(
            store,
            provider_id="PROVIDER-A",
            account_id="acct-selected",
            environment="PAPER",
            evidence_artifact_store=ArtifactStore(root / "evidence"),
        )

    def test_reinitialization_cannot_poison_original_settlement_composition(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = self._book(selected, root)

            with self.assertRaises((SettlementConflict, RuntimeError)):
                book.__init__(
                    replacement,
                    provider_id="PROVIDER-A",
                    account_id="acct-other",
                    environment="PAPER",
                    evidence_artifact_store=ArtifactStore(root / "other-evidence"),
                )

            bound_store, _bound_identity = book._selected_store()
            self.assertIs(bound_store, selected)
            self.assertIs(book.store, selected)
            self.assertEqual(book.scope.account_id, "acct-selected")
            self.assertEqual(book.scope_id, settlement_authority._scope_id(book.scope))

    def test_caller_cannot_retarget_account_scope_within_same_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            book = self._book(store, root)

            replacement_scope = SettlementAccountScope(
                provider_id="PROVIDER-A",
                account_id="acct-other",
                environment="PAPER",
            )
            book.scope = replacement_scope
            book.scope_id = settlement_authority._scope_id(replacement_scope)

            with self.assertRaises(SettlementConflict):
                book.refresh()

    def test_caller_cannot_retarget_scope_value_while_scope_id_stays_selected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            book = self._book(store, root)
            selected_scope_id = book.scope_id

            book.scope = SettlementAccountScope(
                provider_id="PROVIDER-A",
                account_id="acct-other",
                environment="PAPER",
            )
            self.assertEqual(book.scope_id, selected_scope_id)

            with self.assertRaises(SettlementConflict):
                book.refresh()

    def test_caller_cannot_retarget_scope_id_while_scope_value_stays_selected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            book = self._book(store, root)
            selected_scope = book.scope

            replacement_scope = SettlementAccountScope(
                provider_id="PROVIDER-A",
                account_id="acct-other",
                environment="PAPER",
            )
            book.scope_id = settlement_authority._scope_id(replacement_scope)
            self.assertEqual(book.scope, selected_scope)

            with self.assertRaises(SettlementConflict):
                book.refresh()


if __name__ == "__main__":
    unittest.main()
