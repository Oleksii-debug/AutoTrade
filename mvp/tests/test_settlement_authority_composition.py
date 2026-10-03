from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.settlement import SettlementAccountScope, SettlementConflict


class SettlementAuthorityCompositionTests(unittest.TestCase):
    def _book(
        self,
        root: Path,
        *,
        store: JournalStore,
        provider_environment: str,
    ) -> DurableSettlementBook:
        evidence_root = root / "evidence"
        artifacts = ArtifactStore(evidence_root)
        return DurableSettlementBook(
            store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment=provider_environment,
            evidence_artifact_root=evidence_root,
            evidence_artifact_store=artifacts,
        )

    def test_provider_domain_is_part_of_retained_store_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            testnet = self._book(
                root / "testnet",
                store=store,
                provider_environment="TESTNET",
            )
            demo = self._book(
                root / "demo",
                store=store,
                provider_environment="DEMO",
            )

            self.assertNotEqual(testnet.scope_id, demo.scope_id)
            self.assertEqual(testnet.scope.provider_environment, "TESTNET")
            self.assertEqual(demo.scope.provider_environment, "DEMO")
            self.assertEqual(testnet.obligations, ())
            self.assertEqual(demo.obligations, ())

    def test_visible_provider_domain_cannot_retarget_retained_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            book = self._book(
                root,
                store=store,
                provider_environment="TESTNET",
            )
            book.scope = SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="DEMO",
            )

            with self.assertRaisesRegex(
                SettlementConflict,
                "scope changed after construction",
            ):
                _ = book.obligations

    def test_visible_store_cannot_retarget_provider_domain_book(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected_store = JournalStore(root / "selected.sqlite3")
            book = self._book(
                root,
                store=selected_store,
                provider_environment="TESTNET",
            )
            book.store = JournalStore(root / "attacker.sqlite3")

            with self.assertRaisesRegex(
                SettlementConflict,
                "JournalStore generation changed",
            ):
                book.refresh()


if __name__ == "__main__":
    unittest.main()
