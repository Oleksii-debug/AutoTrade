from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.runtime_target_host_chronology_bound_qualification import (
    AcceptedChronologyBoundRuntimeTargetHostQualification,
    RuntimeTargetHostChronologyBindingError,
    verify_chronology_bound_runtime_target_host_qualification,
)
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    AcceptedComposedRuntimeTargetHostQualification,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
)
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope
from mvp.autotrade_mvp.trusted_chronology_cut import TrustedChronologyCut
import mvp.autotrade_mvp.runtime_target_host_chronology_bound_qualification as bound


SOURCE_SHA = "a" * 40
STORE_DIGEST = "sha256:" + "b" * 64
RELEASE_ID = str(uuid5(NAMESPACE_URL, "wp65-chronology-release"))
RELEASE_SHA = "sha256:" + "c" * 64
ATTESTATION_ID = str(uuid5(NAMESPACE_URL, "wp65-chronology-attestation"))
EVIDENCE_ID = str(uuid5(NAMESPACE_URL, "wp65-chronology-evidence"))
CUT_ID = str(uuid5(NAMESPACE_URL, "wp65-chronology-cut"))
CHALLENGE_DIGEST = "sha256:" + "d" * 64
MEASUREMENT_ID = str(uuid5(NAMESPACE_URL, "wp65-time-measurement"))
ROOT_ID = "sha256:" + "e" * 64
POLICY_ID = "sha256:" + "f" * 64
ATTESTATION_DIGEST = "sha256:" + "1" * 64
QUALIFICATION_BINDING_DIGEST = "sha256:" + "2" * 64
TARGET_MEASUREMENT_DIGEST = "sha256:" + "3" * 64
FINANCIAL_BINDING_DIGEST = "sha256:" + "4" * 64


