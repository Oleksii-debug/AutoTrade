from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
            evidence_artifact_store=ArtifactStore(root / "evidence"),
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
