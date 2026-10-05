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
ORIGINAL_BUDGET_CLASS = DurableModelBudget


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
    def test_cancel_callback_cannot_poison_model_budget_module_alias(self):
        class DecoyBudget:
            pass

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            inference_calls = []

            def hostile_cancel():
                model_call_module.DurableModelBudget = DecoyBudget
                return False

            try:
                with self.assertRaisesRegex(
                    ModelCallError,
                    r"cancellation probe mutated orchestrator authority:.*"
                    r"module\.DurableModelBudget",
                ):
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
                model_call_module.DurableModelBudget = ORIGINAL_BUDGET_CLASS

            self.assertIs(
                model_call_module.DurableModelBudget,
                ORIGINAL_BUDGET_CLASS,
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


    def test_cancel_callback_cannot_redirect_exact_journal_guard_through_module_alias(self):
        original_guard = model_call_module.require_exact_journal_store_authority
        hostile_guard_calls = []

        def hostile_guard(*_args, **_kwargs):
            hostile_guard_calls.append("called")
            raise AssertionError("callback redirected exact journal authority guard")

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)
            inference_calls = []

            def hostile_cancel():
                model_call_module.require_exact_journal_store_authority = hostile_guard
                return False

            try:
                with self.assertRaisesRegex(
                    ModelCallError,
                    r"cancellation probe mutated orchestrator authority:.*"
                    r"module\.require_exact_journal_store_authority",
                ):
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
                model_call_module.require_exact_journal_store_authority = (
                    original_guard
                )

            self.assertEqual(hostile_guard_calls, [])
            self.assertIs(
                model_call_module.require_exact_journal_store_authority,
                original_guard,
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


    def test_model_call_clock_cannot_redirect_utc_normalizer(self):
        original_utc_text = model_call_module._utc_text
        forged_calls = []
        inference_calls = []

        def forged_utc_text(*_args, **_kwargs):
            forged_calls.append("called")
            raise AssertionError("clock redirected UTC normalizer")

        def hostile_clock():
            model_call_module._utc_text = forged_utc_text
            return NOW_TEXT

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = ORIGINAL_ORCHESTRATOR_CLASS(
                budget=budget,
                clock=hostile_clock,
                pricing_evidence_resolver=_pricing,
                observation_evidence_resolver=lambda *_args: None,
                billing_evidence_resolver=lambda *_args: None,
            )
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)

            try:
                with self.assertRaisesRegex(
                    ModelCallError,
                    r"clock mutated orchestrator authority:.*module\._utc_text",
                ):
                    orchestrator.execute(
                        spec=call_spec,
                        policy=_policy(),
                        request=_request(orchestrator, call_spec),
                        descriptors=[_descriptor()],
                        call=lambda *_args: inference_calls.append("inference"),
                        validate_result=lambda _value: True,
                        now_utc=NOW,
                    )
            finally:
                model_call_module._utc_text = original_utc_text

            self.assertEqual(forged_calls, [])
            self.assertIs(model_call_module._utc_text, original_utc_text)
            self.assertEqual(inference_calls, [])
            self.assertEqual(
                budget.active_reservation(attempt_id),
                Decimal("1.2"),
            )
            self.assertEqual(orchestrator._events(attempt_id), [])


    def test_cancel_callback_cannot_redirect_dataclass_fields_module_alias(self):
        original_fields = model_call_module.fields
        forged_calls = []
        inference_calls = []

        def forged_fields(*_args, **_kwargs):
            forged_calls.append("called")
            raise AssertionError("callback redirected dataclass fields authority")

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)

            def hostile_cancel():
                model_call_module.fields = forged_fields
                return False

            try:
                with self.assertRaisesRegex(
                    ModelCallError,
                    r"cancellation probe mutated orchestrator authority:.*module\.fields",
                ):
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
                model_call_module.fields = original_fields

            self.assertEqual(forged_calls, [])
            self.assertIs(model_call_module.fields, original_fields)
            self.assertEqual(inference_calls, [])
            self.assertEqual(
                budget.active_reservation(attempt_id),
                Decimal("1.2"),
            )
            self.assertEqual(
                [event["event_type"] for event in orchestrator._events(attempt_id)],
                ["ModelCallPrepared"],
            )


    def test_cancel_callback_cannot_redirect_json_load_authority(self):
        original_json = model_call_module.json
        original_loads = original_json.loads
        forged_calls = []
        inference_calls = []

        def forged_loads(*_args, **_kwargs):
            forged_calls.append("called")
            raise AssertionError("callback redirected JSON load authority")

        with TemporaryDirectory() as directory:
            _journal, budget = _open_budget(directory)
            orchestrator = _orchestrator(budget)
            call_spec = _spec()
            attempt_id = orchestrator.attempt_id(call_spec)

            def hostile_cancel():
                original_json.loads = forged_loads
                model_call_module.json = object()
                return False

            try:
                with self.assertRaisesRegex(
                    ModelCallError,
                    r"cancellation probe mutated orchestrator authority:.*module\.json",
                ):
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
                original_json.loads = original_loads
                model_call_module.json = original_json

            self.assertEqual(forged_calls, [])
            self.assertIs(model_call_module.json, original_json)
            self.assertIs(original_json.loads, original_loads)
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
