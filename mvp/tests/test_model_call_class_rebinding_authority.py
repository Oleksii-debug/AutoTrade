from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_call import (
    DurableModelCallOrchestrator,
    ModelCallError,
    ModelCallSpec,
    PricingEvidenceSnapshot,
    PricingQuote,
)
from mvp.autotrade_mvp.model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RoutingMode,
    RoutingPolicy,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_model_call import (
    _billing_evidence,
    _observation_evidence,
    observation,
)


NOW = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-09-25T10:00:00Z"


def _open_budget(directory):
    journal = JournalStore(Path(directory) / "journal.db")
    budget = DurableModelBudget(
        journal=journal,
        budget_id="class-rebinding-budget",
        ceiling="5",
        environment="PAPER",
        clock=lambda: NOW_TEXT,
    )
    return journal, budget


def _spec():
    return ModelCallSpec(
        job_id="class-rebinding-job",
        input_digest="sha256:" + "1" * 64,
        policy_id="class-rebinding-policy",
        pricing_evidence_id="class-rebinding-pricing",
        pricing_as_of=NOW_TEXT,
        result_schema_id="class-rebinding-schema",
    )


def _descriptor():
    return ModelDescriptor(
        model_id="model-a",
        provider_id="provider-a",
        revision="r1",
        remote=True,
        estimated_cost=Decimal("1.2"),
        latency_ms=100,
        quality_score=Decimal("0.8"),
    )


def _policy():
    return RoutingPolicy(
        mode=RoutingMode.FIXED,
        allowed_model_ids=("model-a",),
        fixed_model_id="model-a",
        allow_remote=True,
        maximum_cost=Decimal("2"),
        maximum_latency_ms=1000,
    )


def _pricing(call_spec, descriptors):
    return PricingEvidenceSnapshot(
        evidence_id=call_spec.pricing_evidence_id,
        evidence_digest="sha256:" + "2" * 64,
        as_of=call_spec.pricing_as_of,
        valid_until="2026-09-25T11:00:00Z",
        cost_currency=call_spec.cost_currency,
        quotes=tuple(
            PricingQuote(
                provider_id=item.provider_id,
                model_id=item.model_id,
                revision=item.revision,
                estimated_cost=item.estimated_cost,
            )
            for item in descriptors
        ),
    )


def _orchestrator(budget):
    return DurableModelCallOrchestrator(
        budget=budget,
        clock=lambda: NOW_TEXT,
        pricing_evidence_resolver=_pricing,
        observation_evidence_resolver=lambda *_args: None,
        billing_evidence_resolver=lambda *_args: None,
    )


def _request(orchestrator, call_spec):
    return ModelRequest(
        request_id=orchestrator.attempt_id(call_spec),
        allowed_model_ids=("model-a",),
        privacy_remote_allowed=True,
        budget_remaining=Decimal("2"),
        deadline_utc=NOW + timedelta(hours=1),
        cancelled=False,
    )


