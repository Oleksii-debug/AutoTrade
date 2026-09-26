from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

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


def _billing_evidence(attempt_id, billing_id, billed, observed_payload):
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
                    "ModelCallStarted",
                    "ModelCallNotSent",
                ],
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
            self.assertEqual(outcome.status, "NOT_SENT")
            self.assertEqual(resolver_calls, [])
            self.assertEqual(len(bindings), 1)
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

    def test_explicit_not_sent_proof_releases_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            call_spec = spec()
            request = request_for(orchestrator, call_spec)
            calls = []

            def invoke(_binding, _cancel):
                calls.append("call")
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
            self.assertEqual(first.status, "NOT_SENT")
            self.assertIsNone(
                budget.active_reservation(orchestrator.attempt_id(call_spec))
            )
            second = orchestrator.execute(
                spec=call_spec,
                policy=fixed_policy(),
                request=request,
                descriptors=[descriptor()],
                call=invoke,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(second.status, "NOT_SENT")
            self.assertEqual(calls, ["call"])

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
                    billed="0.25",
                )
            )
            with self.assertRaisesRegex(
                ModelCallError,
                "billing identity does not match",
            ):
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="other-line",
                    billed="0.25",
                )

    def test_self_authored_billing_line_cannot_reconcile_unbilled_cost(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)

            def reject_billing(_attempt, _billing, _billed, _observed):
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
                    billed="0.25",
                )
            after = budget.snapshot()
            self.assertEqual(after.incurred, before.incurred)
            self.assertEqual(after.estimated_unbilled, before.estimated_unbilled)

    def test_reused_billing_identity_with_changed_amount_fails_closed(self):
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
                    billed="0.2",
                )
            )
            before = budget.snapshot()
            with self.assertRaisesRegex(ModelCallError, "conflicting immutable evidence"):
                orchestrator.reconcile_observed_billing(
                    attempt_id=result.attempt_id,
                    billing_id="invoice-stable",
                    billed="0.3",
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
                    billed="0.4",
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

    def test_unknown_billing_reconciliation_rejects_untrusted_evidence(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)

            def reject_billing(_attempt, _billing, _billed, _scope):
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
                    billed="0.4",
                )
            self.assertEqual(budget.snapshot(), before)

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
                    billed="0.2",
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
                    billed="0.2",
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

    def test_fallback_lineage_remains_local_only_when_policy_is_local_only(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            policy = local_only_policy("local-a", "remote-only")
            parent_request = request_for(
                orchestrator,
                parent_spec,
                allowed_model_ids=("local-a", "remote-only"),
            )

            def parent_not_sent(_binding, _cancelled):
                raise ModelCallNotSent("local model unavailable")

            parent = orchestrator.execute(
                spec=parent_spec,
                policy=policy,
                request=parent_request,
                descriptors=[
                    descriptor(
                        model_id="local-a",
                        provider_id="local-provider",
                        remote=False,
                        cost="0.1",
                    )
                ],
                call=parent_not_sent,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "NOT_SENT")

            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            fallback_request = request_for(
                orchestrator,
                fallback,
                allowed_model_ids=("remote-only",),
            )
            calls = []
            outcome = orchestrator.execute(
                spec=fallback,
                policy=policy,
                request=fallback_request,
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

    def test_fallback_rejects_policy_broadening_after_local_parent(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            parent_policy = local_only_policy("local-a", "remote-only")
            parent_request = request_for(
                orchestrator,
                parent_spec,
                allowed_model_ids=("local-a", "remote-only"),
            )

            def parent_not_sent(_binding, _cancelled):
                raise ModelCallNotSent("local model unavailable")

            parent = orchestrator.execute(
                spec=parent_spec,
                policy=parent_policy,
                request=parent_request,
                descriptors=[
                    descriptor(
                        model_id="local-a",
                        provider_id="local-provider",
                        remote=False,
                        cost="0.1",
                    )
                ],
                call=parent_not_sent,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            fallback_request = request_for(
                orchestrator,
                fallback,
                allowed_model_ids=("remote-only",),
            )
            calls = []
            with self.assertRaisesRegex(
                ModelCallError,
                "fallback routing policy must match parent routing authority",
            ):
                orchestrator.execute(
                    spec=fallback,
                    policy=fixed_policy(
                        model_id="remote-only",
                        allow_remote=True,
                        maximum_cost="2",
                    ),
                    request=fallback_request,
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
            self.assertEqual(calls, [])
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_fallback_rejects_unknown_parent_without_second_call(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            parent_spec = spec()
            parent_request = request_for(orchestrator, parent_spec)
            calls = []

            def ambiguous_parent(_binding, _cancelled):
                calls.append("parent")
                raise TimeoutError("provider response lost")

            parent = orchestrator.execute(
                spec=parent_spec,
                policy=fixed_policy(),
                request=parent_request,
                descriptors=[descriptor()],
                call=ambiguous_parent,
                validate_result=lambda _value: True,
                now_utc=NOW,
            )
            self.assertEqual(parent.status, "UNKNOWN")

            fallback = spec(
                fallback_parent_attempt_id=parent.attempt_id,
                fallback_index=1,
            )
            fallback_request = request_for(orchestrator, fallback)
            with self.assertRaisesRegex(
                ModelCallError,
                "fallback cannot continue from uncertain parent call",
            ):
                orchestrator.execute(
                    spec=fallback,
                    policy=fixed_policy(),
                    request=fallback_request,
                    descriptors=[descriptor()],
                    call=lambda *_args: calls.append("child"),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )
            self.assertEqual(calls, ["parent"])
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("1.2"),
            )

    def test_fallback_requires_durable_parent_evidence(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = orchestrator_for(
                budget=budget,
                clock=MutableClock(),
            )
            fallback = spec(
                fallback_parent_attempt_id="missing-parent-attempt",
                fallback_index=1,
            )
            request = request_for(orchestrator, fallback)
            calls = []
            with self.assertRaisesRegex(
                ModelCallError,
                "fallback parent has no durable model-call evidence",
            ):
                orchestrator.execute(
                    spec=fallback,
                    policy=fixed_policy(),
                    request=request,
                    descriptors=[descriptor()],
                    call=lambda *_args: calls.append(True),
                    validate_result=lambda _value: True,
                    now_utc=NOW,
                )
            self.assertEqual(calls, [])
            self.assertEqual(budget.snapshot().reserved, Decimal("0"))

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
