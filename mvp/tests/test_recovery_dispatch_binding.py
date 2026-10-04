from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.recovery_dispatch import (
    RecoveryBoundDispatcher,
    RecoveryDispatchBindingError,
)


class RecoveryDispatchBindingTests(unittest.TestCase):
    def _started(
        self,
        root: Path,
        *,
        scope: str = "PAPER:acct",
    ) -> tuple[JournalStore, RecoveryController]:
        store = JournalStore(root / "journal.sqlite3")
        controller = RecoveryController(owner_store=store, owner_scope=scope)
        controller.start("host-a")
        return store, controller

    def test_binding_requires_exact_store_object_and_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store, controller = self._started(root)
            same_path_other_object = JournalStore(store.path)

            with self.assertRaisesRegex(
                RecoveryDispatchBindingError,
                "exact JournalStore object",
            ):
                RecoveryBoundDispatcher(
                    controller,
                    same_path_other_object,
                    environment="PAPER",
                    account_id="acct",
                )

            with self.assertRaisesRegex(
                RecoveryDispatchBindingError,
                "owner scope does not match",
            ):
                RecoveryBoundDispatcher(
                    controller,
                    store,
                    environment="LIVE",
                    account_id="acct",
                )

    def test_public_dispatch_has_no_sender_check_substitution_parameter(self):
        with TemporaryDirectory() as directory:
            store, controller = self._started(Path(directory))
            dispatcher = RecoveryBoundDispatcher(
                controller,
                store,
                environment="PAPER",
                account_id="acct",
            )

            with self.assertRaisesRegex(TypeError, "sender_check"):
                dispatcher.dispatch(
                    attempt_id="no-substitution",
                    intent_id="intent-1",
                    intent_hash="hash-1",
                    provider="SIMULATED",
                    request={},
                    now="2026-10-04T00:40:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=lambda _cid, _request, _guard: {"ok": True},
                    sender_check=lambda _owner, _epoch: None,  # type: ignore[call-arg]
                )

            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"),
                [],
            )

    def test_canonical_bound_validator_reaches_wire_with_captured_owner(self):
        with TemporaryDirectory() as directory:
            store, controller = self._started(Path(directory))
            dispatcher = RecoveryBoundDispatcher(
                controller,
                store,
                environment="PAPER",
                account_id="acct",
            )
            wire_calls: list[str] = []

            def transport(client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append(client_order_id)
                return {"status": "accepted"}

            with patch.object(
                RecoveryController,
                "validate_sender",
                autospec=True,
            ) as canonical_validator:
                outcome = dispatcher.dispatch(
                    attempt_id="bound-positive",
                    intent_id="intent-positive",
                    intent_hash="hash-positive",
                    provider="SIMULATED",
                    request={},
                    now="2026-10-04T00:40:30Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(len(wire_calls), 1)
            canonical_validator.assert_called_once_with(controller, "host-a", 1)

    def test_instance_shadow_cannot_replace_canonical_recovery_validator(self):
        with TemporaryDirectory() as directory:
            store, controller = self._started(Path(directory))
            dispatcher = RecoveryBoundDispatcher(
                controller,
                store,
                environment="PAPER",
                account_id="acct",
            )
            # The controller is deliberately still RECOVERING.  A permissive
            # instance attribute would bypass readiness if the bound dispatcher
            # used ordinary dynamic method lookup.
            controller.validate_sender = lambda _owner, _epoch: None  # type: ignore[method-assign]
            wire_calls: list[str] = []

            def transport(client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append(client_order_id)
                return {"status": "accepted"}

            outcome = dispatcher.dispatch(
                attempt_id="shadowed-validator",
                intent_id="intent-2",
                intent_hash="hash-2",
                provider="SIMULATED",
                request={},
                now="2026-10-04T00:41:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "sender_fence_rejected:PermissionError",
            )
            self.assertEqual(wire_calls, [])
            events = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )


if __name__ == "__main__":
    unittest.main()