class ModelCallClassRebindingAuthorityTests(unittest.TestCase):
    def test_cancel_restores_orchestrator_class_before_dynamic_dispatch(self):
        class HostileOrchestrator(DurableModelCallOrchestrator):
            get_calls = 0
            set_calls = 0

            def __getattribute__(self, name):
                type(self).get_calls += 1
                raise AssertionError(
                    "hostile orchestrator __getattribute__ executed during restore: "
                    + name
                )

            def __setattr__(self, name, value):
                type(self).set_calls += 1
                raise AssertionError(
                    "hostile orchestrator __setattr__ executed during restore: "
                    + name
                )

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            calls = []

            def hostile_cancel():
                object.__setattr__(
                    orchestrator,
                    "__class__",
                    HostileOrchestrator,
                )
                return False

            with self.assertRaisesRegex(
                ModelCallError,
                r"cancellation probe mutated orchestrator authority:.*orchestrator\.__class__",
            ):
                orchestrator.execute(
                    spec=call_spec,
                    policy=_policy(),
                    request=_request(orchestrator, call_spec),
                    descriptors=[_descriptor()],
                    call=lambda *_args: calls.append("inference"),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                    cancel_requested=hostile_cancel,
                )

            self.assertIs(type(orchestrator), DurableModelCallOrchestrator)
            self.assertEqual(HostileOrchestrator.get_calls, 0)
            self.assertEqual(HostileOrchestrator.set_calls, 0)
            self.assertEqual(calls, [])
            self.assertEqual(budget.active_reservation(attempt_id), Decimal("1.2"))
            self.assertEqual(
                [event["event_type"] for event in orchestrator._events(attempt_id)],
                ["ModelCallPrepared"],
            )

    def test_cancel_restores_budget_class_before_dynamic_dispatch(self):
        class HostileBudget(DurableModelBudget):
            get_calls = 0
            set_calls = 0

            def __getattribute__(self, name):
                type(self).get_calls += 1
                raise AssertionError(
                    "hostile budget __getattribute__ executed during restore: " + name
                )

            def __setattr__(self, name, value):
                type(self).set_calls += 1
                raise AssertionError(
                    "hostile budget __setattr__ executed during restore: " + name
                )

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            calls = []

            def hostile_cancel():
                object.__setattr__(budget, "__class__", HostileBudget)
                return False

            with self.assertRaisesRegex(
                ModelCallError,
                r"cancellation probe mutated orchestrator authority:.*budget\.__class__",
            ):
                orchestrator.execute(
                    spec=call_spec,
                    policy=_policy(),
                    request=_request(orchestrator, call_spec),
                    descriptors=[_descriptor()],
                    call=lambda *_args: calls.append("inference"),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                    cancel_requested=hostile_cancel,
                )

            self.assertIs(type(budget), DurableModelBudget)
            self.assertEqual(HostileBudget.get_calls, 0)
            self.assertEqual(HostileBudget.set_calls, 0)
            self.assertEqual(calls, [])
            self.assertEqual(budget.active_reservation(attempt_id), Decimal("1.2"))
            self.assertEqual(
                [event["event_type"] for event in orchestrator._events(attempt_id)],
                ["ModelCallPrepared"],
            )


    @staticmethod
    def _new_hostile_orchestrator_class():
        class HostileOrchestrator(DurableModelCallOrchestrator):
            __slots__ = ()
            get_calls = 0
            set_calls = 0

            def __getattribute__(self, name):
                HostileOrchestrator.get_calls += 1
                raise AssertionError(
                    "hostile orchestrator __getattribute__ executed during restore: "
                    + name
                )

            def __setattr__(self, name, value):
                HostileOrchestrator.set_calls += 1
                raise AssertionError(
                    "hostile orchestrator __setattr__ executed during restore: "
                    + name
                )

        return HostileOrchestrator

    def _assert_orchestrator_class_restored(
        self,
        orchestrator,
        hostile_class,
    ):
        self.assertIs(type(orchestrator), DurableModelCallOrchestrator)
        self.assertEqual(hostile_class.get_calls, 0)
        self.assertEqual(hostile_class.set_calls, 0)

    def test_descriptor_restores_orchestrator_class_before_reservation(self):
        HostileOrchestrator = self._new_hostile_orchestrator_class()
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()

            class HostileInventory:
                def __iter__(self):
                    object.__setattr__(
                        orchestrator,
                        "__class__",
                        HostileOrchestrator,
                    )
                    yield _descriptor()

            with self.assertRaisesRegex(
                ModelCallError,
                r"descriptor inventory mutated orchestrator authority:.*orchestrator\.__class__",
            ):
                orchestrator.execute(
                    spec=call_spec,
                    policy=_policy(),
                    request=_request(orchestrator, call_spec),
                    descriptors=HostileInventory(),
                    call=lambda *_args: self.fail(
                        "descriptor class rebinding crossed the inference boundary"
                    ),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )

            self._assert_orchestrator_class_restored(
                orchestrator,
                HostileOrchestrator,
            )
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))
            self.assertEqual(
                orchestrator._events(orchestrator.attempt_id(call_spec)),
                [],
            )

    def test_pricing_restores_orchestrator_class_before_reservation(self):
        HostileOrchestrator = self._new_hostile_orchestrator_class()
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = None

            def hostile_pricing(resolver_spec, descriptors):
                evidence = _pricing(resolver_spec, descriptors)
                object.__setattr__(
                    orchestrator,
                    "__class__",
                    HostileOrchestrator,
                )
                return evidence

            orchestrator = DurableModelCallOrchestrator(
                budget=budget,
                clock=lambda: NOW_TEXT,
                pricing_evidence_resolver=hostile_pricing,
                observation_evidence_resolver=lambda *_args: None,
                billing_evidence_resolver=lambda *_args: None,
            )
            call_spec = _spec()

            with self.assertRaisesRegex(
                ModelCallError,
                r"pricing evidence resolver mutated orchestrator authority:.*orchestrator\.__class__",
            ):
                orchestrator.execute(
                    spec=call_spec,
                    policy=_policy(),
                    request=_request(orchestrator, call_spec),
                    descriptors=[_descriptor()],
                    call=lambda *_args: self.fail(
                        "pricing class rebinding crossed the inference boundary"
                    ),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )

            self._assert_orchestrator_class_restored(
                orchestrator,
                HostileOrchestrator,
            )
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))
            self.assertEqual(
                orchestrator._events(orchestrator.attempt_id(call_spec)),
                [],
            )

    def test_observation_restores_orchestrator_class_before_unknown_settlement(self):
        HostileOrchestrator = self._new_hostile_orchestrator_class()
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = None

            def hostile_observation(value, binding):
                evidence = _observation_evidence(value, binding)
                object.__setattr__(
                    orchestrator,
                    "__class__",
                    HostileOrchestrator,
                )
                return evidence

            orchestrator = DurableModelCallOrchestrator(
                budget=budget,
                clock=lambda: NOW_TEXT,
                pricing_evidence_resolver=_pricing,
                observation_evidence_resolver=hostile_observation,
                billing_evidence_resolver=lambda *_args: None,
            )
            call_spec = _spec()
            result = orchestrator.execute(
                spec=call_spec,
                policy=_policy(),
                request=_request(orchestrator, call_spec),
                descriptors=[_descriptor()],
                call=lambda *_args: observation(),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )

            self.assertEqual(result.status, "UNKNOWN")
            self.assertIn("orchestrator.__class__", result.reason)
            self._assert_orchestrator_class_restored(
                orchestrator,
                HostileOrchestrator,
            )
            self.assertEqual(budget.snapshot().incurred, Decimal("0"))
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("1.2"),
            )

    def test_validator_restores_orchestrator_class_before_settlement(self):
        HostileOrchestrator = self._new_hostile_orchestrator_class()
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = DurableModelCallOrchestrator(
                budget=budget,
                clock=lambda: NOW_TEXT,
                pricing_evidence_resolver=_pricing,
                observation_evidence_resolver=_observation_evidence,
                billing_evidence_resolver=lambda *_args: None,
            )
            call_spec = _spec()

            def hostile_validator(_value):
                object.__setattr__(
                    orchestrator,
                    "__class__",
                    HostileOrchestrator,
                )
                return True

            result = orchestrator.execute(
                spec=call_spec,
                policy=_policy(),
                request=_request(orchestrator, call_spec),
                descriptors=[_descriptor()],
                call=lambda *_args: observation(),
                validate_result=hostile_validator,
                now_utc=NOW,
            )

            self.assertEqual(result.status, "OBSERVED_INVALID")
            self.assertFalse(result.schema_valid)
            self.assertIsNone(result.output)
            self._assert_orchestrator_class_restored(
                orchestrator,
                HostileOrchestrator,
            )
            self.assertEqual(budget.snapshot().incurred, Decimal("0.4"))
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("0.2"),
            )

    def test_recovery_restores_orchestrator_class_and_preserves_reservation(self):
        HostileOrchestrator = self._new_hostile_orchestrator_class()
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            budget.admit_route(
                _policy(),
                _request(orchestrator, call_spec),
                [_descriptor()],
                now_utc=NOW,
                reservation_context=orchestrator._reservation_context(
                    call_spec,
                    _pricing(call_spec, (_descriptor(),)),
                ),
            )

            def hostile_fence():
                object.__setattr__(
                    orchestrator,
                    "__class__",
                    HostileOrchestrator,
                )

            with self.assertRaisesRegex(
                ModelCallError,
                r"recovery fence mutated orchestrator authority:.*orchestrator\.__class__",
            ):
                orchestrator.recover_reserved_not_started(
                    spec=call_spec,
                    recovery_fence=hostile_fence,
                )

            self._assert_orchestrator_class_restored(
                orchestrator,
                HostileOrchestrator,
            )
            self.assertEqual(
                budget.active_reservation(attempt_id),
                Decimal("1.2"),
            )
            self.assertEqual(orchestrator._events(attempt_id), [])

    def test_billing_restores_orchestrator_class_before_reconciliation(self):
        HostileOrchestrator = self._new_hostile_orchestrator_class()
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = None

            def hostile_billing(attempt_id, billing_id, scope):
                evidence = _billing_evidence(
                    attempt_id,
                    billing_id,
                    scope,
                )
                object.__setattr__(
                    orchestrator,
                    "__class__",
                    HostileOrchestrator,
                )
                return evidence

            orchestrator = DurableModelCallOrchestrator(
                budget=budget,
                clock=lambda: NOW_TEXT,
                pricing_evidence_resolver=_pricing,
                observation_evidence_resolver=_observation_evidence,
                billing_evidence_resolver=hostile_billing,
            )
            call_spec = _spec()
            result = orchestrator.execute(
                spec=call_spec,
                policy=_policy(),
                request=_request(orchestrator, call_spec),
                descriptors=[_descriptor()],
                call=lambda *_args: observation(
                    billing_id="class-rebinding-billing",
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(result.status, "OBSERVED_VALID")

            with self.assertRaisesRegex(
                ModelCallError,
                r"billing evidence resolver mutated orchestrator authority:.*orchestrator\.__class__",
            ):
                orchestrator.reconcile_billing(
                    attempt_id=result.attempt_id,
                    billing_id="class-rebinding-billing",
                    expected_billed="0.25",
                )

            self._assert_orchestrator_class_restored(
                orchestrator,
                HostileOrchestrator,
            )
            self.assertEqual(budget.snapshot().incurred, Decimal("0.4"))
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("0.2"),
            )
            self.assertNotIn(
                "ModelBillingEvidenceObserved",
                [
                    event["event_type"]
                    for event in orchestrator._events(result.attempt_id)
                ],
            )


if __name__ == "__main__":
    unittest.main()
