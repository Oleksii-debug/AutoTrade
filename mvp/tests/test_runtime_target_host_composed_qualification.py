from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.runtime_load_qualification import RuntimeCampaignPlan
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
    _read_accepted_raw_payload,
    _require_signed_campaign_match,
    target_host_measurement_projection_bytes,
    target_host_measurement_projection_digests,
    verify_composed_runtime_target_host_qualification,
)
from mvp.autotrade_mvp.runtime_target_host_durable_financial import (
    RuntimeTargetHostDurableFinancialError,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResearchInterferenceSample,
    ResourceTargetHostSample,
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
TAXONOMY = "sha256:" + "f" * 64
JOURNAL = "sha256:" + "1" * 64
RELEASE_ID = "50000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "2" * 64
DURABLE = "sha256:" + "4" * 64
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


def runtime_campaign_plan(current_spec: RuntimeBudgetSpec) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=current_spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1_000,
        expected_financial_event_ids=("event-1",),
        financial_aggregate_types=("risk_decision",),
        release_artifact_sha256=RELEASE_SHA,
    )


SPEC = budget_spec().digest
PLAN = runtime_campaign_plan(budget_spec()).digest


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


def campaign_observation_for(
    current: TargetHostMeasurementArtifact,
    plan: RuntimeCampaignPlan,
    **overrides,
):
    values = {
        "expected_financial_events": len(plan.expected_financial_event_ids),
        "recovered_financial_events": len(current.financial_samples),
        "financial_latency_us": current.financial_latency_us,
        "financial_staleness_us": current.financial_staleness_us,
        "research_interference_us": current.research_interference_us,
        "reconnect_backlog_remaining": 0,
        "declared_duration_us": plan.declared_duration_ms * 1_000,
        "observed_duration_us": plan.declared_duration_ms * 1_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def parsed_campaign_for(
    current: TargetHostMeasurementArtifact,
    plan: RuntimeCampaignPlan,
    *,
    observation=None,
    journal_sequence_before=None,
    journal_sequence_after=None,
    recovered_event_ids=None,
    recovered_journal_sequences=None,
):
    if observation is None:
        observation = campaign_observation_for(current, plan)
    if journal_sequence_before is None:
        journal_sequence_before = current.start_journal_sequence
    if journal_sequence_after is None:
        journal_sequence_after = current.end_journal_sequence
    if recovered_event_ids is None:
        recovered_event_ids = tuple(
            sample.event_id for sample in current.financial_samples
        )
    if recovered_journal_sequences is None:
        recovered_journal_sequences = tuple(
            sample.journal_sequence for sample in current.financial_samples
        )
    return SimpleNamespace(
        evidence=SimpleNamespace(
            observation=observation,
            journal_sequence_before=journal_sequence_before,
            journal_sequence_after=journal_sequence_after,
            recovered_event_ids=recovered_event_ids,
            recovered_journal_sequences=recovered_journal_sequences,
        )
    )


def accepted_for(
    current: TargetHostMeasurementArtifact,
    *,
    payload_overrides=None,
) -> AcceptedRuntimeTargetHostQualification:
    projections = dict(target_host_measurement_projection_digests(current))
    projections[CAMPAIGN_EVIDENCE_KIND] = "sha256:" + "7" * 64
    projections[HOST_INVENTORY_EVIDENCE_KIND] = "sha256:" + "8" * 64
    if payload_overrides:
        projections.update(payload_overrides)
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

    def test_terminal_durable_authority_failure_prevents_signed_dispatch(self):
        current = measurement()
        current_spec = budget_spec()
        current_plan = runtime_campaign_plan(current_spec)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
            side_effect=RuntimeTargetHostDurableFinancialError("terminal authority failed"),
        ) as durable, patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
        ) as signed, self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "terminal authority failed",
        ):
            verify_composed_runtime_target_host_qualification(
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
        durable.assert_called_once()
        signed.assert_not_called()

    def test_durable_binding_substitution_prevents_signed_verifier_dispatch(self):
        current = measurement()
        current_spec = budget_spec()
        current_plan = runtime_campaign_plan(current_spec)
        substituted = SimpleNamespace(
            digest=DURABLE,
            target_host_measurement_digest=OTHER,
            source_sha=current.source_sha,
            spec_digest=current.spec_digest,
        )
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
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
                spec=current_spec,
                campaign_plan=current_plan,
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )
        signed.assert_not_called()

    def test_signed_campaign_metric_series_must_match_measurement(self):
        current = measurement()
        current_plan = runtime_campaign_plan(budget_spec())
        accepted = accepted_for(current)
        mismatches = (
            {"financial_latency_us": (999,)},
            {"financial_staleness_us": (999,)},
            {"research_interference_us": (999,)},
            {"expected_financial_events": 2},
            {"recovered_financial_events": 0},
        )
        for overrides in mismatches:
            parsed = parsed_campaign_for(
                current,
                current_plan,
                observation=campaign_observation_for(
                    current,
                    current_plan,
                    **overrides,
                ),
            )
            with self.subTest(overrides=overrides), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "_read_accepted_raw_payload",
                return_value=b"canonical-campaign",
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "ParsedRuntimeTargetHostCampaign.parse",
                return_value=parsed,
            ), self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "campaign metric series do not match canonical target-host measurement",
            ):
                _require_signed_campaign_match(
                    accepted,
                    evidence_store=object(),
                    evidence_root="unused",
                    measurement=current,
                    campaign_plan=current_plan,
                )

    def test_signed_campaign_declared_duration_and_observed_duration_are_required(self):
        current = measurement()
        current_plan = runtime_campaign_plan(budget_spec())
        accepted = accepted_for(current)
        for overrides, message in (
            (
                {"declared_duration_us": 999},
                "campaign declared duration does not match canonical campaign plan",
            ),
            (
                {"observed_duration_us": None},
                "campaign lacks observed target-host duration",
            ),
        ):
            parsed = parsed_campaign_for(
                current,
                current_plan,
                observation=campaign_observation_for(
                    current,
                    current_plan,
                    **overrides,
                ),
            )
            with self.subTest(overrides=overrides), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "_read_accepted_raw_payload",
                return_value=b"canonical-campaign",
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "ParsedRuntimeTargetHostCampaign.parse",
                return_value=parsed,
            ), self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                message,
            ):
                _require_signed_campaign_match(
                    accepted,
                    evidence_store=object(),
                    evidence_root="unused",
                    measurement=current,
                    campaign_plan=current_plan,
                )

    def test_signed_campaign_cut_and_financial_identities_must_match_measurement(self):
        current = measurement()
        current_plan = runtime_campaign_plan(budget_spec())
        accepted = accepted_for(current)
        cases = (
            (
                {"journal_sequence_after": current.end_journal_sequence + 1},
                "campaign journal cut does not match canonical target-host measurement",
            ),
            (
                {"recovered_event_ids": ("substituted-event",)},
                "campaign financial identities do not match canonical target-host measurement",
            ),
            (
                {"recovered_journal_sequences": (12,)},
                "campaign financial identities do not match canonical target-host measurement",
            ),
        )
        for overrides, message in cases:
            parsed = parsed_campaign_for(current, current_plan, **overrides)
            with self.subTest(overrides=overrides), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "_read_accepted_raw_payload",
                return_value=b"canonical-campaign",
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
                "ParsedRuntimeTargetHostCampaign.parse",
                return_value=parsed,
            ), self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                message,
            ):
                _require_signed_campaign_match(
                    accepted,
                    evidence_store=object(),
                    evidence_root="unused",
                    measurement=current,
                    campaign_plan=current_plan,
                )

    def test_accepted_campaign_payload_is_re_read_under_authenticated_digest(self):
        current = measurement()
        raw = b"canonical-campaign-payload"
        digest = "sha256:" + sha256(raw).hexdigest()
        accepted = accepted_for(
            current,
            payload_overrides={CAMPAIGN_EVIDENCE_KIND: digest},
        )
        evidence_store = object()
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "trusted_authenticated_reader",
            return_value=lambda artifact_id: ({}, raw),
        ) as reader:
            recovered = _read_accepted_raw_payload(
                accepted,
                evidence_store=evidence_store,
                evidence_root="root",
                evidence_kind=CAMPAIGN_EVIDENCE_KIND,
            )
        self.assertEqual(recovered, raw)
        reader.assert_called_once_with("root", publication_store=evidence_store)

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
        current_spec = budget_spec()
        current_plan = runtime_campaign_plan(current_spec)
        accepted = accepted_for(
            current,
            payload_overrides={STALENESS_EVIDENCE_KIND: OTHER},
        )
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
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
                spec=current_spec,
                campaign_plan=current_plan,
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

    def test_exact_projection_payloads_compose_signed_campaign_and_durable_authorities(self):
        current = measurement()
        current_spec = budget_spec()
        current_plan = runtime_campaign_plan(current_spec)
        accepted = accepted_for(current)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
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
        self.assertEqual(result.durable_financial_binding_digest, DURABLE)
        self.assertEqual(
            dict(result.projection_sha256_by_kind),
            dict(target_host_measurement_projection_digests(current)),
        )
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

    def test_caller_measurement_and_plan_mutation_after_snapshot_cannot_change_composition(self):
        current = measurement()
        current_spec = budget_spec()
        current_plan = runtime_campaign_plan(current_spec)
        original_configuration = current.configuration_hash
        original_duration = current_plan.declared_duration_ms
        accepted = accepted_for(current)

        def durable_with_mutation(**kwargs):
            snapshotted_measurement = kwargs["measurement"]
            snapshotted_plan = kwargs["campaign_plan"]
            self.assertIsNot(snapshotted_measurement, current)
            self.assertIsNot(snapshotted_plan, current_plan)
            object.__setattr__(current, "configuration_hash", OTHER)
            object.__setattr__(current_plan, "declared_duration_ms", 9_999)
            return durable_binding_for(snapshotted_measurement)

        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
            side_effect=durable_with_mutation,
        ), patch(
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
                spec=current_spec,
                campaign_plan=current_plan,
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
            campaign_match.call_args.kwargs["campaign_plan"].declared_duration_ms,
            original_duration,
        )
        self.assertEqual(
            result.qualification.configuration_hash,
            original_configuration,
        )
        self.assertNotEqual(current.configuration_hash, original_configuration)
        self.assertNotEqual(current_plan.declared_duration_ms, original_duration)


    def test_post_issuance_executable_measurement_sample_is_rejected_without_execution(self):
        current = measurement()
        executed = []

        class ExecutableSample:
            def canonical_payload(self):
                executed.append("canonical_payload")
                raise AssertionError("caller sample code executed")

        object.__setattr__(
            current,
            "financial_samples",
            (ExecutableSample(),),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "financial_samples must contain exact FinancialTargetHostSample",
        ):
            target_host_measurement_projection_digests(current)

        self.assertEqual(executed, [])

    def test_post_issuance_executable_resource_metrics_are_rejected_without_iteration(self):
        current = measurement()
        executed = []

        class ExecutableMetrics(dict):
            def __iter__(self):
                executed.append("__iter__")
                raise AssertionError("caller mapping iteration executed")

            def items(self):
                executed.append("items")
                raise AssertionError("caller mapping items executed")

        object.__setattr__(
            current.resource_samples[0],
            "metrics",
            ExecutableMetrics({"memory_rss_bytes": 4096, "thread_count": 3}),
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "resource sample metrics must remain canonical mappingproxy",
        ):
            target_host_measurement_projection_digests(current)

        self.assertEqual(executed, [])


if __name__ == "__main__":
    unittest.main()
