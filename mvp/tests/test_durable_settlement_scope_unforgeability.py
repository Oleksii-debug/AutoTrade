from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import durable_settlement as settlement_authority
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.settlement import SettlementAccountScope, SettlementConflict
from research.autotrade_research.artifacts.store import ArtifactStore


class DurableSettlementScopeUnforgeabilityTests(unittest.TestCase):
    def test_caller_cannot_retarget_account_scope_within_same_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            book = DurableSettlementBook(
                store,
                provider_id="PROVIDER-A",
                account_id="acct-selected",
                environment="PAPER",
                evidence_artifact_store=ArtifactStore(root / "evidence"),
            )

            replacement_scope = SettlementAccountScope(
                provider_id="PROVIDER-A",
                account_id="acct-other",
                environment="PAPER",
            )
            book.scope = replacement_scope
            book.scope_id = settlement_authority._scope_id(replacement_scope)

            with self.assertRaises(SettlementConflict):
                book.refresh()


if __name__ == "__main__":
    unittest.main()
