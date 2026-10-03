from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_campaign import (
    RuntimeLoadCampaignEvidence,
    serialize_runtime_load_campaign_evidence,
)
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    MEASUREMENT_METHOD_ID,
    MEASUREMENT_METHOD_VERSION,
    MONOTONIC_CLOCK_ID,
    FinancialTargetHostSample,
    ResourceTargetHostSample,
    TargetHostMeasurementArtifact,
)
from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as terminal_module
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    CAMPAIGN_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    AcceptedRuntimeTargetHostQualification,
    RuntimeTargetHostQualificationError,
)


SOURCE = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
WORKLOAD = "sha256:" + ("c" * 64)
ALT_WORKLOAD = "sha256:" + ("9" * 64)
RELEASE_ID = "70000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + ("d" * 64)
HOST_IDENTITY = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.13.7",
    "cpu_count": 8,
}
HOST = host_identity_fingerprint(HOST_IDENTITY)


def _sha(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="terminal-measurement-composition",
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=10_000,
        max_p95_financial_latency_us=10_000,
        max_financial_staleness_us=10_000,
        max_research_interference_us=10_000,
        min_financial_samples=1,
        min_research_samples=0,
    )


def _expected() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="financial-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="risk-1",
        aggregate_version=1,
    )


def _campaign_plan(spec: RuntimeBudgetSpec, *, workload: str = WORKLOAD) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan(
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        release_sha=spec.release_sha,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        workload_profile_hash=workload,
        declared_duration_ms=1,
        expected_financial_event_ids=("financial-1",),
        financial_aggregate_types=("risk_decision",),
        release_artifact_sha256=RELEASE_SHA,
    )


def _measurement(spec, campaign_plan, cut) -> TargetHostMeasurementArtifact:
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=campaign_plan.workload_profile_hash,
        plan_digest=campaign_plan.digest,
        journal_taxonomy_digest=campaign_plan.journal_taxonomy_digest,
        journal_store_identity_digest=cut.journal_store_identity_digest,
        start_journal_sequence=cut.start_journal_sequence,
        end_journal_sequence=cut.start_journal_sequence + 1,
        monotonic_clock_id=MONOTONIC_CLOCK_ID,
        staleness_basis="same-host-monotonic-source-age",
        research_interference_basis="same-host-monotonic-contention-delay",
        financial_samples=(
            FinancialTargetHostSample(
                sample_id="financial-sample-1",
                event_id="financial-1",
                journal_sequence=cut.start_journal_sequence + 1,
                latency_start_monotonic_ns=1_000_000,
                latency_end_monotonic_ns=1_000_100,
                staleness_source_monotonic_ns=999_900,
                staleness_observed_monotonic_ns=1_000_100,
            ),
        ),
        research_samples=(),
        resource_samples=(
            ResourceTargetHostSample(
                sample_id="resource-sample-1",
                monotonic_ns=1_000_050,
                phase="steady-state",
                metrics={"cpu_peak_millis": 1},
            ),
        ),
    )


def _campaign_raw(spec, measurement, *, event_id: str = "financial-1") -> bytes:
    observation = RuntimeLoadObservation.create(
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        expected_financial_events=1,
        recovered_financial_events=1,
        financial_latency_us=measurement.financial_latency_us,
        financial_staleness_us=measurement.financial_staleness_us,
        research_interference_us=measurement.research_interference_us,
        reconnect_backlog_remaining=0,
        declared_duration_us=1_000,
        observed_duration_us=900,
    )
    evidence = RuntimeLoadCampaignEvidence(
        observation=observation,
        journal_sequence_before=measurement.start_journal_sequence,
        journal_sequence_after=measurement.end_journal_sequence,
        recovered_event_ids=(event_id,),
        recovered_journal_sequences=(measurement.end_journal_sequence,),
        host_identity=HOST_IDENTITY,
    )
    return serialize_runtime_load_campaign_evidence(evidence)


def _accepted(measurement_raw: bytes, campaign_raw: bytes) -> AcceptedRuntimeTargetHostQualification:
    return AcceptedRuntimeTargetHostQualification(
        attestation_id="80000000-0000-4000-8000-000000000001",
        attestation_digest="sha256:" + ("1" * 64),
        source_sha=SOURCE,
        scenario_id="terminal-measurement-composition",
        spec_digest="sha256:" + ("2" * 64),
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=WORKLOAD,
        journal_store_identity_digest="sha256:" + ("3" * 64),
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        binding_artifact_id="80000000-0000-4000-8000-000000000002",
        binding_sha256="sha256:" + ("4" * 64),
        evidence_sha256_by_kind={},
        payload_artifact_id_by_kind={
            RESOURCE_EVIDENCE_KIND: "80000000-0000-4000-8000-000000000003",
            CAMPAIGN_EVIDENCE_KIND: "80000000-0000-4000-8000-000000000004",
        },
        payload_sha256_by_kind={
            RESOURCE_EVIDENCE_KIND: _sha(measurement_raw),
            CAMPAIGN_EVIDENCE_KIND: _sha(campaign_raw),
        },
        collector_by_kind={
            RESOURCE_EVIDENCE_KIND: (
                f"{MEASUREMENT_METHOD_ID}@{MEASUREMENT_METHOD_VERSION}"
            ),
        },
    )


