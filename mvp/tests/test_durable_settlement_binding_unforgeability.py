from pathlib import Path
from tempfile import TemporaryDirectory
import gc
import unittest
import weakref

from mvp.autotrade_mvp import durable_settlement as settlement_authority
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    require_exact_journal_store_authority,
)
from mvp.autotrade_mvp.settlement import SettlementConflict
from research.autotrade_research.artifacts.store import ArtifactStore


class DurableSettlementBindingUnforgeabilityTests(unittest.TestCase):
    def _book(self, store: JournalStore, root: Path) -> DurableSettlementBook:
        return DurableSettlementBook(
            store,
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            evidence_artifact_root=root / "evidence",
            evidence_artifact_store=ArtifactStore(root / "evidence"),
        )

    def test_live_book_exposes_no_callable_binding_removal_callback(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            replacement = JournalStore(root / "replacement.sqlite")
            artifacts = ArtifactStore(root / "evidence")
            book = DurableSettlementBook(
                selected,
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
                evidence_artifact_root=root / "evidence",
                evidence_artifact_store=artifacts,
            )
            original_reader = settlement_authority._durable_settlement_evidence_reader(
                book
            )

            callbacks = [
                reference.__callback__
                for reference in weakref.getweakrefs(book)
                if reference.__callback__ is not None
            ]
            self.assertEqual(callbacks, [])

            with self.assertRaisesRegex(
                SettlementConflict,
                "evidence authority is already bound|authority is already bound",
            ):
                book.__init__(
                    replacement,
                    provider_id="PROVIDER-A",
                    account_id="acct-1",
                    environment="PAPER",
                    evidence_artifact_root=root / "evidence",
                    evidence_artifact_store=artifacts,
                )

            bound_store, _identity = book._selected_store()
            self.assertIs(bound_store, selected)
            self.assertIs(
                settlement_authority._durable_settlement_evidence_reader(book),
                original_reader,
            )

    def test_destroyed_book_releases_store_and_reader_without_next_bind(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            artifacts = ArtifactStore(root / "evidence")
            book = DurableSettlementBook(
                selected,
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
                evidence_artifact_root=root / "evidence",
                evidence_artifact_store=artifacts,
            )
            book_ref = weakref.ref(book)
            store_ref = weakref.ref(selected)
            reader_ref = weakref.ref(
                settlement_authority._durable_settlement_evidence_reader(book)
            )

            del book
            gc.collect()
            self.assertIsNone(book_ref())
            self.assertIsNone(reader_ref())

            del selected
            gc.collect()
            self.assertIsNone(store_ref())

    def test_original_store_binding_registry_is_not_module_mutable_state(self):
        self.assertFalse(
            hasattr(settlement_authority, "_DURABLE_SETTLEMENT_STORE_BINDINGS")
        )
        self.assertFalse(
            hasattr(
                settlement_authority,
                "_DURABLE_SETTLEMENT_STORE_BINDINGS_LOCK",
            )
        )
        self.assertFalse(
            hasattr(
                settlement_authority,
                "_install_durable_settlement_store_binding",
            )
        )

    def test_caller_cannot_retarget_store_and_visible_identity_together(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            replacement = JournalStore(root / "replacement.sqlite")
            book = self._book(selected, root)

            # Financial composition authority must not live in caller-mutable
            # instance attributes. Rebinding both fields to a second legitimate
            # exact JournalStore must still fail closed.
            book.store = replacement
            book._store_identity = require_exact_journal_store_authority(
                replacement,
                subject="adversarial replacement JournalStore",
            )

            with self.assertRaises(SettlementConflict):
                book.refresh()


if __name__ == "__main__":
    unittest.main()
