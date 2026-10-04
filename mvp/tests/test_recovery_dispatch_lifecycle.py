from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.dispatch import DispatchOutcome
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import (
    RecoveryBoundDispatcher,
    RecoveryDispatchBindingError,
)


class RecoveryDispatchLifecycleTests(unittest.TestCase):
    @staticmethod
    def _bound_pair(root: Path):
        store = JournalStore(root / "journal.sqlite3")
        controller = RecoveryController(
            owner_store=store,
            owner_scope="PAPER:acct",
        )
        controller.start("owner-a")
        return store, controller

    @staticmethod
    def _dispatch(dispatcher: RecoveryBoundDispatcher, attempt_id: str):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=f"intent-{attempt_id}",
            intent_hash=f"hash-{attempt_id}",
            provider="SIMULATED",
            request={},
            now="2026-10-04T04:50:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=Mock(),
        )

    def test_shared_lifecycle_is_not_written_into_controller_state(self):
        with TemporaryDirectory() as directory:
            store, controller = self._bound_pair(Path(directory))
            state_before = dict(vars(controller))

            first = RecoveryBoundDispatcher(
                controller,
                store,
                environment="PAPER",
                account_id="acct",
            )
            second = RecoveryBoundDispatcher(
                controller,
                store,
                environment="PAPER",
                account_id="acct",
            )

            self.assertEqual(vars(controller), state_before)
            self.assertNotIn("_recovery_dispatch_lifecycle", vars(controller))
            self.assertIs(
                object.__getattribute__(first, "_lifecycle"),
                object.__getattribute__(second, "_lifecycle"),
            )

            first.stop_and_drain()
            self.assertIsNone(controller.owner)
            self.assertEqual(controller.state, HostState.STOPPED)
            with self.assertRaisesRegex(
                RecoveryDispatchBindingError,
                "permanently revoked",
            ):
                self._dispatch(second, "after-stop")

    def test_dispatch_arriving_during_revoke_never_enters_inner_dispatcher(self):
        with TemporaryDirectory() as directory:
            store, controller = self._bound_pair(Path(directory))
            active = RecoveryBoundDispatcher(
                controller,
                store,
                environment="PAPER",
                account_id="acct",
            )
            late = RecoveryBoundDispatcher(
                controller,
                store,
                environment="PAPER",
                account_id="acct",
            )
            lifecycle = object.__getattribute__(active, "_lifecycle")
            self.assertIs(lifecycle, object.__getattribute__(late, "_lifecycle"))

            active_inner = object.__getattribute__(active, "_dispatcher")
            late_inner = object.__getattribute__(late, "_dispatcher")
            active_entered = Event()
            active_release = Event()
            stop_finished = Event()
            late_finished = Event()
            errors: list[BaseException] = []
            late_errors: list[BaseException] = []

            def hold_active_dispatch(**kwargs):
                del kwargs
                active_entered.set()
                if not active_release.wait(5):
                    raise AssertionError("active dispatch was not released")
                return DispatchOutcome(
                    "SENT",
                    "client-order",
                    {"status": "accepted"},
                    "sent_confirmed",
                )

            def run_active():
                try:
                    self._dispatch(active, "active")
                except BaseException as error:
                    errors.append(error)

            def run_stop():
                try:
                    late.stop_and_drain()
                except BaseException as error:
                    errors.append(error)
                finally:
                    stop_finished.set()

            def run_late():
                try:
                    self._dispatch(late, "late")
                except BaseException as error:
                    late_errors.append(error)
                finally:
                    late_finished.set()

            with (
                patch.object(active_inner, "dispatch", side_effect=hold_active_dispatch),
                patch.object(late_inner, "dispatch") as late_inner_dispatch,
            ):
                active_thread = Thread(target=run_active, name="active-dispatch")
                active_thread.start()
                self.assertTrue(active_entered.wait(5))

                stop_thread = Thread(target=run_stop, name="dispatcher-stop")
                stop_thread.start()
                with lifecycle.condition:
                    self.assertTrue(
                        lifecycle.condition.wait_for(
                            lambda: lifecycle.revoking,
                            timeout=5,
                        )
                    )

                late_thread = Thread(target=run_late, name="late-dispatch")
                late_thread.start()
                self.assertFalse(late_finished.is_set())
                late_inner_dispatch.assert_not_called()
                self.assertFalse(stop_finished.is_set())

                active_release.set()
                active_thread.join(5)
                stop_thread.join(5)
                late_thread.join(5)

                self.assertFalse(active_thread.is_alive())
                self.assertFalse(stop_thread.is_alive())
                self.assertFalse(late_thread.is_alive())
                late_inner_dispatch.assert_not_called()

            self.assertEqual(errors, [])
            self.assertEqual(len(late_errors), 1)
            self.assertIsInstance(late_errors[0], RecoveryDispatchBindingError)
            self.assertIn("permanently revoked", str(late_errors[0]))
            self.assertIsNone(controller.owner)
            self.assertEqual(controller.state, HostState.STOPPED)


if __name__ == "__main__":
    unittest.main()
