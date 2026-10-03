from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts.store import ArtifactStore
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.securities_borrow import (
    BorrowRecallConflict,
    DurableBorrowRecallProjection,
)


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


class _JournalSubclass(JournalStore):
    def load_events(self, *_args, **_kwargs):
        raise AssertionError("JournalStore subclass virtual dispatch must not run")


def _projection(store: JournalStore, root: Path) -> DurableBorrowRecallProjection:
    return DurableBorrowRecallProjection(
        store,
        provider_id="TEST_PROVIDER",
        account_id="acct-1",
        environment="SIMULATION",
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        evidence_artifact_store=ArtifactStore(root / "artifacts"),
    )


class SecuritiesBorrowJournalAuthorityTests(unittest.TestCase):
    def test_constructor_rejects_journal_subclass_before_restore_dispatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = _JournalSubclass(root / "journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                _projection(store, root)

    def test_constructor_rejects_instance_shadow_before_restore_dispatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            executed = False

            def hostile_load_events(*_args, **_kwargs):
                nonlocal executed
                executed = True
                raise AssertionError("instance shadow must not execute")

            store.load_events = hostile_load_events
            with self.assertRaisesRegex(TypeError, "shadowed"):
                _projection(store, root)
            self.assertFalse(executed)

    def test_post_construction_shadow_fails_before_journal_io(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            projection = _projection(store, root)
            executed = False

            def hostile_append_event(*_args, **_kwargs):
                nonlocal executed
                executed = True
                raise AssertionError("instance shadow must not execute")

            store.append_event = hostile_append_event
            with self.assertRaisesRegex(TypeError, "shadowed"):
                projection._append(
                    event_type="BorrowRecallObserved",
                    identity="shadowed-event",
                    payload={"operation": "RECALL", "evidence": {}},
                    committed_at="2026-09-25T05:00:00Z",
                )
            self.assertFalse(executed)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "securities_borrow_recall",
                    projection.aggregate_id,
                ),
                [],
            )

    def test_post_construction_store_swap_fails_before_either_journal_mutates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = JournalStore(root / "first.sqlite3")
            second = JournalStore(root / "second.sqlite3")
            projection = _projection(first, root)
            projection.store = second

            with self.assertRaisesRegex(
                BorrowRecallConflict,
                "JournalStore changed",
            ):
                projection._events()

            self.assertEqual(
                JournalStore.load_events(
                    first,
                    "securities_borrow_recall",
                    projection.aggregate_id,
                ),
                [],
            )
            self.assertEqual(
                JournalStore.load_events(
                    second,
                    "securities_borrow_recall",
                    projection.aggregate_id,
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
