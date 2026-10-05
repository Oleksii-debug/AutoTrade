from __future__ import annotations

from datetime import timedelta
from tempfile import TemporaryDirectory
import inspect
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.financial_binding_dispatch import (
    FinancialBindingDispatchError,
    dispatch_recovery_bound_durable_financial_request,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.tests.test_confirmed_pending_admission import NOW
from mvp.tests.test_financial_binding_dispatch import FinancialBindingDispatchTests


class FinancialBindingRecoveryDispatchTests(unittest.TestCase):
    @staticmethod
    def _allow(_intent_hash, _now):
        return True, "allowed"

    @staticmethod
    def _bound_case(store: JournalStore):
        fixture = FinancialBindingDispatchTests(methodName="runTest")
        return fixture._bound_case(store)

    @staticmethod
    def _ready_recovery(store: JournalStore, *, account_id: str):
        controller = RecoveryController(
            owner_store=store,
            owner_scope=f"SIMULATION:{account_id}",
        )
        owner = controller.start("owner-1")
        # This successor proves mechanical recovery/sender composition only.
        # The current ancestry has no production C/Q/account authority; tests do
        # not counterfeit one.  SIMULATION marks the controller READY locally so
        # the final barrier can exercise its durable-owner validation contract.
        controller.provider_reconciled = True
        controller.reason_codes.discard("startup_reconciliation_required")
        controller._recompute_state()
        if controller.state is not HostState.READY:
            raise AssertionError("simulation recovery fixture did not become READY")
        return controller, owner

    @staticmethod
    def _now(offset: int = 3) -> str:
        return (NOW + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")

    def test_exact_recovery_owner_crosses_existing_final_barrier(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            controller, owner = self._ready_recovery(
                store,
                account_id=material.account_id,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "sim-recovery-order-1"}

            result = dispatch_recovery_bound_durable_financial_request(
                dispatcher,
                recovery_controller=controller,
                admission_id=admitted.admission_id,
                attempt_id="financial-recovery-positive",
                request=request,
                now=self._now(),
                authority_check=self._allow,
                transport_send=transport,
            )

            self.assertEqual(result.status, "SENT")
            self.assertEqual(outbound, 1)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("financial-recovery-positive"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            self.assertEqual(events[1]["payload"]["owner_token"], owner.owner_id)
            self.assertEqual(events[1]["payload"]["owner_epoch"], owner.epoch)

    def test_owner_advance_between_prepared_and_final_guard_is_zero_wire(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            controller, owner = self._ready_recovery(
                store,
                account_id=material.account_id,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                successor = controller.transfer_owner(
                    new_owner_id="owner-2",
                    old_sender_fenced=True,
                    reconciled=True,
                )
                self.assertEqual(successor.epoch, owner.epoch + 1)
                final_guard()
                outbound += 1
                return {"must": "not happen"}

            result = dispatch_recovery_bound_durable_financial_request(
                dispatcher,
                recovery_controller=controller,
                admission_id=admitted.admission_id,
                attempt_id="financial-recovery-stale-owner",
                request=request,
                now=self._now(),
                authority_check=self._allow,
                transport_send=transport,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("financial-recovery-stale-owner"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertTrue(
                events[-1]["payload"]["reason"].startswith(
                    "sender_fence_rejected:PermissionError"
                )
            )

    def test_instance_shadow_cannot_replace_canonical_sender_validator(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            controller, owner = self._ready_recovery(
                store,
                account_id=material.account_id,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )
            hostile_calls = 0
            outbound = 0

            def hostile_validate_sender(_owner_id, _owner_epoch):
                nonlocal hostile_calls
                hostile_calls += 1

            controller.validate_sender = hostile_validate_sender
            controller.state = HostState.RECOVERING

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"must": "not happen"}

            result = dispatch_recovery_bound_durable_financial_request(
                dispatcher,
                recovery_controller=controller,
                admission_id=admitted.admission_id,
                attempt_id="financial-recovery-shadow",
                request=request,
                now=self._now(),
                authority_check=self._allow,
                transport_send=transport,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(hostile_calls, 0)
            self.assertEqual(outbound, 0)

    def test_recovery_store_and_scope_must_match_dispatcher_before_prepared(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            other_store = JournalStore(f"{directory}/other.sqlite3")
            admitted, material, request = self._bound_case(store)
            foreign, foreign_owner = self._ready_recovery(
                other_store,
                account_id=material.account_id,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token=foreign_owner.owner_id,
                owner_epoch=foreign_owner.epoch,
            )

            with self.assertRaisesRegex(
                FinancialBindingDispatchError,
                "same exact JournalStore",
            ):
                dispatch_recovery_bound_durable_financial_request(
                    dispatcher,
                    recovery_controller=foreign,
                    admission_id=admitted.admission_id,
                    attempt_id="financial-recovery-foreign-store",
                    request=request,
                    now=self._now(),
                    authority_check=self._allow,
                    transport_send=lambda *_args: self.fail("transport reached"),
                )
            self.assertEqual(
                store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id("financial-recovery-foreign-store"),
                ),
                [],
            )

            scoped = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:another-account",
            )
            scoped_owner = scoped.start("owner-1")
            scoped.provider_reconciled = True
            scoped.reason_codes.discard("startup_reconciliation_required")
            scoped._recompute_state()
            scoped_dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token=scoped_owner.owner_id,
                owner_epoch=scoped_owner.epoch,
            )
            with self.assertRaisesRegex(
                FinancialBindingDispatchError,
                "scope differs",
            ):
                dispatch_recovery_bound_durable_financial_request(
                    scoped_dispatcher,
                    recovery_controller=scoped,
                    admission_id=admitted.admission_id,
                    attempt_id="financial-recovery-foreign-scope",
                    request=request,
                    now=self._now(4),
                    authority_check=self._allow,
                    transport_send=lambda *_args: self.fail("transport reached"),
                )

    def test_dispatcher_owner_must_be_current_recovery_owner_before_prepared(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            controller, owner = self._ready_recovery(
                store,
                account_id=material.account_id,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch + 1,
            )

            with self.assertRaisesRegex(
                FinancialBindingDispatchError,
                "owner differs",
            ):
                dispatch_recovery_bound_durable_financial_request(
                    dispatcher,
                    recovery_controller=controller,
                    admission_id=admitted.admission_id,
                    attempt_id="financial-recovery-owner-mismatch",
                    request=request,
                    now=self._now(),
                    authority_check=self._allow,
                    transport_send=lambda *_args: self.fail("transport reached"),
                )
            self.assertEqual(
                store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id("financial-recovery-owner-mismatch"),
                ),
                [],
            )

    def test_public_surface_does_not_accept_sender_or_scope_overrides(self) -> None:
        parameters = inspect.signature(
            dispatch_recovery_bound_durable_financial_request
        ).parameters
        for forbidden in (
            "sender_check",
            "owner_id",
            "owner_token",
            "owner_epoch",
            "provider",
            "account_id",
            "environment",
            "intent_id",
            "client_order_id",
            "submission_scope",
        ):
            self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
