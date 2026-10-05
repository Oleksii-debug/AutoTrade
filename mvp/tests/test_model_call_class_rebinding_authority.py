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


    def test_adapter_restores_rebound_restore_and_budget_dispatch(self):
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            restore_descriptor = vars(DurableModelCallOrchestrator)[
                "_restore_callback_shape"
            ]
            settle_descriptor = vars(DurableModelBudget)["settle"]
            try:
                def hostile_adapter(*_args):
                    DurableModelCallOrchestrator._restore_callback_shape = (
                        lambda *_args, **_kwargs: self.fail(
                            "rebound restore intercepted callback recovery"
                        )
                    )
                    DurableModelBudget.settle = (
                        lambda *_args, **_kwargs: self.fail(
                            "rebound settle reached durable UNKNOWN accounting"
                        )
                    )
                    return None

                result = orchestrator.execute(
                    spec=call_spec,
                    policy=_policy(),
                    request=_request(orchestrator, call_spec),
                    descriptors=[_descriptor()],
                    call=hostile_adapter,
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )

                self.assertEqual(result.status, "UNKNOWN")
                self.assertIn(
                    "DurableModelCallOrchestrator._restore_callback_shape",
                    result.reason,
                )
                self.assertIn("DurableModelBudget.settle", result.reason)
                self.assertIs(
                    vars(DurableModelCallOrchestrator)["_restore_callback_shape"],
                    restore_descriptor,
                )
                self.assertIs(vars(DurableModelBudget)["settle"], settle_descriptor)
                self.assertEqual(budget.snapshot().incurred, Decimal("0"))
                self.assertEqual(
                    budget.snapshot().estimated_unbilled,
                    Decimal("1.2"),
                )
            finally:
                type.__setattr__(
                    DurableModelCallOrchestrator,
                    "_restore_callback_shape",
                    restore_descriptor,
                )
                type.__setattr__(DurableModelBudget, "settle", settle_descriptor)

    def test_cancel_restores_rebound_budget_release_dispatch(self):
        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            release_descriptor = vars(DurableModelBudget)["release"]
            calls = []
            try:
                def hostile_cancel():
                    DurableModelBudget.release = (
                        lambda *_args, **_kwargs: self.fail(
                            "rebound release reached cancellation recovery"
                        )
                    )
                    return False

                with self.assertRaises(ModelCallError) as caught:
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

                self.assertIn("DurableModelBudget.release", str(caught.exception))
                self.assertEqual(calls, [])
                self.assertIs(
                    vars(DurableModelBudget)["release"],
                    release_descriptor,
                )
                self.assertEqual(
                    budget.active_reservation(attempt_id),
                    Decimal("1.2"),
                )
                self.assertEqual(
                    [event["event_type"] for event in orchestrator._events(attempt_id)],
                    ["ModelCallPrepared"],
                )
            finally:
                type.__setattr__(DurableModelBudget, "release", release_descriptor)

    def test_budget_clock_restores_rebound_recovery_and_journal_dispatch(self):
        armed = False
        restore_descriptor = vars(DurableModelBudget)["_restore_clock_authority"]
        canonical_load_events = JournalStore.load_events

        def hostile_clock():
            nonlocal armed
            if armed:
                DurableModelBudget._restore_clock_authority = (
                    lambda *_args, **_kwargs: self.fail(
                        "rebound budget-clock restore intercepted recovery"
                    )
                )
                JournalStore.load_events = (
                    lambda *_args, **_kwargs: self.fail(
                        "rebound journal load reached durable budget authority"
                    )
                )
            return NOW_TEXT

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="clock-class-rebinding-budget",
                ceiling="5",
                environment="PAPER",
                clock=hostile_clock,
            )
            armed = True
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ) as caught:
                    budget.reserve("clock-rebind-request", "0.2")

                self.assertIn(
                    "DurableModelBudget._restore_clock_authority",
                    str(caught.exception),
                )
                self.assertIn("JournalStore.load_events", str(caught.exception))
                self.assertIs(
                    vars(DurableModelBudget)["_restore_clock_authority"],
                    restore_descriptor,
                )
                self.assertIs(JournalStore.load_events, canonical_load_events)
                self.assertNotIn("load_events", JournalStore.__dict__)
                self.assertEqual(budget.snapshot().reserved, Decimal("0"))
            finally:
                type.__setattr__(
                    DurableModelBudget,
                    "_restore_clock_authority",
                    restore_descriptor,
                )
                if "load_events" in JournalStore.__dict__:
                    type.__delattr__(JournalStore, "load_events")


if __name__ == "__main__":
    unittest.main()
