from __future__ import annotations

from inspect import getclosurevars
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as plan_bound
from mvp.autotrade_mvp import runtime_target_host_composed_qualification as composed
from mvp.autotrade_mvp import runtime_target_host_durable_financial as durable
from mvp.autotrade_mvp import runtime_target_host_qualification as signed
from mvp.autotrade_mvp import performance_qualification as performance
from mvp.autotrade_mvp.runtime_target_host_composed_authority import (
    _PRODUCTION_PROJECTION_DIGEST_BUILDER,
    _PRODUCTION_SIGNED_CAMPAIGN_MATCHER,
    _build_composed_production_verifier,
    verify_sealed_composed_runtime_target_host_qualification,
)
from mvp.tests.test_runtime_target_host_composed_qualification import (
    RELEASE_ID,
    RELEASE_SHA,
    accepted_for,
    budget_spec,
    durable_binding_for,
    measurement,
    runtime_campaign_plan,
)


class RuntimeTargetHostComposedAuthorityTests(unittest.TestCase):
    def test_factory_uses_captured_binder_and_signed_verifier(self) -> None:
        current = measurement()
        current_spec = budget_spec()
        current_plan = runtime_campaign_plan(current_spec)
        accepted = accepted_for(current)
        binder = Mock(return_value=durable_binding_for(current))
        signed_verifier = Mock(return_value=accepted)
        campaign_match = Mock()
        workload_match = Mock()

        verifier = _build_composed_production_verifier(
            measurement_snapshotter=lambda value: value,
            spec_snapshotter=lambda value: value,
            campaign_plan_snapshotter=lambda value: value,
            campaign_cut_type=type(None),
            campaign_cut_snapshotter=lambda value: value,
            durable_binder=binder,
            composition_error_type=composed.RuntimeTargetHostCompositionError,
            durable_plan_matcher=workload_match,
            signed_verifier=signed_verifier,
            accepted_type=signed.AcceptedRuntimeTargetHostQualification,
            signed_campaign_matcher=campaign_match,
            projection_digest_builder=composed.target_host_measurement_projection_digests,
            composed_type=composed.AcceptedComposedRuntimeTargetHostQualification,
        )

        result = verifier(
            object(),
            evidence_store=object(),
            evidence_root="unused",
            journal_store=object(),
            spec=current_spec,
            campaign_plan=current_plan,
            campaign_cut=object(),
            declared_plan_id="plan-1",
            measurement=current,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
        )

        self.assertIs(result.qualification, accepted)
        self.assertEqual(result.target_host_measurement_digest, current.digest)
        binder.assert_called_once()
        signed_verifier.assert_called_once()
        workload_match.assert_called_once()
        campaign_match.assert_called_once()

    def test_production_adapter_retains_original_direct_authorities_after_rebind(self) -> None:
        original_binder = durable.bind_release_bound_durable_financial_latency_to_target_host_measurement
        original_signed = signed.verify_runtime_target_host_qualification
        before = getclosurevars(
            verify_sealed_composed_runtime_target_host_qualification
        ).nonlocals
        self.assertIs(before["durable_binder"], original_binder)
        self.assertIs(before["signed_verifier"], original_signed)

        forged_binder = Mock()
        forged_signed = Mock()
        with (
            patch.object(
                durable,
                "bind_release_bound_durable_financial_latency_to_target_host_measurement",
                forged_binder,
            ),
            patch.object(
                signed,
                "verify_runtime_target_host_qualification",
                forged_signed,
            ),
            patch.object(
                composed,
                "bind_release_bound_durable_financial_latency_to_target_host_measurement",
                forged_binder,
            ),
            patch.object(
                composed,
                "verify_runtime_target_host_qualification",
                forged_signed,
            ),
        ):
            after = getclosurevars(
                verify_sealed_composed_runtime_target_host_qualification
            ).nonlocals
            self.assertIs(after["durable_binder"], original_binder)
            self.assertIs(after["signed_verifier"], original_signed)

        forged_binder.assert_not_called()
        forged_signed.assert_not_called()

    def test_signed_campaign_matcher_retains_canonical_budget_evaluator(self) -> None:
        before = getclosurevars(_PRODUCTION_SIGNED_CAMPAIGN_MATCHER).nonlocals
        original = performance.evaluate_runtime_budget
        self.assertIs(before["budget_evaluator"], original)

        forged = Mock()
        with patch.object(performance, "evaluate_runtime_budget", forged), patch.object(
            composed,
            "evaluate_runtime_budget",
            forged,
        ):
            after = getclosurevars(_PRODUCTION_SIGNED_CAMPAIGN_MATCHER).nonlocals
            self.assertIs(after["budget_evaluator"], original)
        forged.assert_not_called()

    def test_sealed_projection_digests_match_canonical_public_projection(self) -> None:
        current = measurement()
        expected = composed.target_host_measurement_projection_digests(current)
        observed = _PRODUCTION_PROJECTION_DIGEST_BUILDER(current)
        self.assertEqual(dict(observed), dict(expected))
        with self.assertRaises(TypeError):
            observed["forged"] = "sha256:" + "0" * 64

    def test_plan_bound_production_closure_captures_sealed_composed_adapter(self) -> None:
        private = (
            plan_bound._verify_declared_plan_runtime_target_host_qualification_without_chronology
        )
        captured = getclosurevars(private).nonlocals
        self.assertIs(
            captured["composed_verifier"],
            verify_sealed_composed_runtime_target_host_qualification,
        )

        forged_public = Mock()
        with patch.object(
            plan_bound,
            "verify_composed_runtime_target_host_qualification",
            forged_public,
        ):
            captured_after = getclosurevars(private).nonlocals
            self.assertIs(
                captured_after["composed_verifier"],
                verify_sealed_composed_runtime_target_host_qualification,
            )
        forged_public.assert_not_called()


if __name__ == "__main__":
    unittest.main()
