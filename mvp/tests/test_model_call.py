from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_DOWN, Inexact, Rounded
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_call import (
    DurableModelCallOrchestrator,
    ModelCallError,
    ModelCallNotSent,
    BillingEvidence,
    ModelCallObservation,
    ModelCallSpec,
    ModelObservationEvidence,
    PricingEvidenceSnapshot,
    PricingQuote,
)
from mvp.autotrade_mvp.model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RoutingMode,
    RoutingPolicy,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


NOW = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-09-25T10:00:00Z"
INPUT_DIGEST = "sha256:" + "1" * 64


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value.isoformat().replace("+00:00", "Z")

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def open_budget(directory, *, ceiling="5", environment="PAPER"):
    journal = JournalStore(Path(directory) / "journal.db")
    budget = DurableModelBudget(
        journal=journal,
        budget_id="model-policy-budget",
        ceiling=ceiling,
        environment=environment,
        clock=lambda: NOW_TEXT,
    )
    return journal, budget


def _pricing_evidence(call_spec, descriptors):
    material = [
        {
            "provider_id": item.provider_id,
            "model_id": item.model_id,
            "revision": item.revision,
            "estimated_cost": str(item.estimated_cost),
        }
        for item in descriptors
    ]
    return PricingEvidenceSnapshot(
        evidence_id=call_spec.pricing_evidence_id,
        evidence_digest=payload_digest(
            {
                "pricing_evidence_id": call_spec.pricing_evidence_id,
                "as_of": call_spec.pricing_as_of,
                "cost_currency": call_spec.cost_currency,
                "quotes": material,
            }
        ),
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


def _observation_evidence(observed, binding):
    observation_digest = payload_digest(
        {
            "attempt_id": binding.attempt_id,
            "provider_id": observed.provider_id,
            "model_id": observed.model_id,
            "revision": observed.revision,
            "observed_at": observed.observed_at,
            "incurred_cost": str(observed.incurred_cost),
            "estimated_unbilled": str(observed.estimated_unbilled),
            "output_digest": payload_digest(observed.output),
            "provider_request_id": observed.provider_request_id,
            "provider_response_id": observed.provider_response_id,
            "usage_id": observed.usage_id,
            "billing_id": observed.billing_id,
        }
    )
    return ModelObservationEvidence(
        attempt_id=binding.attempt_id,
        evidence_id="usage-evidence:" + (observed.usage_id or "local"),
        evidence_digest=payload_digest(
            {
                "issuer": observed.provider_id,
                "observation_digest": observation_digest,
            }
        ),
        issuer=observed.provider_id,
        observation_digest=observation_digest,
    )


_TEST_BILLING_AMOUNTS = {
    "invoice-invalid-fallback": Decimal("0.2"),
    "invoice-retry-safe": Decimal("0.2"),
    "late-invoice-line": Decimal("0.4"),
    "invoice-line-7": Decimal("0.25"),
    "other-line": Decimal("0.25"),
    "invoice-stable": Decimal("0.2"),
    "bill-after-process-exit": Decimal("0.6"),
}


def _billing_evidence(attempt_id, billing_id, observed_payload):
    billed = _TEST_BILLING_AMOUNTS.get(billing_id)
    if billed is None:
        raise ValueError("test billing identity has no independent amount")
    return BillingEvidence(
        attempt_id=attempt_id,
        billing_id=billing_id,
        provider_id=observed_payload["provider_id"],
        model_id=observed_payload["model_id"],
        revision=observed_payload.get("revision"),
        billed=billed,
        cost_currency=observed_payload["cost_currency"],
        observed_at="2026-09-25T10:05:00Z",
        evidence_id="billing-evidence:" + billing_id,
        evidence_digest=payload_digest(
            {
                "attempt_id": attempt_id,
                "billing_id": billing_id,
                "billed": str(billed),
                "provider_id": observed_payload["provider_id"],
            }
        ),
        issuer=observed_payload["provider_id"],
    )

def orchestrator_for(
    *,
    budget,
    clock,
    pricing_evidence_resolver=_pricing_evidence,
    observation_evidence_resolver=_observation_evidence,
    billing_evidence_resolver=_billing_evidence,
    **kwargs,
):
    return DurableModelCallOrchestrator(
        budget=budget,
        clock=clock,
        pricing_evidence_resolver=pricing_evidence_resolver,
        observation_evidence_resolver=observation_evidence_resolver,
        billing_evidence_resolver=billing_evidence_resolver,
        **kwargs,
    )


def spec(
    *,
    pricing_evidence_id="pricing-v1",
    fallback_parent_attempt_id=None,
    fallback_index=0,
):
    return ModelCallSpec(
        job_id="research-job-1",
        input_digest=INPUT_DIGEST,
        policy_id="policy-v1",
        pricing_evidence_id=pricing_evidence_id,
        pricing_as_of=NOW_TEXT,
        result_schema_id="schema-v1",
        fallback_parent_attempt_id=fallback_parent_attempt_id,
        fallback_index=fallback_index,
    )


def descriptor(
    *,
    model_id="model-a",
    provider_id="provider-a",
    revision="r1",
    remote=True,
    cost="1.2",
):
    return ModelDescriptor(
        model_id=model_id,
        provider_id=provider_id,
        revision=revision,
        remote=remote,
        estimated_cost=Decimal(cost),
        latency_ms=100,
        quality_score=Decimal("0.8"),
    )


def fixed_policy(*, model_id="model-a", allow_remote=True, maximum_cost="2"):
    return RoutingPolicy(
        mode=RoutingMode.FIXED,
        allowed_model_ids=(model_id,),
        fixed_model_id=model_id,
        allow_remote=allow_remote,
        maximum_cost=Decimal(maximum_cost),
        maximum_latency_ms=1000,
    )


def local_only_policy(*model_ids):
    return RoutingPolicy(
        mode=RoutingMode.LOCAL_ONLY,
        allowed_model_ids=tuple(model_ids),
        allow_remote=False,
        maximum_cost=Decimal("2"),
        maximum_latency_ms=1000,
    )


def request_for(
    orchestrator,
    call_spec,
    *,
    allowed_model_ids=("model-a",),
    cancelled=False,
):
    return ModelRequest(
        request_id=orchestrator.attempt_id(call_spec),
        allowed_model_ids=tuple(allowed_model_ids),
        privacy_remote_allowed=True,
        budget_remaining=Decimal("2"),
        deadline_utc=NOW + timedelta(hours=1),
        cancelled=cancelled,
    )


def observation(
    *,
    provider_id="provider-a",
    model_id="model-a",
    revision="r1",
    incurred="0.4",
    unbilled="0.2",
    output=None,
    billing_id="bill-1",
):
    return ModelCallObservation(
        provider_id=provider_id,
        model_id=model_id,
        revision=revision,
        observed_at="2026-09-25T10:00:01Z",
        incurred_cost=Decimal(incurred),
        estimated_unbilled=Decimal(unbilled),
        output={"answer": 7} if output is None else output,
        provider_request_id="provider-request-1",
        provider_response_id="provider-response-1",
        usage_id="usage-1",
        billing_id=billing_id,
    )


def prepare_only(orchestrator, budget, call_spec, request, route_descriptor=None):
    route_descriptor = route_descriptor or descriptor()
    pricing = _pricing_evidence(call_spec, (route_descriptor,))
    decision = budget.admit_route(
        fixed_policy(model_id=route_descriptor.model_id),
        request,
        [route_descriptor],
        now_utc=NOW,
        reservation_context=orchestrator._reservation_context(
            call_spec,
            pricing,
        ),
    )
    prepared = orchestrator._prepared_payload(
        attempt_id=orchestrator.attempt_id(call_spec),
        spec=call_spec,
        decision=decision,
        descriptor=route_descriptor,
        pricing=pricing,
        request=request,
    )
    orchestrator._append(
        attempt_id=orchestrator.attempt_id(call_spec),
        event_type="ModelCallPrepared",
        version=1,
        payload=prepared,
    )
    return decision, pricing

class ModelCallLifecycleTests(unittest.TestCase):
    def test_schema_invalid_parent_remains_fallback_eligible_after_billing_evidence(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            parent_request = request_for(orchestrator, parent_spec)
            parent = orchestrator.execute(
                spec=parent_spec,
                policy=fixed_policy(),
                request=parent_request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.3",
                    unbilled="0.4",
                    billing_id="invoice-invalid-fallback",
                    output={"unexpected": True},
                ),
                validate_result=lambda _value: False,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "OBSERVED_INVALID")
            self.assertTrue(
                orchestrator.reconcile_observed_billing(
                    attempt_id=parent.attempt_id,
                    billing_id="invoice-invalid-fallback",
                    expected_billed="0.2",
                )
            )
            self.assertEqual(
                orchestrator._events(parent.attempt_id)[-1]["event_type"],
                "ModelBillingEvidenceObserved",
            )

            fallback_spec = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            fallback_request = request_for(orchestrator, fallback_spec)
            calls = []
            fallback = orchestrator.execute(
                spec=fallback_spec,
                policy=fixed_policy(),
                request=fallback_request,
                descriptors=[descriptor()],
                call=lambda *_args: (
                    calls.append("fallback-call") or observation(
                        incurred="0.1",
                        unbilled="0",
                        billing_id="invoice-fallback-child",
                    )
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )

            self.assertEqual(fallback.status, "OBSERVED_VALID")
            self.assertEqual(calls, ["fallback-call"])

    def test_billing_evidence_does_not_hide_observed_terminal_on_retry(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []

            def observed_call(_binding, _cancelled):
                calls.append("provider-call")
                return observation(
                    incurred="0.3",
                    unbilled="0.4",
                    billing_id="invoice-retry-safe",
                )

            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=observed_call,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertTrue(
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-retry-safe",
                    expected_billed="0.2",
                )
            )

            retry = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            ).execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append("retry-call"),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(retry.status, "OBSERVED_VALID")
            self.assertEqual(calls, ["provider-call"])
            snapshot = budget.snapshot()
            self.assertEqual(snapshot.incurred, Decimal("0.5"))
            self.assertEqual(snapshot.estimated_unbilled, Decimal("0.2"))

    def test_unknown_billing_reconciliation_rejects_untrusted_evidence(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)

            def reject_billing(_attempt, _billing, _scope):
                raise ValueError("invoice line is not issuer-authenticated")

            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
                billing_evidence_resolver=reject_billing,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("provider response lost")
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            before = budget.snapshot()
            with self.assertRaisesRegex(
                ModelCallError,
                "billing evidence could not be authenticated",
            ):
                orchestrator.reconcile_billing(
                    attempt_id=result.attempt_id,
                    billing_id="untrusted-invoice",
                    expected_billed="0.4",
                )
            self.assertEqual(budget.snapshot(), before)

    def test_unknown_billing_reconciliation_is_authenticated_and_restart_safe(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []

            def ambiguous_call(_binding, _cancelled):
                calls.append("provider-call")
                raise TimeoutError("provider response lost")

            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=ambiguous_call,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("1.2"),
            )

            restarted = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            self.assertTrue(
                restarted.reconcile_billing(
                    attempt_id=result.attempt_id,
                    billing_id="late-invoice-line",
                    expected_billed="0.4",
                )
            )
            reconciled = budget.snapshot()
            self.assertEqual(reconciled.incurred, Decimal("0.4"))
            self.assertEqual(
                reconciled.estimated_unbilled,
                Decimal("0.8"),
            )

            retry = restarted.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append("retry-call"),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(retry.status, "UNKNOWN")
            self.assertEqual(calls, ["provider-call"])
            after_retry = budget.snapshot()
            self.assertEqual(after_retry.incurred, Decimal("0.4"))
            self.assertEqual(
                after_retry.estimated_unbilled,
                Decimal("0.8"),
            )

    def test_prepared_restart_rejects_changed_routing_policy(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec()
            original_request = request_for(first, call_spec)
            prepare_only(first, budget, call_spec, original_request)
            restarted = orchestrator_for(budget=budget, clock=clock)

            changed_policies = (
                RoutingPolicy(
                    mode=RoutingMode.ZERO,
                    maximum_cost=Decimal("0"),
                ),
                fixed_policy(allow_remote=False),
                fixed_policy(maximum_cost="1"),
            )
            for changed_policy in changed_policies:
                with self.subTest(policy=changed_policy):
                    with self.assertRaisesRegex(
                        ModelCallError,
                        "routing policy conflicts with durable prepared authority",
                    ):
                        restarted.execute(
                            spec=call_spec,
                            policy=changed_policy,
                            request=original_request,
                            descriptors=[descriptor()],
                            call=lambda *_args: self.fail("must not call"),
                            validate_result=lambda _value: True,
                            now_utc=clock.value,
                        )

            self.assertEqual(
                budget.active_reservation(restarted.attempt_id(call_spec)),
                Decimal("1.2"),
            )

    def test_prepared_restart_rejects_changed_request_authority(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec()
            original = request_for(first, call_spec)
            prepare_only(first, budget, call_spec, original)
            restarted = orchestrator_for(budget=budget, clock=clock)

            changed_requests = (
                ModelRequest(
                    request_id=original.request_id,
                    allowed_model_ids=("model-a", "model-b"),
                    privacy_remote_allowed=original.privacy_remote_allowed,
                    budget_remaining=original.budget_remaining,
                    deadline_utc=original.deadline_utc,
                    cancelled=False,
                ),
                ModelRequest(
                    request_id=original.request_id,
                    allowed_model_ids=original.allowed_model_ids,
                    privacy_remote_allowed=False,
                    budget_remaining=original.budget_remaining,
                    deadline_utc=original.deadline_utc,
                    cancelled=False,
                ),
                ModelRequest(
                    request_id=original.request_id,
                    allowed_model_ids=original.allowed_model_ids,
                    privacy_remote_allowed=original.privacy_remote_allowed,
                    budget_remaining=Decimal("1.5"),
                    deadline_utc=original.deadline_utc,
                    cancelled=False,
                ),
            )
            for changed in changed_requests:
                with self.subTest(changed=changed):
                    with self.assertRaisesRegex(
                        ModelCallError,
                        "identity conflicts with durable prepared attempt",
                    ):
                        restarted.execute(
                            spec=call_spec,
                            policy=fixed_policy(),
                            request=changed,
                            descriptors=[descriptor()],
                            call=lambda *_args: self.fail("must not call"),
                            validate_result=lambda _value: True,
                            now_utc=NOW,
                        )
            self.assertEqual(
                budget.active_reservation(restarted.attempt_id(call_spec)),
                Decimal("1.2"),
            )

    def test_prepared_restart_cannot_extend_original_request_deadline(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec()
            default_request = request_for(first, call_spec)
            original_request = ModelRequest(
                request_id=default_request.request_id,
                allowed_model_ids=default_request.allowed_model_ids,
                privacy_remote_allowed=default_request.privacy_remote_allowed,
                budget_remaining=default_request.budget_remaining,
                deadline_utc=NOW + timedelta(minutes=30),
                cancelled=False,
            )
            prepare_only(first, budget, call_spec, original_request)

            clock.advance(1801)
            restarted = orchestrator_for(budget=budget, clock=clock)
            extended_request = ModelRequest(
                request_id=original_request.request_id,
                allowed_model_ids=original_request.allowed_model_ids,
                privacy_remote_allowed=original_request.privacy_remote_allowed,
                budget_remaining=original_request.budget_remaining,
                deadline_utc=NOW + timedelta(hours=2),
                cancelled=False,
            )
            calls = []
            with self.assertRaisesRegex(
                ModelCallError,
                "identity conflicts with durable prepared attempt",
            ):
                restarted.execute(
                    spec=call_spec,
                    policy=fixed_policy(),
                    request=extended_request,
                    descriptors=[descriptor()],
                    call=lambda *_args: calls.append(True),
                    validate_result=lambda _value: True,
                    now_utc=clock.value,
                )
            self.assertEqual(calls, [])
            self.assertEqual(
                budget.active_reservation(restarted.attempt_id(call_spec)),
                Decimal("1.2"),
            )

            outcome = restarted.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=original_request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=clock.value,
            )
            self.assertEqual(outcome.status, "NOT_SENT")
            self.assertEqual(
                outcome.reason,
                "request_deadline_expired_before_call_boundary",
            )
            self.assertEqual(calls, [])
            self.assertIsNone(
                budget.active_reservation(restarted.attempt_id(call_spec))
            )

    def test_prepared_restart_rejects_expired_request_deadline_before_call_boundary(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec()
            default_request = request_for(first, call_spec)
            request = ModelRequest(
                request_id=default_request.request_id,
                allowed_model_ids=default_request.allowed_model_ids,
                privacy_remote_allowed=default_request.privacy_remote_allowed,
                budget_remaining=default_request.budget_remaining,
                deadline_utc=NOW + timedelta(minutes=30),
                cancelled=False,
            )
            prepare_only(first, budget, call_spec, request)

            clock.advance(1801)
            restarted = orchestrator_for(budget=budget, clock=clock)
            calls = []
            outcome = restarted.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=clock.value,
            )

            self.assertEqual(outcome.status, "NOT_SENT")
            self.assertEqual(
                outcome.reason,
                "request_deadline_expired_before_call_boundary",
            )
            self.assertEqual(calls, [])
            self.assertIsNone(
                budget.active_reservation(restarted.attempt_id(call_spec))
            )

    def test_prepared_restart_rejects_expired_pricing_before_call_boundary(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec()
            request = request_for(first, call_spec)
            prepare_only(first, budget, call_spec, request)

            clock.advance(3601)
            restarted = orchestrator_for(budget=budget, clock=clock)
            calls = []
            outcome = restarted.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=clock.value,
            )

            self.assertEqual(outcome.status, "NOT_SENT")
            self.assertEqual(
                outcome.reason,
                "pricing_evidence_expired_before_call_boundary",
            )
            self.assertEqual(calls, [])
            self.assertIsNone(
                budget.active_reservation(restarted.attempt_id(call_spec))
            )
            self.assertEqual(
                [event["event_type"] for event in restarted._events(outcome.attempt_id)],
                ["ModelCallPrepared", "ModelCallNotSent"],
            )

    def test_call_boundary_rechecks_expiry_after_cancellation_probe(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            orchestrator = orchestrator_for(
                budget=budget,
                clock=clock,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []

            def slow_cancellation_probe():
                clock.advance(3601)
                return False

            outcome = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=NOW,
                cancel_requested=slow_cancellation_probe,
            )

            self.assertEqual(outcome.status, "NOT_SENT")
            self.assertEqual(
                outcome.reason,
                "pricing_evidence_expired_before_call_boundary",
            )
            self.assertEqual(calls, [])
            self.assertIsNone(
                budget.active_reservation(orchestrator.attempt_id(call_spec))
            )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in orchestrator._events(outcome.attempt_id)
                ],
                [
                    "ModelCallPrepared",
                    "ModelCallNotSent",
                ],
            )

    def test_fresh_prepared_call_revalidates_expiry_at_inference_boundary(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            ticks = iter(
                (
                    NOW,
                    NOW,
                    NOW + timedelta(seconds=3601),
                    NOW + timedelta(seconds=3601),
                )
            )

            def boundary_clock():
                try:
                    value = next(ticks)
                except StopIteration:
                    value = NOW + timedelta(seconds=3601)
                return value.isoformat().replace("+00:00", "Z")

            orchestrator = orchestrator_for(
                budget=budget,
                clock=boundary_clock,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []

            outcome = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )

            self.assertEqual(outcome.status, "NOT_SENT")
            self.assertEqual(
                outcome.reason,
                "pricing_evidence_expired_before_call_boundary",
            )
            self.assertEqual(calls, [])
            self.assertIsNone(
                budget.active_reservation(orchestrator.attempt_id(call_spec))
            )
            self.assertEqual(
                [event["event_type"] for event in orchestrator._events(outcome.attempt_id)],
                ["ModelCallPrepared", "ModelCallNotSent"],
            )

    def test_zero_mode_never_calls_and_creates_no_reservation(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            orchestrator = orchestrator_for(
                budget=budget,
                clock=clock,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            policy = RoutingPolicy(
                mode=RoutingMode.ZERO,
                maximum_cost=Decimal("2"),
            )
            calls = []

            outcome = orchestrator.execute(
                spec=call_spec,
                policy=policy,
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )

            self.assertEqual(outcome.status, "NO_MODEL")
            self.assertEqual(calls, [])
            self.assertIsNone(
                budget.active_reservation(orchestrator.attempt_id(call_spec))
            )
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_local_only_ignores_remote_and_calls_admitted_local_model(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(
                orchestrator,
                call_spec,
                allowed_model_ids=("local-a", "remote-a"),
            )
            models = [
                descriptor(
                    model_id="remote-a",
                    provider_id="remote-provider",
                    remote=True,
                    cost="0.2",
                ),
                descriptor(
                    model_id="local-a",
                    provider_id="local-runtime",
                    remote=False,
                    cost="0",
                ),
            ]
            seen = []

            def invoke(binding, _cancel):
                seen.append(binding)
                return observation(
                    provider_id="local-runtime",
                    model_id="local-a",
                    incurred="0",
                    unbilled="0",
                    billing_id=None,
                )

            outcome = orchestrator.execute(
                spec=call_spec,
                policy=local_only_policy("local-a", "remote-a"),
                request=request,
                descriptors=models,
                call=invoke,
                validate_result=lambda value: value == {"answer": 7},
                now_utc=NOW,
            )
            self.assertEqual(outcome.status, "OBSERVED_VALID")
            self.assertEqual(len(seen), 1)
            self.assertFalse(seen[0].remote)
            self.assertEqual(seen[0].provider_id, "local-runtime")

    def test_call_begins_only_with_exact_active_durable_reservation(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            admitted = []

            def invoke(binding, _cancel):
                admitted.append(
                    budget.active_reservation(binding.attempt_id)
                )
                return observation()

            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=invoke,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(result.status, "OBSERVED_VALID")
            self.assertEqual(admitted, [Decimal("1.2")])
            self.assertIsNone(
                budget.active_reservation(orchestrator.attempt_id(call_spec))
            )

    def test_concurrent_same_attempt_has_one_call_owner(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(
                budget=budget,
                clock=clock,
                owner_token="owner-a",
                started_lease_seconds=60,
            )
            second = orchestrator_for(
                budget=budget,
                clock=clock,
                owner_token="owner-b",
                started_lease_seconds=60,
            )
            call_spec = spec()
            request = request_for(first, call_spec)
            entered = threading.Event()
            release = threading.Event()
            calls = []
            results = []
            errors = []

            def invoke(_binding, _cancel):
                calls.append("call")
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("test call was not released")
                return observation()

            def run_first():
                try:
                    results.append(
                        first.execute(
                            spec=call_spec,
                            policy=fixed_policy(),
                            request=request,
                            descriptors=[descriptor()],
                            call=invoke,
                            validate_result=lambda _value: True,
                            now_utc=NOW,
                        )
                    )
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(entered.wait(timeout=5))
            concurrent = second.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=invoke,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(concurrent.status, "IN_PROGRESS")
            self.assertEqual(calls, ["call"])
            release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results[0].status, "OBSERVED_VALID")
            self.assertEqual(calls, ["call"])

    def test_timeout_after_call_boundary_is_unknown_and_retry_never_calls_again(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            clock = MutableClock()
            orchestrator = orchestrator_for(
                budget=budget,
                clock=clock,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []

            def invoke(_binding, _cancel):
                calls.append("call")
                raise TimeoutError("provider response lost")

            first = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=invoke,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(calls, ["call"])
            snap = budget.snapshot()
            self.assertEqual(snap.reserved, Decimal("0"))
            self.assertEqual(snap.incurred, Decimal("0"))
            self.assertEqual(snap.estimated_unbilled, Decimal("1.2"))

            retry = orchestrator_for(
                budget=budget,
                clock=clock,
            ).execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=invoke,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(retry.status, "UNKNOWN")
            self.assertEqual(calls, ["call"])
            events = journal.load_events(
                "model_call_attempt",
                orchestrator._aggregate_id(orchestrator.attempt_id(call_spec)),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["ModelCallPrepared", "ModelCallStarted", "ModelCallUnknown"],
            )

    def test_started_crash_becomes_unknown_only_after_owner_lease(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            orchestrator = orchestrator_for(
                budget=budget,
                clock=clock,
                owner_token="owner-a",
                started_lease_seconds=30,
            )
            call_spec = spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            request = request_for(orchestrator, call_spec)
            decision = budget.admit_route(
                fixed_policy(),
                request,
                [descriptor()],
                now_utc=NOW,
                reservation_context=orchestrator._reservation_context(
                    call_spec,
                    _pricing_evidence(call_spec, (descriptor(),)),
                ),
            )
            pricing = _pricing_evidence(call_spec, (descriptor(),))
            prepared = orchestrator._prepared_payload(
                attempt_id=attempt_id,
                spec=call_spec,
                decision=decision,
                descriptor=descriptor(),
                pricing=pricing,
                request=request,
            )
            orchestrator._append(
                attempt_id=attempt_id,
                event_type="ModelCallPrepared",
                version=1,
                payload=prepared,
            )
            orchestrator._append(
                attempt_id=attempt_id,
                event_type="ModelCallStarted",
                version=2,
                payload={
                    "attempt_id": attempt_id,
                    "owner_token": "dead-owner",
                    "started_at": clock(),
                    "provider_id": decision.provider_id,
                    "model_id": decision.model_id,
                    "revision": decision.revision,
                    "reserved_cost": str(decision.reserved_cost),
                },
                unique_claim=True,
            )

            active = orchestrator_for(
                budget=budget,
                clock=clock,
                started_lease_seconds=30,
            ).execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: self.fail("must not call"),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(active.status, "IN_PROGRESS")
            self.assertEqual(budget.snapshot().reserved, Decimal("1.2"))

            clock.advance(31)
            recovered = orchestrator_for(
                budget=budget,
                clock=clock,
                started_lease_seconds=30,
            ).execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: self.fail("must not retry"),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("1.2"),
            )

    def test_recovery_can_prove_not_sent_before_call_boundary_and_release(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            orchestrator = orchestrator_for(
                budget=budget,
                clock=clock,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            attempt_id = orchestrator.attempt_id(call_spec)
            budget.admit_route(
                fixed_policy(),
                request,
                [descriptor()],
                now_utc=NOW,
                reservation_context=orchestrator._reservation_context(
                    call_spec,
                    _pricing_evidence(call_spec, (descriptor(),)),
                ),
            )
            self.assertEqual(
                budget.active_reservation(attempt_id),
                Decimal("1.2"),
            )
            resolver_calls = []

            def changed_pricing_must_not_run(_spec, _descriptors):
                resolver_calls.append(True)
                raise AssertionError(
                    "reserved recovery must use durable pricing identity"
                )

            restarted = orchestrator_for(
                budget=budget,
                clock=clock,
                pricing_evidence_resolver=changed_pricing_must_not_run,
            )
            fences = []
            recovered = restarted.recover_reserved_not_started(
                spec=call_spec,
                recovery_fence=lambda: fences.append("fenced"),
            )
            self.assertEqual(recovered.status, "NOT_SENT")
            self.assertEqual(fences, ["fenced"])
            self.assertEqual(resolver_calls, [])
            self.assertIsNone(budget.active_reservation(attempt_id))

            calls = []
            repeated = restarted.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(repeated.status, "NOT_SENT")
            self.assertEqual(calls, [])

    def test_pricing_evidence_change_cannot_rebind_existing_reservation(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            original = spec(pricing_evidence_id="pricing-v1")
            changed = spec(pricing_evidence_id="pricing-v2")
            self.assertEqual(
                orchestrator.attempt_id(original),
                orchestrator.attempt_id(changed),
            )
            request = request_for(orchestrator, original)
            budget.admit_route(
                fixed_policy(),
                request,
                [descriptor()],
                now_utc=NOW,
                reservation_context=orchestrator._reservation_context(
                    original,
                    _pricing_evidence(original, (descriptor(),)),
                ),
            )
            with self.assertRaisesRegex(
                ValueError,
                "route identity conflicts",
            ):
                orchestrator.execute(
                    spec=changed,
                    policy=fixed_policy(),
                    request=request,
                    descriptors=[descriptor()],
                    call=lambda *_args: self.fail("must not call"),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )

    def test_prepared_restart_uses_durable_pricing_identity_not_new_resolver(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec(pricing_evidence_id="pricing-v1")
            route_descriptor = descriptor()
            request = request_for(first, call_spec)
            pricing_p1 = _pricing_evidence(call_spec, (route_descriptor,))
            decision = budget.admit_route(
                fixed_policy(),
                request,
                [route_descriptor],
                now_utc=NOW,
                reservation_context=first._reservation_context(
                    call_spec,
                    pricing_p1,
                ),
            )
            prepared = first._prepared_payload(
                attempt_id=first.attempt_id(call_spec),
                spec=call_spec,
                decision=decision,
                descriptor=route_descriptor,
                pricing=pricing_p1,
                request=request,
            )
            first._append(
                attempt_id=first.attempt_id(call_spec),
                event_type="ModelCallPrepared",
                version=1,
                payload=prepared,
            )

            resolver_calls = []
            def newer_pricing(_spec, _descriptors):
                resolver_calls.append(True)
                raise AssertionError(
                    "prepared restart must not resolve or rebind pricing"
                )

            restarted = orchestrator_for(
                budget=budget,
                clock=clock,
                pricing_evidence_resolver=newer_pricing,
            )
            bindings = []
            def prove_not_sent(binding, _cancelled):
                bindings.append(binding)
                raise ModelCallNotSent("restart test")

            outcome = restarted.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[route_descriptor],
                call=prove_not_sent,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(outcome.reason, "adapter_not_sent_claim_unverified")
            self.assertEqual(resolver_calls, [])
            self.assertEqual(len(bindings), 1)
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("1.2"),
            )
            self.assertEqual(
                bindings[0].pricing_evidence_digest,
                pricing_p1.evidence_digest,
            )

    def test_prepared_restart_can_observe_without_rebinding_pricing(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            first = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec(pricing_evidence_id="pricing-v1")
            route_descriptor = descriptor()
            request = request_for(first, call_spec)
            pricing_p1 = _pricing_evidence(call_spec, (route_descriptor,))
            decision = budget.admit_route(
                fixed_policy(),
                request,
                [route_descriptor],
                now_utc=NOW,
                reservation_context=first._reservation_context(
                    call_spec,
                    pricing_p1,
                ),
            )
            prepared = first._prepared_payload(
                attempt_id=first.attempt_id(call_spec),
                spec=call_spec,
                decision=decision,
                descriptor=route_descriptor,
                pricing=pricing_p1,
                request=request,
            )
            first._append(
                attempt_id=first.attempt_id(call_spec),
                event_type="ModelCallPrepared",
                version=1,
                payload=prepared,
            )

            resolver_calls = []

            def newer_pricing(_spec, _descriptors):
                resolver_calls.append(True)
                raise AssertionError(
                    "prepared restart must use durable pricing identity"
                )

            restarted = orchestrator_for(
                budget=budget,
                clock=clock,
                pricing_evidence_resolver=newer_pricing,
            )
            outcome = restarted.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[route_descriptor],
                call=lambda _binding, _cancelled: observation(),
                validate_result=lambda value: value == {"answer": 7},
                now_utc=NOW,
            )

            self.assertEqual(outcome.status, "OBSERVED_VALID")
            self.assertEqual(resolver_calls, [])
            observed = [
                event
                for event in restarted._events(restarted.attempt_id(call_spec))
                if event.get("event_type") == "ModelCallObserved"
            ]
            self.assertEqual(len(observed), 1)
            self.assertEqual(
                observed[0]["payload"]["pricing_evidence_digest"],
                pricing_p1.evidence_digest,
            )

    def test_unverified_or_mismatched_pricing_fails_before_reservation(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)

            def wrong_currency(call_spec, descriptors):
                sealed = _pricing_evidence(call_spec, descriptors)
                return PricingEvidenceSnapshot(
                    evidence_id=sealed.evidence_id,
                    evidence_digest=sealed.evidence_digest,
                    as_of=sealed.as_of,
                    valid_until=sealed.valid_until,
                    cost_currency="EUR",
                    quotes=sealed.quotes,
                )

            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
                pricing_evidence_resolver=wrong_currency,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            with self.assertRaisesRegex(ModelCallError, "currency"):
                orchestrator.execute(
                    spec=call_spec,
                    policy=fixed_policy(),
                    request=request,
                    descriptors=[descriptor()],
                    call=lambda *_args: self.fail("must not call"),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )
            snap = budget.snapshot()
            self.assertEqual(snap.reserved, Decimal("0"))
            self.assertEqual(snap.incurred, Decimal("0"))

    def test_usage_observation_without_authenticated_evidence_stays_unknown(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)

            def reject_observation(_observation, _binding):
                raise ValueError("raw adapter assertion is not sealed")

            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
                observation_evidence_resolver=reject_observation,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            outcome = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.01",
                    unbilled="0",
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(outcome.status, "UNKNOWN")
            snap = budget.snapshot()
            self.assertEqual(snap.incurred, Decimal("0"))
            self.assertEqual(snap.estimated_unbilled, Decimal("1.2"))

    def test_schema_invalid_result_still_settles_observed_cost(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.4",
                    unbilled="0.2",
                    output={"hallucinated": True},
                ),
                validate_result=lambda _value: False,
                now_utc=NOW,
            )
            self.assertEqual(result.status, "OBSERVED_INVALID")
            self.assertIsNone(result.output)
            snap = budget.snapshot()
            self.assertEqual(snap.incurred, Decimal("0.4"))
            self.assertEqual(snap.estimated_unbilled, Decimal("0.2"))

    def test_provider_model_mismatch_is_unknown_not_free_release(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(provider_id="other-provider"),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(result.status, "UNKNOWN")
            snap = budget.snapshot()
            self.assertEqual(snap.incurred, Decimal("0"))
            self.assertEqual(snap.estimated_unbilled, Decimal("1.2"))

    def test_post_started_not_sent_claim_is_unknown_and_idempotent(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []

            def invoke(_binding, _cancel):
                calls.append("call")
                # An injected adapter may have crossed a paid boundary before
                # making this claim. Its exception type is not billing proof.
                raise ModelCallNotSent("socket was never opened")

            first = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=invoke,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(first.reason, "adapter_not_sent_claim_unverified")
            snapshot = budget.snapshot()
            self.assertEqual(snapshot.reserved, Decimal("0"))
            self.assertEqual(snapshot.incurred, Decimal("0"))
            self.assertEqual(snapshot.estimated_unbilled, Decimal("1.2"))

            second = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=invoke,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(second.status, "UNKNOWN")
            self.assertEqual(calls, ["call"])
            events = journal.load_events(
                "model_call_attempt",
                orchestrator._aggregate_id(orchestrator.attempt_id(call_spec)),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["ModelCallPrepared", "ModelCallStarted", "ModelCallUnknown"],
            )

    def test_pre_call_cancellation_releases_without_invocation(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []
            outcome = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=NOW,
                cancel_requested=lambda: True,
            )
            self.assertEqual(outcome.status, "NOT_SENT")
            self.assertEqual(calls, [])
            self.assertIsNone(
                budget.active_reservation(orchestrator.attempt_id(call_spec))
            )

    def test_observed_billing_reconciliation_is_bound_to_attempt_identity(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.3",
                    unbilled="0.4",
                    billing_id="invoice-line-7",
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(result.status, "OBSERVED_VALID")
            self.assertTrue(
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-line-7",
                    expected_billed="0.25",
                )
            )
            with self.assertRaisesRegex(
                ModelCallError,
                "billing identity does not match",
            ):
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="other-line",
                    expected_billed="0.25",
                )

    def test_self_authored_billing_line_cannot_reconcile_unbilled_cost(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)

            def reject_billing(_attempt, _billing, _observed):
                raise ValueError("invoice line is not issuer-authenticated")

            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
                billing_evidence_resolver=reject_billing,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.3",
                    unbilled="0.4",
                    billing_id="invoice-self-authored",
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            before = budget.snapshot()
            with self.assertRaisesRegex(ModelCallError, "could not be authenticated"):
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-self-authored",
                    expected_billed="0.25",
                )
            after = budget.snapshot()
            self.assertEqual(after.incurred, before.incurred)
            self.assertEqual(after.estimated_unbilled, before.estimated_unbilled)

    def test_reused_billing_identity_with_changed_amount_fails_closed(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            amounts = iter((Decimal("0.2"), Decimal("0.3")))

            def changing_billing(attempt_id, billing_id, observed_payload):
                billed = next(amounts)
                return BillingEvidence(
                    attempt_id=attempt_id,
                    billing_id=billing_id,
                    provider_id=observed_payload["provider_id"],
                    model_id=observed_payload["model_id"],
                    revision=observed_payload.get("revision"),
                    billed=billed,
                    cost_currency=observed_payload["cost_currency"],
                    observed_at="2026-09-25T10:05:00Z",
                    evidence_id="billing-evidence:" + billing_id,
                    evidence_digest=payload_digest(
                        {
                            "attempt_id": attempt_id,
                            "billing_id": billing_id,
                            "billed": str(billed),
                        }
                    ),
                    issuer=observed_payload["provider_id"],
                )

            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
                billing_evidence_resolver=changing_billing,
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.3",
                    unbilled="0.5",
                    billing_id="invoice-stable",
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertTrue(
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-stable",
                )
            )
            before = budget.snapshot()
            with self.assertRaisesRegex(
                ModelCallError,
                "conflicting immutable evidence",
            ):
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-stable",
                )
            self.assertEqual(budget.snapshot(), before)

    def test_billing_amount_is_evidence_derived_and_scope_is_immutable(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            mutation_blocked = []

            def evidence_authority(attempt_id, billing_id, observed_payload):
                try:
                    observed_payload["provider_id"] = "forged-provider"
                except TypeError:
                    mutation_blocked.append(True)
                return BillingEvidence(
                    attempt_id=attempt_id,
                    billing_id=billing_id,
                    provider_id=observed_payload["provider_id"],
                    model_id=observed_payload["model_id"],
                    revision=observed_payload.get("revision"),
                    billed=Decimal("0.25"),
                    cost_currency=observed_payload["cost_currency"],
                    observed_at="2026-09-25T10:05:00Z",
                    evidence_id="billing-evidence:" + billing_id,
                    evidence_digest=payload_digest(
                        {
                            "attempt_id": attempt_id,
                            "billing_id": billing_id,
                            "billed": "0.25",
                        }
                    ),
                    issuer=observed_payload["provider_id"],
                )

            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
                billing_evidence_resolver=evidence_authority,
            )
            call_spec = spec()
            result = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request_for(orchestrator, call_spec),
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.3",
                    unbilled="0.4",
                    billing_id="invoice-evidence-derived",
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            before = budget.snapshot()
            with self.assertRaisesRegex(
                ModelCallError,
                "amount does not match caller expectation",
            ):
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-evidence-derived",
                    expected_billed="0.2",
                )
            self.assertEqual(budget.snapshot(), before)
            self.assertTrue(
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-evidence-derived",
                    expected_billed="0.25",
                )
            )
            self.assertEqual(mutation_blocked, [True, True])
            self.assertEqual(budget.snapshot().incurred, Decimal("0.55"))
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("0.15"),
            )


    def test_fallback_requires_real_durable_parent(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            fallback = spec(
                fallback_parent_attempt_id="model-attempt-" + "f" * 64,
                fallback_index=1,
            )
            calls = []
            with self.assertRaisesRegex(
                ModelCallError,
                "fallback parent attempt does not exist",
            ):
                orchestrator.execute(
                    spec=fallback,
                    policy=fixed_policy(),
                    request=request_for(orchestrator, fallback),
                    descriptors=[descriptor()],
                    call=lambda *_args: calls.append(True),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )
            self.assertEqual(calls, [])
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))
            self.assertEqual(budget.snapshot().incurred, Decimal("0"))
            self.assertEqual(budget.snapshot().estimated_unbilled, Decimal("0"))

    def test_unknown_parent_cannot_be_blindly_retried_as_fallback(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            parent = orchestrator.execute(
                spec=parent_spec,
                policy=fixed_policy(),
                request=request_for(orchestrator, parent_spec),
                descriptors=[descriptor()],
                call=lambda *_args: (_ for _ in ()).throw(
                    RuntimeError("ambiguous remote boundary")
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "UNKNOWN")

            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            calls = []
            with self.assertRaisesRegex(
                ModelCallError,
                "outcome is uncertain",
            ):
                orchestrator.execute(
                    spec=fallback,
                    policy=fixed_policy(),
                    request=request_for(orchestrator, fallback),
                    descriptors=[descriptor()],
                    call=lambda *_args: calls.append(True),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )
            self.assertEqual(calls, [])
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("1.2"),
            )

    def test_schema_invalid_parent_can_start_one_direct_fallback(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            parent = orchestrator.execute(
                spec=parent_spec,
                policy=fixed_policy(),
                request=request_for(orchestrator, parent_spec),
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.2",
                    unbilled="0",
                    output={"invalid": True},
                ),
                validate_result=lambda _value: False,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "OBSERVED_INVALID")

            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            calls = []

            def fallback_call(*_args):
                calls.append(True)
                return observation(incurred="0.1", unbilled="0")

            child = orchestrator.execute(
                spec=fallback,
                policy=fixed_policy(),
                request=request_for(orchestrator, fallback),
                descriptors=[descriptor()],
                call=fallback_call,
                validate_result=lambda value: value == {"answer": 7},
                now_utc=NOW,
            )
            self.assertEqual(child.status, "OBSERVED_VALID")
            self.assertEqual(calls, [True])

            skipped = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=2,
            )
            with self.assertRaisesRegex(
                ModelCallError,
                "fallback index must directly follow",
            ):
                orchestrator.execute(
                    spec=skipped,
                    policy=fixed_policy(),
                    request=request_for(orchestrator, skipped),
                    descriptors=[descriptor()],
                    call=lambda *_args: self.fail("must not call"),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )

    def test_fallback_policy_cannot_widen_under_same_policy_id(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            parent = orchestrator.execute(
                spec=parent_spec,
                policy=fixed_policy(maximum_cost="2"),
                request=request_for(orchestrator, parent_spec),
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="0.2",
                    unbilled="0",
                    output={"invalid": True},
                ),
                validate_result=lambda _value: False,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "OBSERVED_INVALID")
            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            calls = []
            with self.assertRaisesRegex(
                ModelCallError,
                "fallback policy must match",
            ):
                orchestrator.execute(
                    spec=fallback,
                    policy=fixed_policy(maximum_cost="3"),
                    request=request_for(orchestrator, fallback),
                    descriptors=[descriptor()],
                    call=lambda *_args: calls.append(True),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )
            self.assertEqual(calls, [])

    def test_fallback_request_cannot_widen_parent_remote_privacy(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            parent_request = ModelRequest(
                request_id=orchestrator.attempt_id(parent_spec),
                allowed_model_ids=("model-a",),
                privacy_remote_allowed=False,
                budget_remaining=Decimal("2"),
                deadline_utc=NOW + timedelta(hours=1),
            )
            policy = fixed_policy(allow_remote=True)
            parent = orchestrator.execute(
                spec=parent_spec,
                policy=policy,
                request=parent_request,
                descriptors=[
                    descriptor(
                        provider_id="local-runtime",
                        remote=False,
                    )
                ],
                call=lambda *_args: observation(
                    provider_id="local-runtime",
                    incurred="0.1",
                    unbilled="0",
                    output={"invalid": True},
                    billing_id=None,
                ),
                validate_result=lambda _value: False,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "OBSERVED_INVALID")

            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            widened_request = request_for(orchestrator, fallback)
            calls = []
            with self.assertRaisesRegex(
                ModelCallError,
                "cannot widen remote privacy permission",
            ):
                orchestrator.execute(
                    spec=fallback,
                    policy=policy,
                    request=widened_request,
                    descriptors=[descriptor()],
                    call=lambda *_args: calls.append(True),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )
            self.assertEqual(calls, [])

    def test_fallback_lineage_remains_local_only_when_policy_is_local_only(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            local_policy = local_only_policy("local-a", "remote-only")
            parent_spec = spec()
            parent = orchestrator.execute(
                spec=parent_spec,
                policy=local_policy,
                request=request_for(
                    orchestrator,
                    parent_spec,
                    allowed_model_ids=("local-a", "remote-only"),
                ),
                descriptors=[
                    descriptor(
                        model_id="local-a",
                        provider_id="local-runtime",
                        remote=False,
                        cost="0",
                    ),
                    descriptor(
                        model_id="remote-only",
                        provider_id="remote-provider",
                        remote=True,
                        cost="0.1",
                    ),
                ],
                call=lambda *_args: (_ for _ in ()).throw(
                    ModelCallNotSent("local boundary was not crossed")
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "NOT_SENT")
            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            request = request_for(
                orchestrator,
                fallback,
                allowed_model_ids=("remote-only",),
            )
            calls = []
            outcome = orchestrator.execute(
                spec=fallback,
                policy=local_policy,
                request=request,
                descriptors=[
                    descriptor(
                        model_id="remote-only",
                        provider_id="remote-provider",
                        remote=True,
                        cost="0.1",
                    )
                ],
                call=lambda *_args: calls.append(True),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(outcome.status, "NO_MODEL")
            self.assertEqual(calls, [])

    def test_over_reserved_observed_cost_is_conservative_unknown(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            outcome = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=lambda *_args: observation(
                    incurred="1.1",
                    unbilled="0.2",
                ),
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("1.2"),
            )


if __name__ == "__main__":
    unittest.main()


class ModelCallIntegrityTests(unittest.TestCase):
    def _run(self, directory, *, call, validator=lambda _: True, **kwargs):
        journal, budget = open_budget(directory)
        clock = MutableClock()
        orchestrator = orchestrator_for(budget=budget, clock=clock, **kwargs)
        call_spec = spec()
        result = orchestrator.execute(spec=call_spec, policy=fixed_policy(),
            request=request_for(orchestrator, call_spec), descriptors=[descriptor()],
            call=call, validate_result=validator, now_utc=NOW)
        return journal, budget, orchestrator, result

    def test_validator_cannot_rewrite_authenticated_result(self):
        response = observation()
        def mutate(value):
            value["answer"] = 999
            response.output["answer"] = 123
            object.__setattr__(response, "incurred_cost", Decimal("0"))
            return True
        with TemporaryDirectory() as directory:
            _, budget, orchestrator, result = self._run(directory, call=lambda *_: response, validator=mutate)
            self.assertEqual(result.output, {"answer": 7})
            self.assertEqual(result.result_digest, payload_digest({"answer": 7}))
            self.assertEqual(budget.snapshot().incurred, Decimal("0.4"))
            event = orchestrator._events(result.attempt_id)[-1]["payload"]
            self.assertEqual(event["result_digest"], payload_digest({"answer": 7}))
            self.assertEqual(event["observation_digest"], _observation_evidence(observation(),
                orchestrator._binding(attempt_id=result.attempt_id, spec=spec(), decision=result.route,
                    descriptor=descriptor(), pricing_evidence_digest=event["pricing_evidence_digest"])).observation_digest)

    def test_resolver_cannot_mutate_response_then_self_bind_new_evidence(self):
        def mutating_resolver(value, binding):
            value.output["answer"] = 999
            return _observation_evidence(value, binding)
        with TemporaryDirectory() as directory:
            _, budget, _, result = self._run(directory, call=lambda *_: observation(),
                observation_evidence_resolver=mutating_resolver)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(budget.snapshot().estimated_unbilled, Decimal("1.2"))
            self.assertEqual(budget.snapshot().incurred, Decimal("0"))

    def test_subclass_observation_cannot_grant_cost_authority(self):
        class HostileObservation(ModelCallObservation):
            def __getattribute__(self, name):
                raise AssertionError("subclass must not be dispatched")
        value = object.__new__(HostileObservation)
        with TemporaryDirectory() as directory:
            _, budget, _, result = self._run(directory, call=lambda *_: value)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(budget.snapshot().estimated_unbilled, Decimal("1.2"))

    def test_tampered_scalar_graph_is_unknown_before_evidence_callback(self):
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("scalar callback must not execute")
        response = observation()
        object.__setattr__(response, "incurred_cost", HostileDecimal("0.4"))
        with TemporaryDirectory() as directory:
            _, budget, _, result = self._run(directory, call=lambda *_: response,
                observation_evidence_resolver=lambda *_: self.fail("invalid scalar reached evidence resolver"))
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(budget.snapshot().estimated_unbilled, Decimal("1.2"))

    def test_observed_settlement_crash_repairs_without_second_call(self):
        with TemporaryDirectory() as directory:
            journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(budget=budget, clock=MutableClock())
            call_spec = spec()
            args = dict(spec=call_spec, policy=fixed_policy(), request=request_for(orchestrator, call_spec),
                descriptors=[descriptor()], validate_result=lambda _: False, now_utc=NOW)
            with patch.object(budget, "settle", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    orchestrator.execute(**args, call=lambda *_: observation())
            self.assertEqual(orchestrator._events(orchestrator.attempt_id(call_spec))[-1]["event_type"], "ModelCallObserved")
            self.assertEqual(budget.snapshot().reserved, Decimal("1.2"))
            _, restarted_budget = open_budget(directory)
            restarted = orchestrator_for(budget=restarted_budget, clock=MutableClock())
            result = restarted.execute(**args, call=lambda *_: self.fail("durable observed retry crossed call boundary"))
            self.assertEqual(result.status, "OBSERVED_INVALID")
            self.assertEqual(restarted_budget.snapshot().incurred, Decimal("0.4"))
            self.assertEqual(restarted_budget.snapshot().reserved, Decimal("0"))

    def test_not_sent_release_crash_repairs_without_call(self):
        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory)
            orchestrator = orchestrator_for(budget=budget, clock=MutableClock())
            call_spec = spec()
            args = dict(spec=call_spec, policy=fixed_policy(), request=request_for(orchestrator, call_spec),
                descriptors=[descriptor()], validate_result=lambda _: True, now_utc=NOW)
            with patch.object(budget, "release", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    orchestrator.execute(**args, call=lambda *_: self.fail("cancelled call"), cancel_requested=lambda: True)
            self.assertEqual(budget.snapshot().reserved, Decimal("1.2"))
            result = orchestrator.execute(**args, call=lambda *_: self.fail("NOT_SENT retry called"))
            self.assertEqual(result.status, "NOT_SENT")
            self.assertEqual(budget.snapshot().available, Decimal("5"))

    def test_not_sent_error_text_cannot_enter_journal(self):
        def not_sent(*_):
            raise ModelCallNotSent("TEST_SECRET_DO_NOT_PERSIST")
        with TemporaryDirectory() as directory:
            _, _, orchestrator, result = self._run(directory, call=not_sent)
            self.assertEqual(result.status, "NOT_SENT")
            self.assertNotIn("TEST_SECRET_DO_NOT_PERSIST", str(orchestrator._events(result.attempt_id)))

    def test_callback_cannot_extend_original_request_deadline(self):
        with TemporaryDirectory() as directory:
            _, budget = open_budget(directory)
            clock = MutableClock()
            orchestrator = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec()
            original = request_for(orchestrator, call_spec)
            object.__setattr__(original, "deadline_utc", NOW + timedelta(seconds=30))
            def mutate_request():
                object.__setattr__(original, "deadline_utc", NOW + timedelta(hours=5))
                clock.advance(31)
                return False
            result = orchestrator.execute(spec=call_spec, policy=fixed_policy(), request=original,
                descriptors=[descriptor()], call=lambda *_: self.fail("deadline was extended"),
                validate_result=lambda _: True, now_utc=NOW, cancel_requested=mutate_request)
            self.assertEqual(result.status, "NOT_SENT")
            self.assertEqual(result.reason, "request_deadline_expired_before_call_boundary")

    def test_sub_precision_observed_overrun_stays_unknown(self):
        with TemporaryDirectory() as directory, localcontext() as ctx:
            ctx.prec = 3
            ctx.rounding = ROUND_DOWN
            _, budget, _, result = self._run(directory, call=lambda *_: observation(incurred="1.2", unbilled="0.00009"))
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(budget.snapshot().estimated_unbilled, Decimal("1.2"))


    def test_process_exit_after_started_recovers_unknown_with_zero_calls(self):
        with TemporaryDirectory() as directory:
            script = """
import sys
from mvp.tests.test_model_call import open_budget, orchestrator_for, MutableClock, spec, request_for, fixed_policy, descriptor, NOW
_, budget = open_budget(sys.argv[1])
owner = orchestrator_for(budget=budget, clock=MutableClock())
call_spec = spec()
def process_exit(*_):
    raise SystemExit(44)
owner.execute(spec=call_spec, policy=fixed_policy(), request=request_for(owner, call_spec),
              descriptors=[descriptor()], call=process_exit, validate_result=lambda _: True, now_utc=NOW)
"""
            completed = subprocess.run([sys.executable, "-c", script, directory],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 44, completed.stderr)
            _, budget = open_budget(directory)
            clock = MutableClock()
            clock.advance(61)
            restarted = orchestrator_for(budget=budget, clock=clock)
            call_spec = spec()
            result = restarted.execute(spec=call_spec, policy=fixed_policy(),
                request=request_for(restarted, call_spec), descriptors=[descriptor()],
                call=lambda *_: self.fail("process restart resent uncertain call"),
                validate_result=lambda _: True, now_utc=NOW)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(budget.snapshot().estimated_unbilled, Decimal("1.2"))
            self.assertTrue(restarted.reconcile_billing(attempt_id=result.attempt_id,
                billing_id="bill-after-process-exit", expected_billed="0.6"))
            self.assertEqual(budget.snapshot().incurred, Decimal("0.6"))
            self.assertEqual(budget.snapshot().estimated_unbilled, Decimal("0.6"))
            second = restarted.execute(spec=call_spec, policy=fixed_policy(),
                request=request_for(restarted, call_spec), descriptors=[descriptor()],
                call=lambda *_: self.fail("billing evidence reopened UNKNOWN"),
                validate_result=lambda _: True, now_utc=NOW)
            self.assertEqual(second.status, "UNKNOWN")


    def test_validator_cannot_rewrite_resolver_retained_observation(self):
        retained = []
        def resolver(value, binding):
            retained.append(value)
            return _observation_evidence(value, binding)
        def validator(_):
            object.__setattr__(retained[0], "incurred_cost", Decimal("0"))
            object.__setattr__(retained[0], "revision", "unqualified-revision")
            retained[0].output["answer"] = 999
            return True
        with TemporaryDirectory() as directory:
            _, budget, orchestrator, result = self._run(directory, call=lambda *_: observation(),
                validator=validator, observation_evidence_resolver=resolver)
            self.assertEqual(result.output, {"answer": 7})
            self.assertEqual(budget.snapshot().incurred, Decimal("0.4"))
            payload = orchestrator._events(result.attempt_id)[-1]["payload"]
            self.assertEqual(payload["revision"], "r1")
            self.assertEqual(payload["incurred_cost"], "0.4")