class RuntimeTargetHostTerminalMeasurementCompositionTests(unittest.TestCase):
    def _material(self, *, workload: str = WORKLOAD):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = JournalStore(Path(directory.name) / "journal.sqlite3")
        spec = _spec()
        declared = declare_runtime_event_plan(
            store,
            plan_id="terminal-plan",
            spec=spec,
            expected_events=(_expected(),),
        )
        campaign_plan = _campaign_plan(spec, workload=workload)
        cut = begin_runtime_campaign(journal=store, spec=spec, plan=campaign_plan)
        measurement = _measurement(spec, campaign_plan, cut)
        return directory.name, store, spec, declared, campaign_plan, cut, measurement

    def _verify_composition(
        self,
        *,
        directory,
        spec,
        declared,
        campaign_plan,
        cut,
        measurement_raw,
        campaign_raw,
        accepted_value=None,
    ):
        accepted_value = accepted_value or _accepted(measurement_raw, campaign_raw)
        raw_by_id = {
            accepted_value.payload_artifact_id_by_kind[RESOURCE_EVIDENCE_KIND]: measurement_raw,
            accepted_value.payload_artifact_id_by_kind[CAMPAIGN_EVIDENCE_KIND]: campaign_raw,
        }

        def reader(artifact_id):
            return {}, raw_by_id[artifact_id]

        artifact_store = ArtifactStore(Path(directory) / "artifacts")
        with patch.object(
            terminal_module,
            "trusted_authenticated_reader",
            return_value=reader,
        ):
            return terminal_module._verify_measurement_campaign_composition(
                accepted_value,
                evidence_store=artifact_store,
                evidence_root=directory,
                spec=spec,
                campaign_plan=campaign_plan,
                campaign_cut=cut,
                declared_plan=declared,
                expected_release_artifact_id=RELEASE_ID,
            )

    def test_signed_campaign_is_cross_bound_to_canonical_retained_measurement(self) -> None:
        directory, _store, spec, declared, campaign_plan, cut, measurement = self._material()
        measurement_raw = measurement.canonical_bytes()
        campaign_raw = _campaign_raw(spec, measurement)
        accepted_value = _accepted(measurement_raw, campaign_raw)
        result = self._verify_composition(
            directory=directory,
            spec=spec,
            declared=declared,
            campaign_plan=campaign_plan,
            cut=cut,
            measurement_raw=measurement_raw,
            campaign_raw=campaign_raw,
            accepted_value=accepted_value,
        )
        self.assertIs(result, accepted_value)

    def test_identical_campaign_bytes_cannot_be_resigned_for_another_workload(self) -> None:
        directory, store, spec, declared, campaign_plan, cut, measurement = self._material()
        measurement_raw = measurement.canonical_bytes()
        campaign_raw = _campaign_raw(spec, measurement)

        alternate_plan = _campaign_plan(spec, workload=ALT_WORKLOAD)
        alternate_cut = begin_runtime_campaign(
            journal=store,
            spec=spec,
            plan=alternate_plan,
        )
        self.assertEqual(campaign_raw, _campaign_raw(spec, measurement))
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "another campaign identity: workload_profile_hash",
        ):
            self._verify_composition(
                directory=directory,
                spec=spec,
                declared=declared,
                campaign_plan=alternate_plan,
                cut=alternate_cut,
                measurement_raw=measurement_raw,
                campaign_raw=campaign_raw,
            )

    def test_campaign_financial_identity_must_match_raw_measurement(self) -> None:
        directory, _store, spec, declared, campaign_plan, cut, measurement = self._material()
        measurement_raw = measurement.canonical_bytes()
        campaign_raw = _campaign_raw(spec, measurement, event_id="other-financial-event")
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "financial event identities",
        ):
            self._verify_composition(
                directory=directory,
                spec=spec,
                declared=declared,
                campaign_plan=campaign_plan,
                cut=cut,
                measurement_raw=measurement_raw,
                campaign_raw=campaign_raw,
            )

    def test_resource_provenance_must_name_canonical_measurement_method(self) -> None:
        directory, _store, spec, declared, campaign_plan, cut, measurement = self._material()
        measurement_raw = measurement.canonical_bytes()
        campaign_raw = _campaign_raw(spec, measurement)
        accepted_value = _accepted(measurement_raw, campaign_raw)
        object.__setattr__(
            accepted_value,
            "collector_by_kind",
            {RESOURCE_EVIDENCE_KIND: "generic-resource-collector@1.0.0"},
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "does not identify canonical measurement method",
        ):
            self._verify_composition(
                directory=directory,
                spec=spec,
                declared=declared,
                campaign_plan=campaign_plan,
                cut=cut,
                measurement_raw=measurement_raw,
                campaign_raw=campaign_raw,
                accepted_value=accepted_value,
            )


if __name__ == "__main__":
    unittest.main()
