from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mvp.autotrade_mvp import performance_qualification as budget_module
from mvp.autotrade_mvp import runtime_target_host_campaign as campaign_module
from mvp.autotrade_mvp import runtime_target_host_composed_authority as composed_authority
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import CAMPAIGN_EVIDENCE_KIND
from mvp.tests.test_runtime_target_host_plan_bound_qualification import _spec


class RuntimeTargetHostSignedCampaignAuthorityTests(unittest.TestCase):
    def test_parser_guard_rejects_class_property_rebinding(self) -> None:
        parser_type = campaign_module.ParsedRuntimeTargetHostCampaign
        original = parser_type.__dict__["canonical_bytes"]
        setattr(parser_type, "canonical_bytes", property(lambda _self: b"forged"))
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "signed campaign parser sealed class member changed: canonical_bytes",
            ):
                composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD()
        finally:
            setattr(parser_type, "canonical_bytes", original)

        composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD()

    def test_parser_guard_rejects_parse_dependency_rebinding(self) -> None:
        original = campaign_module._parse_json
        campaign_module._parse_json = lambda _raw: {}
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "signed campaign parser.parse sealed dependency changed: _parse_json",
            ):
                composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD()
        finally:
            campaign_module._parse_json = original

        composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD()

    def test_parser_guard_rejects_added_constructor_override(self) -> None:
        parser_type = campaign_module.ParsedRuntimeTargetHostCampaign
        self.assertNotIn("__new__", parser_type.__dict__)
        setattr(parser_type, "__new__", staticmethod(lambda cls, *args, **kwargs: object.__new__(cls)))
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "signed campaign parser sealed class namespace key set changed",
            ):
                composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD()
        finally:
            delattr(parser_type, "__new__")

        composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD()

    def test_budget_guard_rejects_evaluator_dependency_rebinding(self) -> None:
        original = budget_module._snapshot_runtime_budget_spec
        budget_module._snapshot_runtime_budget_spec = lambda value: value
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "runtime budget evaluator sealed dependency changed: _snapshot_runtime_budget_spec",
            ):
                composed_authority._PRODUCTION_BUDGET_EVALUATOR_GUARD()
        finally:
            budget_module._snapshot_runtime_budget_spec = original

        composed_authority._PRODUCTION_BUDGET_EVALUATOR_GUARD()

    @staticmethod
    def _matching_inputs(raw: bytes):
        digest = "sha256:" + sha256(raw).hexdigest()
        accepted = SimpleNamespace(
            payload_artifact_id_by_kind={CAMPAIGN_EVIDENCE_KIND: "campaign-artifact"},
            payload_sha256_by_kind={CAMPAIGN_EVIDENCE_KIND: digest},
        )
        observation = SimpleNamespace(
            expected_financial_events=0,
            recovered_financial_events=0,
            financial_latency_us=(),
            financial_staleness_us=(),
            research_interference_us=(),
            declared_duration_us=1_000,
            observed_duration_us=1_000,
        )
        evidence = SimpleNamespace(
            observation=observation,
            journal_sequence_before=0,
            journal_sequence_after=0,
            recovered_event_ids=(),
            recovered_journal_sequences=(),
        )
        measurement = SimpleNamespace(
            financial_samples=(),
            financial_latency_us=(),
            financial_staleness_us=(),
            research_interference_us=(),
            start_journal_sequence=0,
            end_journal_sequence=0,
        )
        campaign_plan = SimpleNamespace(
            expected_financial_event_ids=(),
            declared_duration_ms=1,
        )
        return accepted, evidence, measurement, campaign_plan

    def test_matcher_calls_parser_and_budget_guards_around_execution(self) -> None:
        raw = b"signed-campaign-raw"
        accepted, evidence, measurement, campaign_plan = self._matching_inputs(raw)
        parser_guard = Mock()
        budget_guard = Mock()

        class ParsedCampaign:
            @classmethod
            def parse(cls, _raw):
                return SimpleNamespace(evidence=evidence)

        matcher = composed_authority._build_signed_campaign_matcher(
            authenticated_reader_factory=lambda *_args, **_kwargs: (
                lambda _artifact_id: (object(), raw)
            ),
            artifact_integrity_error_type=RuntimeError,
            parsed_campaign_type=ParsedCampaign,
            campaign_error_type=ValueError,
            budget_spec_type=type(_spec()),
            budget_evaluator=lambda _spec_value, _observation: SimpleNamespace(
                status="PASS",
                reasons=(),
            ),
            budget_error_type=ValueError,
            composition_error_type=RuntimeTargetHostCompositionError,
            campaign_evidence_kind=CAMPAIGN_EVIDENCE_KIND,
            sha256_factory=sha256,
            parser_dependency_guard=parser_guard,
            budget_dependency_guard=budget_guard,
        )

        decision = matcher(
            accepted,
            evidence_store=object(),
            evidence_root="evidence-root",
            measurement=measurement,
            campaign_plan=campaign_plan,
            spec=_spec(),
        )

        self.assertEqual(decision.status, "PASS")
        self.assertEqual(parser_guard.call_count, 3)
        self.assertEqual(budget_guard.call_count, 2)

    def test_parser_callback_mutation_is_rejected_before_result_use(self) -> None:
        raw = b"signed-campaign-raw"
        accepted, _evidence, measurement, campaign_plan = self._matching_inputs(raw)
        parser_type = campaign_module.ParsedRuntimeTargetHostCampaign
        original = parser_type.__dict__["canonical_bytes"]

        class MutatingParser:
            @classmethod
            def parse(cls, _raw):
                setattr(
                    parser_type,
                    "canonical_bytes",
                    property(lambda _self: b"forged"),
                )
                return object()

        matcher = composed_authority._build_signed_campaign_matcher(
            authenticated_reader_factory=lambda *_args, **_kwargs: (
                lambda _artifact_id: (object(), raw)
            ),
            artifact_integrity_error_type=RuntimeError,
            parsed_campaign_type=MutatingParser,
            campaign_error_type=ValueError,
            budget_spec_type=type(_spec()),
            budget_evaluator=lambda *_args: object(),
            budget_error_type=ValueError,
            composition_error_type=RuntimeTargetHostCompositionError,
            campaign_evidence_kind=CAMPAIGN_EVIDENCE_KIND,
            sha256_factory=sha256,
            parser_dependency_guard=(
                composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD
            ),
        )
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "signed campaign parser sealed class member changed: canonical_bytes",
            ):
                matcher(
                    accepted,
                    evidence_store=object(),
                    evidence_root="evidence-root",
                    measurement=measurement,
                    campaign_plan=campaign_plan,
                    spec=None,
                )
        finally:
            setattr(parser_type, "canonical_bytes", original)

        composed_authority._PRODUCTION_CAMPAIGN_PARSER_GUARD()

    def test_budget_callback_mutation_is_rejected_before_decision_use(self) -> None:
        raw = b"signed-campaign-raw"
        accepted, evidence, measurement, campaign_plan = self._matching_inputs(raw)
        original = budget_module._snapshot_runtime_budget_spec

        class ParsedCampaign:
            @classmethod
            def parse(cls, _raw):
                return SimpleNamespace(evidence=evidence)

        def mutating_budget(_spec_value, _observation):
            budget_module._snapshot_runtime_budget_spec = lambda value: value
            return SimpleNamespace(status="PASS", reasons=())

        matcher = composed_authority._build_signed_campaign_matcher(
            authenticated_reader_factory=lambda *_args, **_kwargs: (
                lambda _artifact_id: (object(), raw)
            ),
            artifact_integrity_error_type=RuntimeError,
            parsed_campaign_type=ParsedCampaign,
            campaign_error_type=ValueError,
            budget_spec_type=type(_spec()),
            budget_evaluator=mutating_budget,
            budget_error_type=ValueError,
            composition_error_type=RuntimeTargetHostCompositionError,
            campaign_evidence_kind=CAMPAIGN_EVIDENCE_KIND,
            sha256_factory=sha256,
            budget_dependency_guard=(
                composed_authority._PRODUCTION_BUDGET_EVALUATOR_GUARD
            ),
        )
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "runtime budget evaluator sealed dependency changed: _snapshot_runtime_budget_spec",
            ):
                matcher(
                    accepted,
                    evidence_store=object(),
                    evidence_root="evidence-root",
                    measurement=measurement,
                    campaign_plan=campaign_plan,
                    spec=_spec(),
                )
        finally:
            budget_module._snapshot_runtime_budget_spec = original

        composed_authority._PRODUCTION_BUDGET_EVALUATOR_GUARD()


if __name__ == "__main__":
    unittest.main()
