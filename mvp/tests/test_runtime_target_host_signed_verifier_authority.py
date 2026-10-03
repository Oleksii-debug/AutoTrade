from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mvp.autotrade_mvp import qualification_attestation as qualification_module
from mvp.autotrade_mvp import runtime_target_host_composed_authority as composed_authority
from mvp.autotrade_mvp import runtime_target_host_qualification as signed_module
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)


class RuntimeTargetHostSignedVerifierAuthorityTests(unittest.TestCase):
    @staticmethod
    def _measurement():
        return SimpleNamespace(
            digest="sha256:" + "1" * 64,
            source_sha="a" * 40,
            scenario_id="signed-graph-scenario",
            spec_digest="sha256:" + "2" * 64,
            configuration_hash="sha256:" + "3" * 64,
            host_fingerprint="sha256:" + "4" * 64,
            workload_profile_hash="sha256:" + "5" * 64,
            journal_store_identity_digest="sha256:" + "6" * 64,
        )

    @classmethod
    def _build(
        cls,
        *,
        signed_verifier,
        signed_dependency_guard,
        signed_external_dependency_guard=None,
        signed_campaign_matcher=None,
    ):
        measurement = cls._measurement()
        if signed_campaign_matcher is None:
            signed_campaign_matcher = lambda *_args, **_kwargs: None

        def durable_binder(**_kwargs):
            return SimpleNamespace(
                target_host_measurement_digest=measurement.digest,
                source_sha=measurement.source_sha,
                spec_digest=measurement.spec_digest,
                declared_plan_digest=measurement.workload_profile_hash,
                digest="sha256:" + "7" * 64,
            )

        verifier = composed_authority._build_composed_production_verifier(
            measurement_snapshotter=lambda value: value,
            spec_snapshotter=lambda value: value,
            campaign_plan_snapshotter=lambda value: value,
            campaign_cut_type=type(None),
            campaign_cut_snapshotter=lambda value: value,
            durable_binder=durable_binder,
            composition_error_type=RuntimeTargetHostCompositionError,
            durable_plan_matcher=lambda *_args: None,
            signed_verifier=signed_verifier,
            accepted_type=object,
            signed_campaign_matcher=signed_campaign_matcher,
            projection_digest_builder=lambda _measurement: {},
            composed_type=lambda **kwargs: kwargs,
            signed_dependency_guard=signed_dependency_guard,
            signed_external_dependency_guard=signed_external_dependency_guard,
        )
        return verifier, measurement

    @staticmethod
    def _invoke(verifier, measurement):
        return verifier(
            object(),
            evidence_store=object(),
            evidence_root="evidence-root",
            journal_store=object(),
            spec=object(),
            campaign_plan=object(),
            campaign_cut=object(),
            declared_plan_id="plan-id",
            measurement=measurement,
            expected_release_artifact_id="release-id",
            expected_release_artifact_sha256="release-digest",
        )

    def test_pre_call_guard_rejects_same_module_dependency_rebinding(self) -> None:
        signed_verifier = Mock(return_value=object())
        verifier, measurement = self._build(
            signed_verifier=signed_verifier,
            signed_dependency_guard=(
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD
            ),
        )
        original = signed_module._identity_tuple
        signed_module._identity_tuple = lambda **_kwargs: ("forged",)
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "signed target-host verifier sealed dependency changed: _identity_tuple",
            ):
                self._invoke(verifier, measurement)
        finally:
            signed_module._identity_tuple = original

        signed_verifier.assert_not_called()
        composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD()

    def test_post_call_guard_rejects_callback_time_dependency_rebinding(self) -> None:
        original = signed_module._identity_tuple

        def mutating_signed_verifier(*_args, **_kwargs):
            signed_module._identity_tuple = lambda **_values: ("forged",)
            return object()

        verifier, measurement = self._build(
            signed_verifier=mutating_signed_verifier,
            signed_dependency_guard=(
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD
            ),
        )
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "signed target-host verifier sealed dependency changed: _identity_tuple",
            ):
                self._invoke(verifier, measurement)
        finally:
            signed_module._identity_tuple = original

        composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD()

    def test_return_boundary_guard_rejects_campaign_callback_rebinding(self) -> None:
        original = signed_module._identity_tuple

        def mutating_campaign_matcher(*_args, **_kwargs):
            signed_module._identity_tuple = lambda **_values: ("forged",)

        verifier, measurement = self._build(
            signed_verifier=Mock(return_value=object()),
            signed_dependency_guard=(
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD
            ),
            signed_campaign_matcher=mutating_campaign_matcher,
        )
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "signed target-host verifier sealed dependency changed: _identity_tuple",
            ):
                self._invoke(verifier, measurement)
        finally:
            signed_module._identity_tuple = original

        composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD()

    def test_external_guard_runs_at_all_signed_authority_boundaries(self) -> None:
        external_guard = Mock()
        verifier, measurement = self._build(
            signed_verifier=Mock(return_value=object()),
            signed_dependency_guard=Mock(),
            signed_external_dependency_guard=external_guard,
        )

        self._invoke(verifier, measurement)

        self.assertEqual(external_guard.call_count, 3)

    def test_canonical_qualification_guard_rejects_transitive_rebinding(self) -> None:
        signed_verifier = Mock(return_value=object())
        verifier, measurement = self._build(
            signed_verifier=signed_verifier,
            signed_dependency_guard=(
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD
            ),
            signed_external_dependency_guard=(
                composed_authority._PRODUCTION_CANONICAL_QUALIFICATION_GUARD
            ),
        )
        original = qualification_module._qualification_trust_policy_id_exact
        qualification_module._qualification_trust_policy_id_exact = lambda _policy: (
            "sha256:" + "0" * 64
        )
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "canonical qualification verifier sealed dependency changed: _qualification_trust_policy_id_exact",
            ):
                self._invoke(verifier, measurement)
        finally:
            qualification_module._qualification_trust_policy_id_exact = original

        signed_verifier.assert_not_called()
        composed_authority._PRODUCTION_CANONICAL_QUALIFICATION_GUARD()

    def test_canonical_guard_rechecks_after_signed_callback(self) -> None:
        original = qualification_module._qualification_trust_policy_id_exact

        def mutating_signed_verifier(*_args, **_kwargs):
            qualification_module._qualification_trust_policy_id_exact = (
                lambda _policy: "sha256:" + "0" * 64
            )
            return object()

        verifier, measurement = self._build(
            signed_verifier=mutating_signed_verifier,
            signed_dependency_guard=(
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD
            ),
            signed_external_dependency_guard=(
                composed_authority._PRODUCTION_CANONICAL_QUALIFICATION_GUARD
            ),
        )
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "canonical qualification verifier sealed dependency changed: _qualification_trust_policy_id_exact",
            ):
                self._invoke(verifier, measurement)
        finally:
            qualification_module._qualification_trust_policy_id_exact = original

        composed_authority._PRODUCTION_CANONICAL_QUALIFICATION_GUARD()

    def test_canonical_guard_rechecks_at_composed_return_boundary(self) -> None:
        original = qualification_module._qualification_trust_policy_id_exact

        def mutating_campaign_matcher(*_args, **_kwargs):
            qualification_module._qualification_trust_policy_id_exact = (
                lambda _policy: "sha256:" + "0" * 64
            )

        verifier, measurement = self._build(
            signed_verifier=Mock(return_value=object()),
            signed_dependency_guard=(
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD
            ),
            signed_external_dependency_guard=(
                composed_authority._PRODUCTION_CANONICAL_QUALIFICATION_GUARD
            ),
            signed_campaign_matcher=mutating_campaign_matcher,
        )
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "canonical qualification verifier sealed dependency changed: _qualification_trust_policy_id_exact",
            ):
                self._invoke(verifier, measurement)
        finally:
            qualification_module._qualification_trust_policy_id_exact = original

        composed_authority._PRODUCTION_CANONICAL_QUALIFICATION_GUARD()

    def test_stable_signed_graph_reaches_composed_result(self) -> None:
        accepted = object()
        signed_verifier = Mock(return_value=accepted)
        verifier, measurement = self._build(
            signed_verifier=signed_verifier,
            signed_dependency_guard=(
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD
            ),
            signed_external_dependency_guard=(
                composed_authority._PRODUCTION_CANONICAL_QUALIFICATION_GUARD
            ),
        )

        result = self._invoke(verifier, measurement)

        self.assertIs(result["qualification"], accepted)
        self.assertEqual(
            result["target_host_measurement_digest"],
            measurement.digest,
        )
        signed_verifier.assert_called_once()


if __name__ == "__main__":
    unittest.main()
