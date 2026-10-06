from pathlib import Path
from tempfile import TemporaryDirectory
import gc
import threading
import unittest
import weakref
from types import MappingProxyType

from mvp.autotrade_mvp import durable_reservations as reservation_authority
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import ReservationConflict
from research.autotrade_research.artifacts import ArtifactStore



class _HostileText(str):
    def strip(self, *_args, **_kwargs):
        raise AssertionError("hostile strip dispatched")

    def upper(self, *_args, **_kwargs):
        raise AssertionError("hostile upper dispatched")


class _HostileMapping(dict):
    def __bool__(self):
        raise AssertionError("hostile mapping truth dispatched")

    def __len__(self):
        raise AssertionError("hostile mapping length dispatched")

    def __iter__(self):
        raise AssertionError("hostile mapping iteration dispatched")

    def items(self):
        raise AssertionError("hostile mapping items dispatched")


class DurableReservationStoreAuthorityTests(unittest.TestCase):
    def _book(self, store: JournalStore) -> DurableReservationBook:
        return DurableReservationBook(
            store,
            environment="PAPER",
            account_id="acct-reservation-authority",
        )

    def test_journal_store_subclass_is_not_reservation_authority(self):
        with TemporaryDirectory() as directory:
            class HostileStore(JournalStore):
                pass

            store = HostileStore(Path(directory) / "hostile.sqlite3")
            with self.assertRaises(TypeError):
                self._book(store)

    def test_instance_method_shadow_cannot_replace_reservation_replay(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                return []

            vars(book)["_events"] = hostile
            with self.assertRaisesRegex(
                ReservationConflict,
                "instance state is shadowed",
            ):
                book._reload()
            self.assertEqual(called, [])

    def test_selected_journal_store_method_shadow_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                return []

            vars(store)["load_events"] = hostile
            with self.assertRaisesRegex(
                ReservationConflict,
                "JournalStore authority changed",
            ):
                book._reload()
            self.assertEqual(called, [])

    def test_scope_retarget_fails_before_reservation_replay(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            object.__setattr__(book, "account_id", "different-account")
            with self.assertRaisesRegex(
                ReservationConflict,
                "scope changed after construction",
            ):
                book._reload()

    def test_imported_module_has_no_raw_reservation_registrar(self):
        self.assertFalse(
            hasattr(
                reservation_authority,
                "_register_reservation_store_binding",
            )
        )

    def test_reservation_authority_weakrefs_expose_no_removal_callback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = self._book(selected)

            callbacks = [
                reference.__callback__
                for reference in weakref.getweakrefs(book)
                if reference.__callback__ is not None
            ]
            self.assertEqual(callbacks, [])

            with self.assertRaisesRegex(
                ReservationConflict,
                "already established",
            ):
                reservation_authority._initialize_reservation_store_binding(
                    book,
                    replacement,
                    environment="PAPER",
                    account_id="acct-reservation-authority",
                )

            self.assertIs(vars(book)["store"], selected)
            book._reload()

    def test_concurrent_initializers_cannot_retarget_first_financial_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = object.__new__(DurableReservationBook)

            original_require = (
                reservation_authority.require_exact_journal_store_authority
            )
            selected_arrived = threading.Event()
            replacement_arrived = threading.Event()
            release_selected = threading.Event()
            release_replacement = threading.Event()
            outcomes: dict[str, BaseException | None] = {}
            outcomes_lock = threading.Lock()

            def fenced_require(store, *args, **kwargs):
                if store is selected:
                    selected_arrived.set()
                    if not release_selected.wait(5):
                        raise AssertionError("selected initializer rendezvous timed out")
                elif store is replacement:
                    replacement_arrived.set()
                    if not release_replacement.wait(5):
                        raise AssertionError("replacement initializer rendezvous timed out")
                return original_require(store, *args, **kwargs)

            def initialize(label: str, store: JournalStore) -> None:
                error = None
                try:
                    reservation_authority._initialize_reservation_store_binding(
                        book,
                        store,
                        environment="PAPER",
                        account_id="acct-reservation-authority",
                    )
                except BaseException as caught:  # captured for cross-thread assertion
                    error = caught
                with outcomes_lock:
                    outcomes[label] = error

            reservation_authority.require_exact_journal_store_authority = fenced_require
            try:
                selected_thread = threading.Thread(
                    target=initialize,
                    args=("selected", selected),
                )
                replacement_thread = threading.Thread(
                    target=initialize,
                    args=("replacement", replacement),
                )
                selected_thread.start()
                replacement_thread.start()

                self.assertTrue(selected_arrived.wait(5))
                self.assertTrue(replacement_arrived.wait(5))

                # Both calls are now past the old unlocked registration precheck.
                # Let the selected store publish first, then release the stale
                # competing initializer. It must recheck under the publication
                # lock instead of overwriting the first binding.
                release_selected.set()
                selected_thread.join(5)
                self.assertFalse(selected_thread.is_alive())

                release_replacement.set()
                replacement_thread.join(5)
                self.assertFalse(replacement_thread.is_alive())
            finally:
                release_selected.set()
                release_replacement.set()
                reservation_authority.require_exact_journal_store_authority = (
                    original_require
                )

            self.assertIsNone(outcomes.get("selected"))
            self.assertIsInstance(outcomes.get("replacement"), ReservationConflict)
            self.assertRegex(
                str(outcomes["replacement"]),
                "already established",
            )
            bound_store, _identity, _scope = (
                reservation_authority._require_reservation_store_binding(book)
            )
            self.assertIs(bound_store, selected)
            self.assertIs(vars(book)["store"], selected)
            book._reload()

    def test_destroyed_book_releases_selected_authorities_without_next_bind(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "selected.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            book = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="acct-reservation-authority",
                resolution_artifact_store=artifacts,
                resolution_artifact_root=root / "artifacts",
            )

            book_ref = weakref.ref(book)
            store_ref = weakref.ref(store)
            artifacts_ref = weakref.ref(artifacts)
            reader_ref = weakref.ref(vars(book)["_resolution_artifact_reader"])

            del book
            gc.collect()
            self.assertIsNone(book_ref())
            self.assertIsNone(reader_ref())

            del store
            del artifacts
            gc.collect()
            self.assertIsNone(store_ref())
            self.assertIsNone(artifacts_ref())

    def test_nonlocal_initial_reload_failure_releases_binding_for_retry(self):
        class ForcedAbort(BaseException):
            pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            book = object.__new__(DurableReservationBook)
            original_reload = DurableReservationBook._reload

            def abort_reload(_value) -> None:
                raise ForcedAbort("forced reservation initialization abort")

            DurableReservationBook._reload = abort_reload
            try:
                with self.assertRaisesRegex(
                    ForcedAbort,
                    "forced reservation initialization abort",
                ):
                    reservation_authority._initialize_reservation_store_binding(
                        book,
                        selected,
                        environment="PAPER",
                        account_id="acct-reservation-authority",
                    )
            finally:
                DurableReservationBook._reload = original_reload

            with self.assertRaisesRegex(
                ReservationConflict,
                "authority is not established",
            ):
                reservation_authority._require_reservation_store_binding(book)

            reservation_authority._initialize_reservation_store_binding(
                book,
                selected,
                environment="PAPER",
                account_id="acct-reservation-authority",
            )
            bound_store, _identity, _scope = (
                reservation_authority._require_reservation_store_binding(book)
            )
            self.assertIs(bound_store, selected)
            book._reload()

    def test_binding_is_not_observable_until_initial_reload_completes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            book = object.__new__(DurableReservationBook)

            original_reload = DurableReservationBook._reload
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
                    raise AssertionError("initial reload rendezvous timed out")
                original_reload(value)

            def initialize() -> None:
                try:
                    reservation_authority._initialize_reservation_store_binding(
                        book,
                        selected,
                        environment="PAPER",
                        account_id="acct-reservation-authority",
                    )
                except BaseException as error:  # captured for cross-thread assertion
                    with outcomes_lock:
                        outcomes["initialize_error"] = error
                finally:
                    initializer_done.set()

            def observe() -> None:
                observer_entered.set()
                try:
                    observed = reservation_authority._require_reservation_store_binding(
                        book
                    )
                    with outcomes_lock:
                        outcomes["observed_store"] = observed[0]
                except BaseException as error:  # captured for cross-thread assertion
                    with outcomes_lock:
                        outcomes["observe_error"] = error
                finally:
                    observer_done.set()

            DurableReservationBook._reload = fenced_reload
            initializer_thread = threading.Thread(target=initialize)
            observer_thread = threading.Thread(target=observe)
            try:
                initializer_thread.start()
                self.assertTrue(reload_started.wait(5))
                self.assertFalse(initializer_done.is_set())

                observer_thread.start()
                self.assertTrue(observer_entered.wait(5))

                # The authority is provisionally registered for the initializing
                # thread's re-entrant replay, but competing threads must not see
                # it until durable replay succeeds. Before this fence an
                # observer can complete against the empty in-memory projection.
                self.assertFalse(observer_done.wait(0.25))

                release_reload.set()
                initializer_thread.join(5)
                observer_thread.join(5)
            finally:
                release_reload.set()
                DurableReservationBook._reload = original_reload
                initializer_thread.join(5)
                observer_thread.join(5)

            self.assertFalse(initializer_thread.is_alive())
            self.assertFalse(observer_thread.is_alive())
            self.assertTrue(initializer_done.is_set())
            self.assertTrue(observer_done.is_set())
            self.assertNotIn("initialize_error", outcomes)
            self.assertNotIn("observe_error", outcomes)
            self.assertIs(outcomes.get("observed_store"), selected)
            book._reload()

    def test_reinitialization_cannot_retarget_reservation_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = self._book(selected)

            with self.assertRaisesRegex(
                ReservationConflict,
                "already established",
            ):
                reservation_authority._initialize_reservation_store_binding(
                    book,
                    replacement,
                    environment="PAPER",
                    account_id="acct-reservation-authority",
                )

            self.assertIs(vars(book)["store"], selected)
            book._reload()

    def test_caller_cannot_retarget_reservation_history_after_construction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = JournalStore(root / "selected.sqlite3")
            replacement = JournalStore(root / "replacement.sqlite3")
            book = self._book(selected)

            # Reservation authority must remain bound to the originally selected
            # physical JournalStore generation. Caller-writable instance state
            # cannot redefine which durable history owns reserved capital.
            vars(book)["store"] = replacement

            with self.assertRaises(ReservationConflict):
                book._reload()


    def test_hostile_environment_text_subclass_fails_before_callback_or_journal_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            with self.assertRaisesRegex(
                ValueError,
                "environment must be REPLAY, SIMULATION, PAPER, or LIVE",
            ):
                DurableReservationBook(
                    store,
                    environment=_HostileText(" paper "),
                    account_id="acct-reservation-authority",
                )

            self.assertEqual(store.current_journal_sequence(), 0)

    def test_hostile_account_text_subclass_fails_before_callback_or_journal_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            with self.assertRaisesRegex(ValueError, "account_id is required"):
                DurableReservationBook(
                    store,
                    environment="PAPER",
                    account_id=_HostileText(" acct-reservation-authority "),
                )

            self.assertEqual(store.current_journal_sequence(), 0)

    def test_executable_resource_mapping_is_rejected_before_callbacks_or_journal_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            requirements = _HostileMapping()
            dict.__setitem__(requirements, "CASH:USD", "10")

            with self.assertRaisesRegex(
                TypeError,
                "resource amounts must use an exact dict",
            ):
                book.reserve(
                    command_id="cmd-hostile-map",
                    idempotency_key="idem-hostile-map",
                    reservation_id="reservation-hostile-map",
                    intent_id="intent-hostile-map",
                    requirements=requirements,
                    available={"CASH:USD": "100"},
                )

            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertEqual(book.active(), ())

    def test_mapping_proxy_over_executable_mapping_is_rejected_before_callbacks(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            book = self._book(store)
            hostile = _HostileMapping()
            dict.__setitem__(hostile, "CASH:USD", "10")
            proxied = MappingProxyType(hostile)

            with self.assertRaisesRegex(
                TypeError,
                "resource amounts must use an exact dict",
            ):
                book.reserve(
                    command_id="cmd-hostile-proxy",
                    idempotency_key="idem-hostile-proxy",
                    reservation_id="reservation-hostile-proxy",
                    intent_id="intent-hostile-proxy",
                    requirements=proxied,
                    available={"CASH:USD": "100"},
                )

            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertEqual(book.active(), ())

    def test_snapshot_digest_rejects_subclass_before_snapshot_semantics(self):
        class SnapshotSubclass(reservation_authority.ReservationSnapshot):
            pass

        snapshot = SnapshotSubclass(
            reservation_id="reservation-subclass",
            intent_id="intent-subclass",
            original={},
            remaining={},
            consumed={},
            state="WORKING",
        )
        with self.assertRaisesRegex(
            TypeError,
            "snapshot must be exact ReservationSnapshot",
        ):
            reservation_authority.reservation_snapshot_digest(snapshot)



if __name__ == "__main__":
    unittest.main()
