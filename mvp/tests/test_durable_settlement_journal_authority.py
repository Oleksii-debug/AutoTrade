from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.settlement import SettlementConflict
from research.autotrade_research.artifacts.store import ArtifactStore


class _HostileJournalStore(JournalStore):
    def __init__(self, path):
        super().__init__(path)
        self.hostile_load_calls = 0

    def load_events(self, aggregate_type, aggregate_id):
        self.hostile_load_calls += 1
        return []


class DurableSettlementJournalAuthorityTests(unittest.TestCase):
    @staticmethod
    def _book(store: JournalStore, root: Path) -> DurableSettlementBook:
        return DurableSettlementBook(
            store,
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            evidence_artifact_store=ArtifactStore(root / "evidence"),
        )

    def test_hostile_journal_subclass_is_rejected_before_virtual_replay(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _HostileJournalStore(root / "journal.sqlite")

            with self.assertRaises(TypeError):
                self._book(store, root)

            self.assertEqual(store.hostile_load_calls, 0)

    def test_rebinding_book_to_another_exact_store_generation_fails_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            replacement = JournalStore(root / "replacement.sqlite")
            book = self._book(selected, root)

            book.store = replacement

            with self.assertRaisesRegex(
                SettlementConflict,
                "JournalStore generation changed",
            ):
                book.refresh()

    def test_instance_method_shadow_is_rejected_before_settlement_replay(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JournalStore(root / "journal.sqlite")
            book = self._book(store, root)
            calls = []

            store.load_events = lambda *_args, **_kwargs: calls.append(True) or []

            with self.assertRaisesRegex(TypeError, "instance state is shadowed"):
                book.refresh()

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
