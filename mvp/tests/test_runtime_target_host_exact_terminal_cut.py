import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResearchInterferenceSample,
    ResourceTargetHostSample,
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    collect_runtime_campaign_evidence_from_measurement_artifact,
)


class RuntimeTargetHostExactTerminalCutTests(unittest.TestCase):
    def test_sub_microsecond_post_cut_resource_sample_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = RuntimeBudgetSpec(
                scenario_id="wp65-exact-terminal-cut",
                release_sha="a" * 40,
                configuration_hash="sha256:" + ("b" * 64),
                host_fingerprint="sha256:" + ("c" * 64),
                strategy_horizon_us=1_000_000,
                max_p95_financial_latency_us=500,
                max_financial_staleness_us=500,
                max_research_interference_us=500,
                min_financial_samples=1,
                min_research_samples=1,
            )
            plan = RuntimeCampaignPlan.create(
                spec=spec,
                workload_profile_hash="sha256:" + ("d" * 64),
                declared_duration_ms=1_000,
                expected_financial_event_ids=("fin-1",),
                financial_aggregate_types=("risk_decision",),
                release_artifact_sha256="sha256:" + ("e" * 64),
            )
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_000_000,
            ):
                cut = begin_runtime_campaign(journal=journal, spec=spec, plan=plan)

            payload = {"event_id": "fin-1", "kind": "risk_decision"}
            journal.append_event(
                {
                    "event_id": "fin-1",
                    "event_type": "QualificationEvent",
                    "aggregate_type": "risk_decision",
                    "aggregate_id": "fin-1",
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-10-03T07:00:00+00:00",
                }
            )
            measurement = TargetHostMeasurementArtifact(
                source_sha=spec.release_sha,
                release_artifact_id="40000000-0000-4000-8000-000000000001",
                release_artifact_sha256=plan.release_artifact_sha256,
                scenario_id=spec.scenario_id,
                spec_digest=spec.digest,
                configuration_hash=spec.configuration_hash,
                host_fingerprint=spec.host_fingerprint,
                workload_profile_hash=plan.workload_profile_hash,
                plan_digest=plan.digest,
                journal_taxonomy_digest=plan.journal_taxonomy_digest,
                journal_store_identity_digest=cut.journal_store_identity_digest,
                start_journal_sequence=cut.start_journal_sequence,
                end_journal_sequence=1,
                monotonic_clock_id="python-time.monotonic_ns",
                staleness_basis="host-monotonic-financial-state-age",
                research_interference_basis="host-monotonic-contention-delay",
                financial_samples=(
                    FinancialTargetHostSample(
                        sample_id="financial-1",
                        event_id="fin-1",
                        journal_sequence=1,
                        latency_start_monotonic_ns=1_100_000_000,
                        latency_end_monotonic_ns=1_100_000_100,
                        staleness_source_monotonic_ns=1_099_999_900,
                        staleness_observed_monotonic_ns=1_100_000_000,
                    ),
                ),
                research_samples=(
                    ResearchInterferenceSample(
                        sample_id="research-1",
                        phase="contention",
                        start_monotonic_ns=1_500_000_000,
                        end_monotonic_ns=1_500_000_050,
                    ),
                ),
                resource_samples=(
                    ResourceTargetHostSample(
                        sample_id="resource-post-cut",
                        monotonic_ns=1_900_000_500,
                        phase="post-cut",
                        metrics={"memory_rss_bytes": 4096},
                    ),
                ),
            )

            # The exact terminal sample is 1_900_000_001ns. The old rounded
            # microsecond reconstruction widened the accepted window through
            # 1_900_001_000ns and therefore admitted this post-cut telemetry.
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_900_000_001,
            ), self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "resource sample lies outside campaign monotonic cut",
            ):
                collect_runtime_campaign_evidence_from_measurement_artifact(
                    journal=journal,
                    spec=spec,
                    plan=plan,
                    cut=cut,
                    measurement=measurement,
                )


if __name__ == "__main__":
    unittest.main()
