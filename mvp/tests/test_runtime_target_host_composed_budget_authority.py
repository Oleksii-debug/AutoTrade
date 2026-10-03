from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
)
from mvp.autotrade_mvp.runtime_load_qualification import RuntimeCampaignPlan
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
    target_host_measurement_projection_digests,
    verify_composed_runtime_target_host_qualification,
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
)


SOURCE = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
WORKLOAD = "sha256:" + "d" * 64
JOURNAL = "sha256:" + "e" * 64
RELEASE_ID = "70000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "f" * 64
DURABLE = "sha256:" + "1" * 64


def spec(*, min_financial_samples: int = 1) -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-composed-budget-authority",
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500_000,
        max_research_interference_us=500,
        min_financial_samples=min_financial_samples,
        min_research_samples=1,
    )


def plan(current_spec: RuntimeBudgetSpec, *, event_ids=("event-1",)) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=current_spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1_000,
        expected_financial_event_ids=event_ids,
        financial_aggregate_types=("risk_decision",),
        release_artifact_sha256=RELEASE_SHA,
    )


def measurement(
    current_spec: RuntimeBudgetSpec,
    current_plan: RuntimeCampaignPlan,
    *,
    latency_us: int = 100,
    staleness_us: int = 200_000,
    interference_us: int = 50,
) -> TargetHostMeasurementArtifact:
    observed_ns = 1_800_000_000
    latency_start_ns = 1_100_000_000
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id=current_spec.scenario_id,
        spec_digest=current_spec.digest,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=WORKLOAD,
        plan_digest=current_plan.digest,
        journal_taxonomy_digest=current_plan.journal_taxonomy_digest,
        journal_store_identity_digest=JOURNAL,
        start_journal_sequence=0,
        end_journal_sequence=1,
        monotonic_clock_id="python-time.monotonic_ns",
        staleness_basis="host-monotonic-financial-state-age",
        research_interference_basis="host-monotonic-contention-delay",
        financial_samples=(
            FinancialTargetHostSample(
                sample_id="financial-1",
                event_id="event-1",
                journal_sequence=1,
                latency_start_monotonic_ns=latency_start_ns,
                latency_end_monotonic_ns=latency_start_ns + latency_us * 1_000,
                staleness_source_monotonic_ns=observed_ns - staleness_us * 1_000,
                staleness_observed_monotonic_ns=observed_ns,
            ),
        ),
        research_samples=(
            ResearchInterferenceSample(
                sample_id="research-1",
                phase="contention",
                start_monotonic_ns=1_900_000_000,
                end_monotonic_ns=1_900_000_000 + interference_us * 1_000,
            ),
        ),
        resource_samples=(
            ResourceTargetHostSample(
                sample_id="resource-1",
                monotonic_ns=1_950_000_000,
                phase="steady",
                metrics={"memory_rss_bytes": 4096},
            ),
        ),
    )


def accepted_for(current: TargetHostMeasurementArtifact) -> AcceptedRuntimeTargetHostQualification:
    payloads = dict(target_host_measurement_projection_digests(current))
    payloads[CAMPAIGN_EVIDENCE_KIND] = "sha256:" + "7" * 64
    payloads[HOST_INVENTORY_EVIDENCE_KIND] = "sha256:" + "8" * 64
    return AcceptedRuntimeTargetHostQualification(
        attestation_id="71000000-0000-4000-8000-000000000001",
        attestation_digest="sha256:" + "2" * 64,
        source_sha=current.source_sha,
        scenario_id=current.scenario_id,
        spec_digest=current.spec_digest,
        configuration_hash=current.configuration_hash,
        host_fingerprint=current.host_fingerprint,
        workload_profile_hash=current.workload_profile_hash,
        journal_store_identity_digest=current.journal_store_identity_digest,
        release_artifact_id=current.release_artifact_id,
        release_artifact_sha256=current.release_artifact_sha256,
        binding_artifact_id="71000000-0000-4000-8000-000000000002",
        binding_sha256="sha256:" + "3" * 64,
        evidence_sha256_by_kind={},
        payload_artifact_id_by_kind={},
        payload_sha256_by_kind=payloads,
        collector_by_kind={},
    )