class RuntimeTargetHostChronologyBoundTests(unittest.TestCase):
    @staticmethod
    def _receipt() -> SignedQualificationAttestation:
        ref = EvidenceArtifactRef(
            artifact_id=EVIDENCE_ID,
            sha256="sha256:" + "5" * 64,
            media_type="application/json",
            evidence_kind="RUNTIME_TARGET_HOST_BINDING",
            source_sha=SOURCE_SHA,
        )
        attestation = QualificationAttestation(
            attestation_id=ATTESTATION_ID,
            source_sha=SOURCE_SHA,
            domain="PERFORMANCE",
            gate="RUNTIME_TARGET_HOST",
            package_id="WP-65",
            protocol_id="runtime-target-host-v1",
            protocol_version="1.0.0",
            requirement_ids=("target-host-pressure-budget",),
            evidence_refs=(ref,),
            producer_id="independent.qualifier",
            verifier_id="autotrade.qualifier",
            trust_root_id=ROOT_ID,
            runner_id="runner-1",
            harness_version="1.0.0",
            started_at="2026-10-03T13:59:00Z",
            completed_at="2026-10-03T14:00:01Z",
            signed_at="2026-10-03T14:00:02Z",
            result="PASS",
            release_artifact_id=RELEASE_ID,
            release_artifact_sha256=RELEASE_SHA,
        )
        return SignedQualificationAttestation(attestation=attestation, signature_b64="AA==")

    @staticmethod
    def _measurement(*, store_digest: str = STORE_DIGEST):
        return SimpleNamespace(
            source_sha=SOURCE_SHA,
            journal_store_identity_digest=store_digest,
            digest=TARGET_MEASUREMENT_DIGEST,
        )

    @staticmethod
    def _chronology(
        *,
        scope: ChronologyScope = ChronologyScope.RELEASE_RUNTIME,
        store_digest: str = STORE_DIGEST,
        source_sha: str = SOURCE_SHA,
        release_id: str | None = RELEASE_ID,
        release_sha: str | None = RELEASE_SHA,
    ) -> TrustedChronologyCut:
        runtime_bound = scope is ChronologyScope.RELEASE_RUNTIME
        return TrustedChronologyCut(
            cut_id=CUT_ID,
            challenge_digest=CHALLENGE_DIGEST,
            scope=scope,
            source_sha=source_sha,
            store_identity_digest=store_digest,
            owner_scope="PAPER:paper-account",
            owner_id="host-a",
            owner_epoch=1,
            clock_incident_generation=0,
            runtime_environment="PAPER",
            release_artifact_id=release_id if runtime_bound else None,
            release_artifact_sha256=release_sha if runtime_bound else None,
            runtime_host_id="host-a" if runtime_bound else None,
            runtime_occurrence_id=(
                str(uuid5(NAMESPACE_URL, "runtime-occurrence"))
                if runtime_bound
                else None
            ),
            runtime_occurrence_version=1 if runtime_bound else None,
            runtime_occurrence_journal_sequence=1 if runtime_bound else None,
            covered_utc="2026-10-03T14:00:02Z",
            utc_lower_bound="2026-10-03T14:00:02Z",
            utc_upper_bound="2026-10-03T14:00:03Z",
            external_authority_id="independent-time-authority",
            external_protocol_id="rfc3161-or-equivalent-v1",
            external_protocol_version="1.0.0",
            external_response_id="response-1",
            measurement_artifact_id=MEASUREMENT_ID,
            measurement_sha256="sha256:" + "6" * 64,
            measurement_requirement_id="trusted-chronology:" + "7" * 64,
            accepted_attestation_id=str(
                uuid5(NAMESPACE_URL, "chronology-attestation")
            ),
            accepted_attestation_digest=ATTESTATION_DIGEST,
            accepted_policy_id=POLICY_ID,
            accepted_trust_root_id=ROOT_ID,
            accepted_journal_sequence=2,
            cut_digest="sha256:" + "8" * 64,
        )

    @staticmethod
    def _qualification(*, store_digest: str = STORE_DIGEST):
        accepted = AcceptedRuntimeTargetHostQualification(
            attestation_id=ATTESTATION_ID,
            attestation_digest=ATTESTATION_DIGEST,
            source_sha=SOURCE_SHA,
            scenario_id="target-host-pressure",
            spec_digest="sha256:" + "9" * 64,
            configuration_hash="sha256:" + "a" * 64,
            host_fingerprint="sha256:" + "b" * 64,
            workload_profile_hash="sha256:" + "c" * 64,
            journal_store_identity_digest=store_digest,
            release_artifact_id=RELEASE_ID,
            release_artifact_sha256=RELEASE_SHA,
            binding_artifact_id=str(uuid5(NAMESPACE_URL, "binding-artifact")),
            binding_sha256=QUALIFICATION_BINDING_DIGEST,
            evidence_sha256_by_kind={},
            payload_artifact_id_by_kind={},
            payload_sha256_by_kind={},
            collector_by_kind={},
        )
        return AcceptedComposedRuntimeTargetHostQualification(
            qualification=accepted,
            target_host_measurement_digest=TARGET_MEASUREMENT_DIGEST,
            durable_financial_binding_digest=FINANCIAL_BINDING_DIGEST,
            projection_sha256_by_kind={},
        )

    def _verify(self, chronology, *, qualification=None, second_current=None):
        measurement = self._measurement()
        if qualification is None:
            qualification = self._qualification()
        if second_current is None:
            second_current = chronology
        current = Mock(side_effect=[chronology, second_current])
        horizon = Mock()
        plan_verify = Mock(return_value=qualification)
        with (
            patch.object(bound, "snapshot_target_host_measurement", return_value=measurement),
            patch.object(bound, "require_current_trusted_chronology_cut", current),
            patch.object(bound, "require_chronology_horizon", horizon),
            patch.object(
                bound,
                "verify_declared_plan_runtime_target_host_qualification",
                plan_verify,
            ),
        ):
            result = verify_chronology_bound_runtime_target_host_qualification(
                self._receipt(),
                evidence_store=object(),
                evidence_root="evidence-root",
                journal_store=object(),
                recovery=object(),
                runtime=object(),
                chronology_cut=chronology,
                plan_id="plan-1",
                spec=object(),
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=object(),
                campaign_cut=object(),
                measurement=object(),
            )
        return result, current, horizon, plan_verify

    def test_requires_release_runtime_scope_before_terminal_verifier(self):
        chronology = self._chronology(scope=ChronologyScope.SOURCE_QUALIFICATION)
        plan_verify = Mock()
        with (
            patch.object(
                bound,
                "snapshot_target_host_measurement",
                return_value=self._measurement(),
            ),
            patch.object(
                bound,
                "verify_declared_plan_runtime_target_host_qualification",
                plan_verify,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeTargetHostChronologyBindingError,
                "RELEASE_RUNTIME",
            ):
                verify_chronology_bound_runtime_target_host_qualification(
                    self._receipt(),
                    evidence_store=object(),
                    evidence_root="evidence-root",
                    journal_store=object(),
                    recovery=object(),
                    runtime=object(),
                    chronology_cut=chronology,
                    plan_id="plan-1",
                    spec=object(),
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                    campaign_plan=object(),
                    campaign_cut=object(),
                    measurement=object(),
                )
        plan_verify.assert_not_called()

    def test_brackets_wp65_verification_and_covers_signed_instants(self):
        chronology = self._chronology()
        result, current, horizon, plan_verify = self._verify(chronology)

        self.assertIsInstance(
            result,
            AcceptedChronologyBoundRuntimeTargetHostQualification,
        )
        self.assertEqual(result.chronology_cut, chronology)
        self.assertEqual(current.call_count, 2)
        self.assertEqual(plan_verify.call_count, 1)
        self.assertEqual(
            horizon.call_args_list,
            [
                call(
                    chronology,
                    "2026-10-03T14:00:01Z",
                    "2026-10-03T14:00:02Z",
                ),
                call(
                    chronology,
                    "2026-10-03T14:00:01Z",
                    "2026-10-03T14:00:02Z",
                ),
            ],
        )
        first_kwargs = current.call_args_list[0].kwargs
        second_kwargs = current.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["expected_scope"], ChronologyScope.RELEASE_RUNTIME)
        self.assertEqual(second_kwargs["expected_scope"], ChronologyScope.RELEASE_RUNTIME)
        self.assertEqual(first_kwargs["expected_source_sha"], SOURCE_SHA)
        self.assertEqual(first_kwargs["expected_release_artifact_id"], RELEASE_ID)
        self.assertEqual(first_kwargs["expected_release_artifact_sha256"], RELEASE_SHA)
        self.assertEqual(second_kwargs["cut"], chronology)

    def test_prebinding_rejects_foreign_journal_generation_before_wp65_verifier(self):
        chronology = self._chronology(store_digest="sha256:" + "0" * 64)
        current = Mock(return_value=chronology)
        plan_verify = Mock()
        with (
            patch.object(
                bound,
                "snapshot_target_host_measurement",
                return_value=self._measurement(),
            ),
            patch.object(bound, "require_current_trusted_chronology_cut", current),
            patch.object(
                bound,
                "verify_declared_plan_runtime_target_host_qualification",
                plan_verify,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeTargetHostChronologyBindingError,
                "JournalStore identity",
            ):
                verify_chronology_bound_runtime_target_host_qualification(
                    self._receipt(),
                    evidence_store=object(),
                    evidence_root="evidence-root",
                    journal_store=object(),
                    recovery=object(),
                    runtime=object(),
                    chronology_cut=chronology,
                    plan_id="plan-1",
                    spec=object(),
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                    campaign_plan=object(),
                    campaign_cut=object(),
                    measurement=object(),
                )
        plan_verify.assert_not_called()

    def test_signed_wp65_acceptance_cannot_splice_other_journal_generation(self):
        chronology = self._chronology()
        bad = self._qualification(store_digest="sha256:" + "0" * 64)
        with self.assertRaisesRegex(
            RuntimeTargetHostChronologyBindingError,
            "signed WP-65 JournalStore identity",
        ):
            self._verify(chronology, qualification=bad)

    def test_runtime_or_recovery_change_during_wp65_verification_fails_closed(self):
        chronology = self._chronology()
        measurement = self._measurement()
        current = Mock(side_effect=[chronology, PermissionError("runtime occurrence changed")])
        with (
            patch.object(bound, "snapshot_target_host_measurement", return_value=measurement),
            patch.object(bound, "require_current_trusted_chronology_cut", current),
            patch.object(bound, "require_chronology_horizon"),
            patch.object(
                bound,
                "verify_declared_plan_runtime_target_host_qualification",
                return_value=self._qualification(),
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "runtime occurrence changed"):
                verify_chronology_bound_runtime_target_host_qualification(
                    self._receipt(),
                    evidence_store=object(),
                    evidence_root="evidence-root",
                    journal_store=object(),
                    recovery=object(),
                    runtime=object(),
                    chronology_cut=chronology,
                    plan_id="plan-1",
                    spec=object(),
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                    campaign_plan=object(),
                    campaign_cut=object(),
                    measurement=object(),
                )
        self.assertEqual(current.call_count, 2)

    def test_second_chronology_value_must_be_identical_to_first_authority(self):
        chronology = self._chronology()
        changed = self._chronology()
        object.__setattr__(changed, "cut_digest", "sha256:" + "0" * 64)
        with self.assertRaisesRegex(
            RuntimeTargetHostChronologyBindingError,
            "changed during terminal WP-65 verification",
        ):
            self._verify(chronology, second_current=changed)

    def test_verifier_mutation_cannot_rewrite_captured_horizon_or_measurement_identity(self):
        chronology = self._chronology()
        authority_measurement = self._measurement()
        verifier_measurement = self._measurement()
        horizon = Mock()
        current = Mock(side_effect=[chronology, chronology])

        def mutating_verifier(receipt, **kwargs):
            object.__setattr__(
                receipt.attestation,
                "signed_at",
                "2026-10-03T15:00:00Z",
            )
            kwargs["measurement"].source_sha = "f" * 40
            kwargs["measurement"].journal_store_identity_digest = "sha256:" + "0" * 64
            kwargs["measurement"].digest = "sha256:" + "0" * 64
            return self._qualification()

        with (
            patch.object(
                bound,
                "snapshot_target_host_measurement",
                side_effect=[authority_measurement, verifier_measurement],
            ),
            patch.object(bound, "require_current_trusted_chronology_cut", current),
            patch.object(bound, "require_chronology_horizon", horizon),
            patch.object(
                bound,
                "verify_declared_plan_runtime_target_host_qualification",
                side_effect=mutating_verifier,
            ),
        ):
            result = verify_chronology_bound_runtime_target_host_qualification(
                self._receipt(),
                evidence_store=object(),
                evidence_root="evidence-root",
                journal_store=object(),
                recovery=object(),
                runtime=object(),
                chronology_cut=chronology,
                plan_id="plan-1",
                spec=object(),
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=object(),
                campaign_cut=object(),
                measurement=object(),
            )

        self.assertIsInstance(result, AcceptedChronologyBoundRuntimeTargetHostQualification)
        self.assertEqual(
            horizon.call_args_list,
            [
                call(
                    chronology,
                    "2026-10-03T14:00:01Z",
                    "2026-10-03T14:00:02Z",
                ),
                call(
                    chronology,
                    "2026-10-03T14:00:01Z",
                    "2026-10-03T14:00:02Z",
                ),
            ],
        )
        self.assertEqual(current.call_args_list[1].kwargs["expected_source_sha"], SOURCE_SHA)


if __name__ == "__main__":
    unittest.main()
