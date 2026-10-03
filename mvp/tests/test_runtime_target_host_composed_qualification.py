from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
    _read_accepted_raw_payload,
    _require_signed_campaign_match,
    target_host_measurement_projection_bytes,
    target_host_measurement_projection_digests,
    verify_composed_runtime_target_host_qualification,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResearchInterferenceSample,
    ResourceTargetHostSample,
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
    CAMPAIGN_EVIDENCE_KIND,
    HOST_INVENTORY_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
)


SOURCE = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
WORKLOAD = "sha256:" + "d" * 64
PLAN = "sha256:" + "e" * 64
TAXONOMY = "sha256:" + "f" * 64
JOURNAL = "sha256:" + "1" * 64
RELEASE_ID = "50000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "2" * 64
DURABLE = "sha256:" + "4" * 64
CAMPAIGN = "sha256:" + "5" * 64
OTHER = "sha256:" + "9" * 64


def budget_spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-composed",
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500_000,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


SPEC = budget_spec().digest


def measurement() -> TargetHostMeasurementArtifact:
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id="wp65-composed",
        spec_digest=SPEC,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=WORKLOAD,
        plan_digest=PLAN,
        journal_taxonomy_digest=TAXONOMY,
        journal_store_identity_digest=JOURNAL,
        start_journal_sequence=10,
        end_journal_sequence=20,
        monotonic_clock_id="python-time.monotonic_ns",
        staleness_basis="host-monotonic-financial-state-age",
        research_interference_basis="host-monotonic-contention-delay",
        financial_samples=(
            FinancialTargetHostSample(
                sample_id="financial-1",
                event_id="event-1",
                journal_sequence=11,
                latency_start_monotonic_ns=1_100_000_000,
                latency_end_monotonic_ns=1_100_100_000,
                staleness_source_monotonic_ns=1_000_000_000,
                staleness_observed_monotonic_ns=1_200_000_000,
            ),
        ),
        research_samples=(
            ResearchInterferenceSample(
                sample_id="research-1",
                phase="contention",
                start_monotonic_ns=1_210_000_000,
                end_monotonic_ns=1_210_050_000,
            ),
        ),
        resource_samples=(
            ResourceTargetHostSample(
                sample_id="resource-1",
                monotonic_ns=1_220_000_000,
                phase="steady",
                metrics={"memory_rss_bytes": 4096, "thread_count": 3},
            ),
        ),
    )


def durable_binding_for(current: TargetHostMeasurementArtifact):
    return SimpleNamespace(
        digest=DURABLE,
        target_host_measurement_digest=current.digest,
        source_sha=current.source_sha,
        spec_digest=current.spec_digest,
    )


def campaign_evidence_for(
    current: TargetHostMeasurementArtifact,
    *,
    observation=None,
):
    if observation is None:
        observation = object()
    return SimpleNamespace(
        digest=CAMPAIGN,
        resource_evidence_hash=current.digest,
        start_journal_sequence=current.start_journal_sequence,
        end_journal_sequence=current.end_journal_sequence,
        recovered_financial_event_ids=tuple(
            sample.event_id for sample in current.financial_samples
        ),
        recovered_financial_event_bindings=tuple(
            (
                sample.event_id,
                "sha256:" + "6" * 64,
                sample.journal_sequence,
            )
            for sample in current.financial_samples
        ),
        to_observation=lambda _spec: observation,
    )


