import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
    evaluate_runtime_campaign,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResearchInterferenceSample,
    ResourceTargetHostSample,
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    collect_runtime_campaign_evidence_from_measurement_artifact,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
WORKLOAD = "sha256:" + "d" * 64
RELEASE_ARTIFACT_ID = "40000000-0000-4000-8000-000000000001"
ALT_RELEASE_ARTIFACT_ID = "40000000-0000-4000-8000-000000000002"
RELEASE_ARTIFACT_SHA = "sha256:" + "e" * 64
ALT_RELEASE_ARTIFACT_SHA = "sha256:" + "f" * 64
FINANCIAL_AGGREGATE = "risk_decision"


def runtime_spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-target-host-raw",
        release_sha=SOURCE_SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=2,
        min_research_samples=1,
    )


def campaign_plan(spec: RuntimeBudgetSpec, *event_ids: str) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1_000,
        expected_financial_event_ids=event_ids,
        financial_aggregate_types=(FINANCIAL_AGGREGATE,),
        release_artifact_sha256=RELEASE_ARTIFACT_SHA,
    )


def envelope(event_id: str, *, aggregate_type: str = FINANCIAL_AGGREGATE):
    payload = {"event_id": event_id, "kind": aggregate_type}
    return {
        "event_id": event_id,
        "event_type": "QualificationEvent",
        "aggregate_type": aggregate_type,
        "aggregate_id": event_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-03T07:00:00+00:00",
    }


def financial_sample(event_id: str, sequence: int, index: int):
    start = 1_100_000_000 + index * 10_000_000
    return FinancialTargetHostSample(
        sample_id=f"financial-sample-{index}",
        event_id=event_id,
        journal_sequence=sequence,
        latency_start_monotonic_ns=start,
        latency_end_monotonic_ns=start + 100_000,
        staleness_source_monotonic_ns=start - 200_000,
        staleness_observed_monotonic_ns=start,
    )


def measurement(
    *,
    current_plan,
    cut,
    event_ids=("fin-1", "fin-2"),
    sequences=(1, 2),
    end_sequence=2,
    release_artifact_id=RELEASE_ARTIFACT_ID,
    release_artifact_sha256=RELEASE_ARTIFACT_SHA,
    financial_samples=None,
    resource_samples=None,
):
    if financial_samples is None:
        financial_samples = tuple(
            financial_sample(event_id, sequence, index)
            for index, (event_id, sequence) in enumerate(
                zip(event_ids, sequences, strict=True),
                start=1,
            )
        )
    if resource_samples is None:
        resource_samples = (
            ResourceTargetHostSample(
                sample_id="resource-1",
                monotonic_ns=1_200_000_000,
                phase="steady",
                metrics={
                    "cpu_busy_milli_pct": 400,
                    "disk_write_bytes": 1000,
                    "memory_rss_bytes": 4096,
                    "queue_backlog": 0,
                },
            ),
            ResourceTargetHostSample(
                sample_id="resource-2",
                monotonic_ns=1_600_000_000,
                phase="pressure",
                metrics={
                    "cpu_busy_milli_pct": 700,
                    "disk_write_bytes": 2000,
                    "memory_rss_bytes": 8192,
                    "queue_backlog": 1,
                },
            ),
        )
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE_SHA,
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
        scenario_id=current_plan.scenario_id,
        spec_digest=current_plan.spec_digest,
        configuration_hash=current_plan.configuration_hash,
        host_fingerprint=current_plan.host_fingerprint,
        workload_profile_hash=current_plan.workload_profile_hash,
        plan_digest=current_plan.digest,
        journal_taxonomy_digest=current_plan.journal_taxonomy_digest,
        journal_store_identity_digest=cut.journal_store_identity_digest,
        start_journal_sequence=cut.start_journal_sequence,
        end_journal_sequence=end_sequence,
        monotonic_clock_id="python-time.monotonic_ns",
        staleness_basis="host-monotonic-financial-state-age",
        research_interference_basis="host-monotonic-contention-delay",
        financial_samples=tuple(financial_samples),
        research_samples=(
            ResearchInterferenceSample(
                sample_id="research-1",
                phase="research-contention",
                start_monotonic_ns=1_500_000_000,
                end_monotonic_ns=1_500_050_000,
            ),
        ),
        resource_samples=tuple(resource_samples),
    )


