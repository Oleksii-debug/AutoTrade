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
    ModelCallObservation,
    ModelCallSpec,
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


class ModelCallLifecycleTests(unittest.TestCase):
    def test_zero_mode_never_calls_and_creates_no_reservation(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            clock = MutableClock()
            orchestrator = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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
            first = DurableModelCallOrchestrator(
                budget=budget,
                clock=clock,
                owner_token="owner-a",
                started_lease_seconds=60,
            )
            second = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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

            retry = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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
                reservation_context=orchestrator._reservation_context(call_spec),
            )
            prepared = orchestrator._prepared_payload(
                attempt_id=attempt_id,
                spec=call_spec,
                decision=decision,
                descriptor=descriptor(),
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

            active = DurableModelCallOrchestrator(
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
            recovered = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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
                reservation_context=orchestrator._reservation_context(call_spec),
            )
            self.assertEqual(
                budget.active_reservation(attempt_id),
                Decimal("1.2"),
            )
            fences = []
            recovered = orchestrator.recover_reserved_not_started(
                spec=call_spec,
                recovery_fence=lambda: fences.append("fenced"),
            )
            self.assertEqual(recovered.status, "NOT_SENT")
            self.assertEqual(fences, ["fenced"])
            self.assertIsNone(budget.active_reservation(attempt_id))

            calls = []
            repeated = orchestrator.execute(
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
            orchestrator = DurableModelCallOrchestrator(
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
                reservation_context=orchestrator._reservation_context(original),
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

    def test_schema_invalid_result_still_settles_observed_cost(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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
            orchestrator = DurableModelCallOrchestrator(
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

    def test_fallback_lineage_remains_local_only_when_policy_is_local_only(self):
        with TemporaryDirectory() as directory:
            _journal, budget = open_budget(directory)
            orchestrator = DurableModelCallOrchestrator(
                budget=budget,
                clock=MutableClock(),
            )
            parent = orchestrator.attempt_id(spec())
            fallback = spec(
                fallback_parent_attempt_id=parent,
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
                policy=local_only_policy("remote-only"),
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
            orchestrator = DurableModelCallOrchestrator(
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
