from pathlib import Path
from tempfile import TemporaryDirectory
import gc
import threading
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

    def test_live_book_retains_selected_store_after_external_reference_release(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            store_ref = weakref.ref(selected)
            book = self._book(selected, root)

            del selected
            gc.collect()

            retained = store_ref()
            self.assertIsNotNone(retained)
            bound_store, _identity = book._selected_store()
            self.assertIs(bound_store, retained)

            del bound_store
            del retained
            book_ref = weakref.ref(book)
            del book
            gc.collect()

            self.assertIsNone(book_ref())
            self.assertIsNone(store_ref())

    def test_caller_cannot_retarget_trusted_evidence_reader(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            book = self._book(selected, root)
            original_reader = settlement_authority._durable_settlement_evidence_reader(
                book
            )

            book._settlement_evidence_reader = object()

            with self.assertRaisesRegex(
                SettlementConflict,
                "durable settlement evidence authority changed",
            ):
                settlement_authority._durable_settlement_evidence_reader(book)

            self.assertIsNotNone(original_reader)

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

    def test_failed_initial_reload_releases_bindings_for_safe_same_object_retry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            artifacts = ArtifactStore(root / "evidence")
            book = object.__new__(DurableSettlementBook)

            original_reload = DurableSettlementBook._reload

            def fail_initial_reload(_value) -> None:
                raise RuntimeError("forced initial settlement reload failure")

            DurableSettlementBook._reload = fail_initial_reload
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "forced initial settlement reload failure",
                ):
                    book.__init__(
                        selected,
                        provider_id="PROVIDER-A",
                        account_id="acct-1",
                        environment="PAPER",
                        evidence_artifact_root=root / "evidence",
                        evidence_artifact_store=artifacts,
                    )
            finally:
                DurableSettlementBook._reload = original_reload

            with self.assertRaisesRegex(
                SettlementConflict,
                "evidence authority is unavailable",
            ):
                settlement_authority._durable_settlement_evidence_reader(book)
            with self.assertRaisesRegex(
                SettlementConflict,
                "binding is unavailable",
            ):
                settlement_authority._bound_durable_settlement_store(book)

            book.__init__(
                selected,
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
                evidence_artifact_root=root / "evidence",
                evidence_artifact_store=artifacts,
            )
            bound_store, _identity = book._selected_store()
            self.assertIs(bound_store, selected)
            self.assertIsNotNone(
                settlement_authority._durable_settlement_evidence_reader(book)
            )
            book.refresh()

    def test_store_binding_is_not_observable_until_initial_reload_completes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = JournalStore(root / "selected.sqlite")
            artifacts = ArtifactStore(root / "evidence")
            book = object.__new__(DurableSettlementBook)

            original_reload = DurableSettlementBook._reload
            reload_started = threading.Event()
            release_reload = threading.Event()
            initializer_done = threading.Event()
            observer_entered = threading.Event()
            observer_done = threading.Event()
            outcomes: dict[str, object] = {}
            outcomes_lock = threading.Lock()

            def fenced_reload(value) -> None:
                reload_started.set()
                if not release_reload.wait(5):
                    raise AssertionError("initial settlement reload rendezvous timed out")
                original_reload(value)

            def initialize() -> None:
                try:
                    book.__init__(
                        selected,
                        provider_id="PROVIDER-A",
                        account_id="acct-1",
                        environment="PAPER",
                        evidence_artifact_root=root / "evidence",
                        evidence_artifact_store=artifacts,
                    )
                except BaseException as error:  # captured for cross-thread assertion
                    with outcomes_lock:
                        outcomes["initialize_error"] = error
                finally:
                    initializer_done.set()

            def observe() -> None:
                observer_entered.set()
                try:
                    observed = settlement_authority._bound_durable_settlement_store(
                        book
                    )
                    with outcomes_lock:
                        outcomes["observed_store"] = observed[0]
                except BaseException as error:  # captured for cross-thread assertion
                    with outcomes_lock:
                        outcomes["observe_error"] = error
                finally:
                    observer_done.set()

            DurableSettlementBook._reload = fenced_reload
            initializer_thread = threading.Thread(target=initialize)
            observer_thread = threading.Thread(target=observe)
            try:
                initializer_thread.start()
                self.assertTrue(reload_started.wait(5))
                self.assertFalse(initializer_done.is_set())

                observer_thread.start()
                self.assertTrue(observer_entered.wait(5))

                # The store row exists for the initializer's re-entrant replay,
                # but no competing thread may acquire it while _book still
                # represents the pre-replay empty state.
                self.assertFalse(observer_done.wait(0.25))

                release_reload.set()
                initializer_thread.join(5)
                observer_thread.join(5)
            finally:
                release_reload.set()
                DurableSettlementBook._reload = original_reload
                initializer_thread.join(5)
                observer_thread.join(5)

            self.assertFalse(initializer_thread.is_alive())
            self.assertFalse(observer_thread.is_alive())
            self.assertTrue(initializer_done.is_set())
            self.assertTrue(observer_done.is_set())
            self.assertNotIn("initialize_error", outcomes)
            self.assertNotIn("observe_error", outcomes)
            self.assertIs(outcomes.get("observed_store"), selected)
            book.refresh()

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