class RuntimeTargetHostMeasurementTests(unittest.TestCase):
    def _cut(self, journal, spec, current_plan):
        with patch(
            "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
            return_value=1_000_000_000,
        ):
            return begin_runtime_campaign(
                journal=journal,
                spec=spec,
                plan=current_plan,
            )

    def test_canonical_round_trip_recomputes_all_summaries(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            value = measurement(current_plan=current_plan, cut=cut)

            parsed = TargetHostMeasurementArtifact.parse(value.canonical_bytes())
            self.assertEqual(parsed.digest, value.digest)
            self.assertEqual(parsed.financial_event_ids, ("fin-1", "fin-2"))
            self.assertEqual(parsed.financial_latency_us, (100, 100))
            self.assertEqual(parsed.financial_staleness_us, (200, 200))
            self.assertEqual(parsed.research_interference_us, (50,))
            self.assertEqual(parsed.resource_metric_maxima["memory_rss_bytes"], 8192)
            self.assertEqual(parsed.resource_metric_maxima["queue_backlog"], 1)

    def test_serialized_derived_latency_cannot_be_forged(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            value = measurement(current_plan=current_plan, cut=cut)
            payload = json.loads(value.canonical_bytes().decode("utf-8"))
            payload["financial_samples"][0]["latency_us"] = 1
            raw = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "latency_us does not match",
            ):
                TargetHostMeasurementArtifact.parse(raw)

    def test_noncanonical_bytes_and_clock_reversal_fail_closed(self):
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementError,
            "clock moved backwards",
        ):
            FinancialTargetHostSample(
                sample_id="bad",
                event_id="fin-1",
                journal_sequence=1,
                latency_start_monotonic_ns=20,
                latency_end_monotonic_ns=19,
                staleness_source_monotonic_ns=10,
                staleness_observed_monotonic_ns=20,
            )

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            value = measurement(current_plan=current_plan, cut=cut)
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "not canonical JSON",
            ):
                TargetHostMeasurementArtifact.parse(value.canonical_bytes() + b"\n")

    def test_duplicate_financial_identity_is_rejected(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "unique event IDs",
            ):
                measurement(
                    current_plan=current_plan,
                    cut=cut,
                    event_ids=("fin-1", "fin-1"),
                )

    def test_resource_metric_key_drift_is_rejected(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            resources = (
                ResourceTargetHostSample(
                    sample_id="resource-1",
                    monotonic_ns=1_200_000_000,
                    phase="steady",
                    metrics={"memory_rss_bytes": 4096},
                ),
                ResourceTargetHostSample(
                    sample_id="resource-2",
                    monotonic_ns=1_300_000_000,
                    phase="pressure",
                    metrics={"memory_rss_bytes": 8192, "queue_backlog": 1},
                ),
            )
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "stable metric-key set",
            ):
                measurement(
                    current_plan=current_plan,
                    cut=cut,
                    resource_samples=resources,
                )

    def test_exact_raw_artifact_drives_existing_campaign_evidence(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            journal.append_event(envelope("fin-1"))
            journal.append_event(envelope("fin-2"))
            value = measurement(current_plan=current_plan, cut=cut)

            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_900_000_000,
            ):
                evidence = collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    measurement=value,
                    expected_release_artifact_id=RELEASE_ARTIFACT_ID,
                )

            self.assertEqual(evidence.financial_latency_us, (100, 100))
            self.assertEqual(evidence.financial_staleness_us, (200, 200))
            self.assertEqual(evidence.research_interference_us, (50,))
            self.assertEqual(evidence.resource_evidence_hash, value.digest)
            self.assertEqual(evidence.resource_metrics["memory_rss_bytes"], 8192)
            self.assertEqual(evidence.recovered_financial_event_ids, ("fin-1", "fin-2"))
            decision = evaluate_runtime_campaign(spec, evidence)
            self.assertEqual(decision.status, "INCONCLUSIVE")
            self.assertEqual(
                decision.reasons,
                ("unverified_runtime_measurement_provenance",),
            )

    def test_measurement_for_substituted_event_identity_cannot_pair_with_journal(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            journal.append_event(envelope("fin-1"))
            journal.append_event(envelope("fin-2"))
            value = measurement(
                current_plan=current_plan,
                cut=cut,
                event_ids=("fin-1", "substituted-fin"),
            )
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_900_000_000,
            ), self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "identities do not exactly match",
            ):
                collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    measurement=value,
                    expected_release_artifact_id=RELEASE_ARTIFACT_ID,
                )

    def test_measurement_cannot_reuse_another_delivered_artifact(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            value = measurement(
                current_plan=current_plan,
                cut=cut,
                release_artifact_sha256=ALT_RELEASE_ARTIFACT_SHA,
            )
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "release_artifact_sha256",
            ):
                collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    measurement=value,
                    expected_release_artifact_id=RELEASE_ARTIFACT_ID,
                )

    def test_low_level_minting_rejects_self_asserted_release_uuid(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            value = measurement(
                current_plan=current_plan,
                cut=cut,
                release_artifact_id=ALT_RELEASE_ARTIFACT_ID,
            )
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "another delivered release artifact",
            ):
                collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    measurement=value,
                    expected_release_artifact_id=RELEASE_ARTIFACT_ID,
                )

    def test_low_level_minting_rejects_pre_campaign_staleness_observation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1")
            cut = self._cut(journal, spec, current_plan)
            sample = FinancialTargetHostSample(
                sample_id="financial-pre-start",
                event_id="fin-1",
                journal_sequence=1,
                latency_start_monotonic_ns=1_100_000_000,
                latency_end_monotonic_ns=1_100_100_000,
                staleness_source_monotonic_ns=900_000_000,
                staleness_observed_monotonic_ns=999_999_999,
            )
            value = measurement(
                current_plan=current_plan,
                cut=cut,
                event_ids=("fin-1",),
                sequences=(1,),
                end_sequence=1,
                financial_samples=(sample,),
            )
            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "staleness observation occurs before campaign monotonic cut",
            ):
                collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    measurement=value,
                    expected_release_artifact_id=RELEASE_ARTIFACT_ID,
                )

    def test_frozen_measurement_end_cut_cannot_ignore_later_durable_activity(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1")
            cut = self._cut(journal, spec, current_plan)
            journal.append_event(envelope("fin-1"))
            value = measurement(
                current_plan=current_plan,
                cut=cut,
                event_ids=("fin-1",),
                sequences=(1,),
                end_sequence=1,
            )
            journal.append_event(envelope("control-1", aggregate_type="model_budget"))
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_900_000_000,
            ), self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "end cut does not match",
            ):
                collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    measurement=value,
                    expected_release_artifact_id=RELEASE_ARTIFACT_ID,
                )

    def test_sample_outside_campaign_monotonic_window_is_rejected(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = campaign_plan(spec, "fin-1", "fin-2")
            cut = self._cut(journal, spec, current_plan)
            journal.append_event(envelope("fin-1"))
            journal.append_event(envelope("fin-2"))
            late_resources = (
                ResourceTargetHostSample(
                    sample_id="resource-1",
                    monotonic_ns=1_200_000_000,
                    phase="steady",
                    metrics={"memory_rss_bytes": 4096},
                ),
                ResourceTargetHostSample(
                    sample_id="resource-2",
                    monotonic_ns=2_000_000_000,
                    phase="late",
                    metrics={"memory_rss_bytes": 8192},
                ),
            )
            value = measurement(
                current_plan=current_plan,
                cut=cut,
                resource_samples=late_resources,
            )
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_900_000_000,
            ), self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "resource sample lies outside",
            ):
                collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    measurement=value,
                    expected_release_artifact_id=RELEASE_ARTIFACT_ID,
                )


if __name__ == "__main__":
    unittest.main()