def accepted_for(
    current: TargetHostMeasurementArtifact,
    *,
    payload_overrides=None,
) -> AcceptedRuntimeTargetHostQualification:
    projections = dict(target_host_measurement_projection_digests(current))
    if payload_overrides:
        projections.update(payload_overrides)
    projections[CAMPAIGN_EVIDENCE_KIND] = "sha256:" + "7" * 64
    projections[HOST_INVENTORY_EVIDENCE_KIND] = "sha256:" + "8" * 64
    return AcceptedRuntimeTargetHostQualification(
        attestation_id="60000000-0000-4000-8000-000000000001",
        attestation_digest="sha256:" + "a" * 64,
        source_sha=current.source_sha,
        scenario_id=current.scenario_id,
        spec_digest=current.spec_digest,
        configuration_hash=current.configuration_hash,
        host_fingerprint=current.host_fingerprint,
        workload_profile_hash=current.workload_profile_hash,
        journal_store_identity_digest=current.journal_store_identity_digest,
        release_artifact_id=current.release_artifact_id,
        release_artifact_sha256=current.release_artifact_sha256,
        binding_artifact_id="60000000-0000-4000-8000-000000000002",
        binding_sha256="sha256:" + "b" * 64,
        evidence_sha256_by_kind={},
        payload_artifact_id_by_kind={
            CAMPAIGN_EVIDENCE_KIND: "61000000-0000-4000-8000-000000000001",
            STALENESS_EVIDENCE_KIND: "61000000-0000-4000-8000-000000000002",
            INTERFERENCE_EVIDENCE_KIND: "61000000-0000-4000-8000-000000000003",
            RESOURCE_EVIDENCE_KIND: "61000000-0000-4000-8000-000000000004",
            HOST_INVENTORY_EVIDENCE_KIND: "61000000-0000-4000-8000-000000000005",
        },
        payload_sha256_by_kind=projections,
        collector_by_kind={},
    )


