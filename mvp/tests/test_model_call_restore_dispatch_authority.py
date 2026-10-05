from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_call as model_call_module
from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_call import (
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
ORIGINAL_ORCHESTRATOR_CLASS = model_call_module.DurableModelCallOrchestrator


def _open_budget(directory):
    journal = JournalStore(Path(directory) / "journal.db")
    budget = DurableModelBudget(
        journal=journal,
        budget_id="restore-dispatch-budget",
        ceiling="5",
        environment="PAPER",
        clock=lambda: NOW_TEXT,
    )
    return journal, budget


def _spec():
    return ModelCallSpec(
        job_id="restore-dispatch-job",
        input_digest="sha256:" + "1" * 64,
        policy_id="restore-dispatch-policy",
        pricing_evidence_id="restore-dispatch-pricing",
        pricing_as_of=NOW_TEXT,
        result_schema_id="restore-dispatch-schema",
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
    return ORIGINAL_ORCHESTRATOR_CLASS(
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


class ModelCallRestoreDispatchAuthorityTests(unittest.TestCase):
    def test_cancel_callback_cannot_redirect_restore_dispatch_through_module_class_alias(self):
        class DecoyOrchestrator:
            restore_calls = 0

            @staticmethod
            def _restore_callback_shape(*_args, **_kwargs):
                DecoyOrchestrator.restore_calls += 1
                raise AssertionError("callback redirected restore dispatch")

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            inference_calls = []

            def hostile_cancel():
                model_call_module.DurableModelCallOrchestrator = DecoyOrchestrator
                return False

            try:
                with self.assertRaises(ModelCallError):
                    orchestrator.execute(
                        spec=call_spec,
                        policy=_policy(),
                        request=_request(orchestrator, call_spec),
                        descriptors=[_descriptor()],
                        call=lambda *_args: inference_calls.append("inference"),
                        validate_result=lambda _value: True,
                        now_utc=NOW,
                        cancel_requested=hostile_cancel,
                    )
            finally:
                # Keep the regression isolated even when testing a vulnerable
                # pre-fix implementation whose cleanup was redirected.
                model_call_module.DurableModelCallOrchestrator = (
                    ORIGINAL_ORCHESTRATOR_CLASS
                )

            self.assertEqual(DecoyOrchestrator.restore_calls, 0)
            self.assertIs(
                model_call_module.DurableModelCallOrchestrator,
                ORIGINAL_ORCHESTRATOR_CLASS,
            )
            self.assertEqual(inference_calls, [])
            self.assertEqual(
                budget.active_reservation(attempt_id),
                Decimal("1.2"),
            )
            self.assertEqual(
                [event["event_type"] for event in orchestrator._events(attempt_id)],
                ["ModelCallPrepared"],
            )


    def test_cancel_callback_cannot_redirect_journal_class_helper_through_module_alias(self):
        original_helper = model_call_module._model_journal_class_authority_changes
        hostile_helper_calls = []

        def hostile_helper(*, restore):
            hostile_helper_calls.append(restore)
            raise AssertionError("callback redirected journal-class recovery helper")

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            inference_calls = []

            def hostile_cancel():
                model_call_module._model_journal_class_authority_changes = hostile_helper
                return False

            try:
                with self.assertRaises(ModelCallError):
                    orchestrator.execute(
                        spec=call_spec,
                        policy=_policy(),
                        request=_request(orchestrator, call_spec),
                        descriptors=[_descriptor()],
                        call=lambda *_args: inference_calls.append("inference"),
                        validate_result=lambda _value: True,
                        now_utc=NOW,
                        cancel_requested=hostile_cancel,
                    )
            finally:
                model_call_module._model_journal_class_authority_changes = (
                    original_helper
                )

            self.assertEqual(hostile_helper_calls, [])
            self.assertIs(
                model_call_module._model_journal_class_authority_changes,
                original_helper,
            )
            self.assertEqual(inference_calls, [])
            self.assertEqual(
                budget.active_reservation(attempt_id),
                Decimal("1.2"),
            )
            self.assertEqual(
                [event["event_type"] for event in orchestrator._events(attempt_id)],
                ["ModelCallPrepared"],
            )


if __name__ == "__main__":
    unittest.main()
