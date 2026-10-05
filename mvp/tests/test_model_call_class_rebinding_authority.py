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


if __name__ == "__main__":
    unittest.main()