class RuntimeTargetHostComposedQualificationTests(unittest.TestCase):
    def test_measurement_projections_are_domain_separated_and_bind_same_identity(self):
        current = measurement()
        payloads = {
            kind: target_host_measurement_projection_bytes(
                current,
                evidence_kind=kind,
            )
            for kind in (
                STALENESS_EVIDENCE_KIND,
                INTERFERENCE_EVIDENCE_KIND,
                RESOURCE_EVIDENCE_KIND,
            )
        }
        self.assertEqual(len(set(payloads.values())), 3)
        parsed = {kind: json.loads(raw) for kind, raw in payloads.items()}
        for kind, value in parsed.items():
            self.assertEqual(value["evidence_kind"], kind)
            self.assertEqual(value["target_host_measurement_digest"], current.digest)
            self.assertEqual(value["workload_profile_hash"], WORKLOAD)
            self.assertEqual(value["journal_store_identity_digest"], JOURNAL)
            self.assertEqual(value["release_artifact_id"], RELEASE_ID)
            self.assertEqual(value["release_artifact_sha256"], RELEASE_SHA)
        self.assertEqual(
            parsed[STALENESS_EVIDENCE_KIND]["samples"][0]["staleness_us"],
            200_000,
        )
        self.assertEqual(
            parsed[INTERFERENCE_EVIDENCE_KIND]["samples"][0]["interference_us"],
            50,
        )
        self.assertEqual(
            parsed[RESOURCE_EVIDENCE_KIND]["samples"][0]["metrics"]["thread_count"],
            3,
        )

    def test_projection_digests_are_deterministic_and_immutable(self):
        current = measurement()
        first = target_host_measurement_projection_digests(current)
        second = target_host_measurement_projection_digests(
            TargetHostMeasurementArtifact.parse(current.canonical_bytes())
        )
        self.assertEqual(dict(first), dict(second))
        with self.assertRaises(TypeError):
            first[STALENESS_EVIDENCE_KIND] = OTHER

    def test_projection_kind_must_be_exact_supported_text(self):
        current = measurement()

        class ExecutableKind(str):
            pass

        for bad in (
            "RUNTIME_TARGET_HOST_UNKNOWN",
            ExecutableKind(STALENESS_EVIDENCE_KIND),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "canonical target-host measurement projection kind",
            ):
                target_host_measurement_projection_bytes(
                    current,
                    evidence_kind=bad,
                )

    def test_terminal_campaign_authority_failure_prevents_durable_and_signed_dispatch(self):
        current = measurement()
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "collect_release_bound_target_host_evidence",
            side_effect=RuntimeTargetHostMeasurementError("terminal authority failed"),
        ) as campaign, patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_durable_financial_latency_to_target_host_measurement",
        ) as durable, patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
        ) as signed, self.assertRaisesRegex(
            RuntimeTargetHostMeasurementError,
            "terminal authority failed",
        ):
            verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                spec=budget_spec(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )
        campaign.assert_called_once()
        durable.assert_not_called()
        signed.assert_not_called()

    def test_durable_binding_substitution_prevents_signed_verifier_dispatch(self):
        current = measurement()
        substituted = SimpleNamespace(
            digest=DURABLE,
            target_host_measurement_digest=OTHER,
            source_sha=current.source_sha,
            spec_digest=current.spec_digest,
        )
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "collect_release_bound_target_host_evidence",
            return_value=campaign_evidence_for(current),
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_durable_financial_latency_to_target_host_measurement",
            return_value=substituted,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
        ) as signed, self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "durable financial binding does not bind canonical target-host measurement",
        ):
            verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                spec=budget_spec(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )
        signed.assert_not_called()

    def test_campaign_evidence_substitution_prevents_signed_verifier_dispatch(self):
        current = measurement()
        substituted = campaign_evidence_for(current)
        substituted.resource_evidence_hash = OTHER
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "collect_release_bound_target_host_evidence",
            return_value=substituted,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_durable_financial_latency_to_target_host_measurement",
            return_value=durable_binding_for(current),
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
        ) as signed, self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "terminal campaign evidence does not bind canonical target-host measurement",
        ):
            verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                spec=budget_spec(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )
        signed.assert_not_called()

    def test_signed_campaign_observation_must_match_terminal_campaign_evidence(self):
        current = measurement()
        accepted = accepted_for(current)
        expected_observation = object()
        campaign = campaign_evidence_for(
            current,
            observation=expected_observation,
        )
        parsed = SimpleNamespace(
            evidence=SimpleNamespace(
                observation=object(),
                journal_sequence_before=current.start_journal_sequence,
                journal_sequence_after=current.end_journal_sequence,
                recovered_event_ids=("event-1",),
                recovered_journal_sequences=(11,),
            )
        )
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_read_accepted_raw_payload",
            return_value=b"canonical-campaign",
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "ParsedRuntimeTargetHostCampaign.parse",
            return_value=parsed,
        ), self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "campaign observation does not match terminal JournalStore evidence",
        ):
            _require_signed_campaign_match(
                accepted,
                evidence_store=object(),
                evidence_root="unused",
                campaign_evidence=campaign,
                spec=budget_spec(),
            )

    def test_signed_campaign_cut_and_financial_identities_must_match_terminal_evidence(self):
        current = measurement()
        accepted = accepted_for(current)
        expected_observation = object()
        campaign = campaign_evidence_for(
            current,
            observation=expected_observation,
        )
        for parsed_evidence, message in (
            (
                SimpleNamespace(
                    observation=expected_observation,
                    journal_sequence_before=current.start_journal_sequence,
                    journal_sequence_after=current.end_journal_sequence + 1,
                    recovered_event_ids=("event-1",),
                    recovered_journal_sequences=(11,),
                ),
                "campaign journal cut does not match terminal JournalStore evidence",
            ),
            (
                SimpleNamespace(
                    observation=expected_observation,
                    journal_sequence_before=current.start_journal_sequence,
                    journal_sequence_after=current.end_journal_sequence,
                    recovered_event_ids=("substituted-event",),
                    recovered_journal_sequences=(11,),
                ),
                "campaign financial identities do not match terminal JournalStore evidence",
            ),
        ):
            with self.subTest(message=message), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "_read_accepted_raw_payload",
                return_value=b"canonical-campaign",
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "ParsedRuntimeTargetHostCampaign.parse",
                return_value=SimpleNamespace(evidence=parsed_evidence),
            ), self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                message,
            ):
                _require_signed_campaign_match(
                    accepted,
                    evidence_store=object(),
                    evidence_root="unused",
                    campaign_evidence=campaign,
                    spec=budget_spec(),
                )

    def test_accepted_campaign_payload_is_re_read_under_authenticated_digest(self):
        current = measurement()
        raw = b"canonical-campaign-payload"
        digest = "sha256:" + sha256(raw).hexdigest()
        accepted = accepted_for(
            current,
            payload_overrides={CAMPAIGN_EVIDENCE_KIND: digest},
        )
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "trusted_authenticated_reader",
            return_value=lambda artifact_id: ({}, raw),
        ) as reader:
            recovered = _read_accepted_raw_payload(
                accepted,
                evidence_store=object(),
                evidence_root="root",
                evidence_kind=CAMPAIGN_EVIDENCE_KIND,
            )
        self.assertEqual(recovered, raw)
        reader.assert_called_once_with("root", publication_store=unittest.mock.ANY)

    def test_accepted_campaign_payload_digest_change_is_rejected(self):
        current = measurement()
        accepted = accepted_for(current)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "trusted_authenticated_reader",
            return_value=lambda artifact_id: ({}, b"changed-campaign-payload"),
        ), self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "raw payload changed after canonical verification",
        ):
            _read_accepted_raw_payload(
                accepted,
                evidence_store=object(),
                evidence_root="root",
                evidence_kind=CAMPAIGN_EVIDENCE_KIND,
            )

    def test_signed_opaque_measurement_payload_is_rejected_after_authority_validation(self):
        current = measurement()
        accepted = accepted_for(
            current,
            payload_overrides={STALENESS_EVIDENCE_KIND: OTHER},
        )
        campaign = campaign_evidence_for(current)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "collect_release_bound_target_host_evidence",
            return_value=campaign,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_durable_financial_latency_to_target_host_measurement",
            return_value=durable_binding_for(current),
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
            return_value=accepted,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_require_signed_campaign_match",
        ), self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "RUNTIME_TARGET_HOST_STALENESS raw payload does not bind canonical target-host measurement",
        ):
            verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                spec=budget_spec(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

    def test_exact_projection_payloads_compose_signed_campaign_and_durable_authorities(self):
        current = measurement()
        accepted = accepted_for(current)
        campaign = campaign_evidence_for(current)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "collect_release_bound_target_host_evidence",
            return_value=campaign,
        ) as terminal, patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_durable_financial_latency_to_target_host_measurement",
            return_value=durable_binding_for(current),
        ) as durable, patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
            return_value=accepted,
        ) as signed, patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_require_signed_campaign_match",
        ) as campaign_match:
            result = verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                spec=budget_spec(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        self.assertIs(result.qualification, accepted)
        self.assertEqual(result.target_host_measurement_digest, current.digest)
        self.assertEqual(result.durable_financial_binding_digest, DURABLE)
        self.assertEqual(result.campaign_evidence_digest, CAMPAIGN)
        self.assertEqual(
            dict(result.projection_sha256_by_kind),
            dict(target_host_measurement_projection_digests(current)),
        )
        terminal.assert_called_once()
        durable.assert_called_once()
        campaign_match.assert_called_once()
        signed.assert_called_once()
        signed_kwargs = signed.call_args.kwargs
        self.assertEqual(signed_kwargs["expected_source_sha"], current.source_sha)
        self.assertEqual(
            signed_kwargs["expected_workload_profile_hash"],
            current.workload_profile_hash,
        )
        self.assertEqual(
            signed_kwargs["expected_journal_store_identity_digest"],
            current.journal_store_identity_digest,
        )
        self.assertEqual(
            signed_kwargs["expected_release_artifact_id"],
            RELEASE_ID,
        )
        self.assertEqual(
            signed_kwargs["expected_release_artifact_sha256"],
            RELEASE_SHA,
        )

    def test_caller_measurement_mutation_after_snapshot_cannot_change_signed_expectations(self):
        current = measurement()
        original_configuration = current.configuration_hash
        accepted = accepted_for(current)
        campaign = campaign_evidence_for(current)

        def terminal_with_mutation(**kwargs):
            snapshotted = kwargs["measurement"]
            self.assertIsNot(snapshotted, current)
            object.__setattr__(current, "configuration_hash", OTHER)
            return campaign

        def durable_after_mutation(*_args, **kwargs):
            return durable_binding_for(kwargs["measurement"])

        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "collect_release_bound_target_host_evidence",
            side_effect=terminal_with_mutation,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_durable_financial_latency_to_target_host_measurement",
            side_effect=durable_after_mutation,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
            return_value=accepted,
        ) as signed, patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_require_signed_campaign_match",
        ):
            result = verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                spec=budget_spec(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        self.assertEqual(
            signed.call_args.kwargs["expected_configuration_hash"],
            original_configuration,
        )
        self.assertEqual(
            result.qualification.configuration_hash,
            original_configuration,
        )
        self.assertNotEqual(current.configuration_hash, original_configuration)


if __name__ == "__main__":
    unittest.main()