def parsed_campaign(
    current_spec: RuntimeBudgetSpec,
    current_plan: RuntimeCampaignPlan,
    current: TargetHostMeasurementArtifact,
    *,
    expected_financial_events: int | None = None,
    recovered_financial_events: int | None = None,
    reconnect_backlog_remaining: int = 0,
    observed_duration_us: int | None = None,
):
    if expected_financial_events is None:
        expected_financial_events = len(current_plan.expected_financial_event_ids)
    if recovered_financial_events is None:
        recovered_financial_events = len(current.financial_samples)
    if observed_duration_us is None:
        observed_duration_us = current_plan.declared_duration_ms * 1_000
    observation = RuntimeLoadObservation.create(
        scenario_id=current_spec.scenario_id,
        spec_digest=current_spec.digest,
        release_sha=current_spec.release_sha,
        configuration_hash=current_spec.configuration_hash,
        host_fingerprint=current_spec.host_fingerprint,
        expected_financial_events=expected_financial_events,
        recovered_financial_events=recovered_financial_events,
        financial_latency_us=current.financial_latency_us,
        financial_staleness_us=current.financial_staleness_us,
        research_interference_us=current.research_interference_us,
        reconnect_backlog_remaining=reconnect_backlog_remaining,
        declared_duration_us=current_plan.declared_duration_ms * 1_000,
        observed_duration_us=observed_duration_us,
    )
    return SimpleNamespace(
        evidence=SimpleNamespace(
            observation=observation,
            journal_sequence_before=current.start_journal_sequence,
            journal_sequence_after=current.end_journal_sequence,
            recovered_event_ids=tuple(
                sample.event_id for sample in current.financial_samples
            ),
            recovered_journal_sequences=tuple(
                sample.journal_sequence for sample in current.financial_samples
            ),
        )
    )


class RuntimeTargetHostComposedBudgetAuthorityTests(unittest.TestCase):
    def _assert_signed_pass_rejected(
        self,
        current_spec: RuntimeBudgetSpec,
        current_plan: RuntimeCampaignPlan,
        current: TargetHostMeasurementArtifact,
        parsed,
        reason: str,
    ) -> None:
        accepted = accepted_for(current)
        durable = SimpleNamespace(
            digest=DURABLE,
            target_host_measurement_digest=current.digest,
            source_sha=current.source_sha,
            spec_digest=current.spec_digest,
        )
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
            return_value=durable,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
            return_value=accepted,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_read_accepted_raw_payload",
            return_value=b"canonical-campaign",
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "ParsedRuntimeTargetHostCampaign.parse",
            return_value=parsed,
        ), self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            reason,
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

    def test_signed_pass_cannot_override_canonical_budget_failures(self):
        cases = (
            ("event_loss", {}, ("event-1", "event-2"), 1, None, "financial_event_loss"),
            ("backlog", {}, ("event-1",), None, 1, "reconnect_backlog_not_drained"),
            ("latency", {"latency_us": 501}, ("event-1",), None, None, "financial_latency_budget_exceeded"),
            ("staleness", {"staleness_us": 500_001}, ("event-1",), None, None, "financial_staleness_budget_exceeded"),
            ("interference", {"interference_us": 501}, ("event-1",), None, None, "research_interference_budget_exceeded"),
        )
        for name, measurement_kwargs, event_ids, recovered, backlog, reason in cases:
            with self.subTest(name=name):
                current_spec = spec()
                current_plan = plan(current_spec, event_ids=event_ids)
                current = measurement(
                    current_spec,
                    current_plan,
                    **measurement_kwargs,
                )
                parsed = parsed_campaign(
                    current_spec,
                    current_plan,
                    current,
                    recovered_financial_events=recovered,
                    reconnect_backlog_remaining=0 if backlog is None else backlog,
                )
                self._assert_signed_pass_rejected(
                    current_spec,
                    current_plan,
                    current,
                    parsed,
                    reason,
                )

    def test_signed_pass_cannot_override_insufficient_samples(self):
        current_spec = spec(min_financial_samples=2)
        current_plan = plan(current_spec)
        current = measurement(current_spec, current_plan)
        parsed = parsed_campaign(current_spec, current_plan, current)
        self._assert_signed_pass_rejected(
            current_spec,
            current_plan,
            current,
            parsed,
            "insufficient_financial_latency_samples",
        )

    def test_signed_pass_cannot_override_declared_throughput_shortfall(self):
        current_spec = spec()
        current_plan = plan(current_spec)
        current = measurement(current_spec, current_plan)
        parsed = parsed_campaign(
            current_spec,
            current_plan,
            current,
            observed_duration_us=current_plan.declared_duration_ms * 2_000,
        )
        self._assert_signed_pass_rejected(
            current_spec,
            current_plan,
            current,
            parsed,
            "declared_throughput_not_met",
        )


if __name__ == "__main__":
    unittest.main()
