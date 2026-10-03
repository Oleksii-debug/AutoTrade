from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResourceTargetHostSample,
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
)
from mvp.autotrade_mvp.runtime_target_host_measurement_authority import (
    collect_release_bound_target_host_evidence,
)


SOURCE = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
WORKLOAD = "sha256:" + "d" * 64
RELEASE_ID = "40000000-0000-4000-8000-000000000001"
OTHER_RELEASE_ID = "40000000-0000-4000-8000-000000000002"
RELEASE_SHA = "sha256:" + "e" * 64


def spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-authority-guard",
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def plan(value: RuntimeBudgetSpec) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=value,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1_000,
        expected_financial_event_ids=("fin-1",),
        financial_aggregate_types=("risk_decision",),
        release_artifact_sha256=RELEASE_SHA,
    )


def artifact(current_plan, cut, *, observed_ns=1_100_000_000):
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id=current_plan.scenario_id,
        spec_digest=current_plan.spec_digest,
        configuration_hash=current_plan.configuration_hash,
        host_fingerprint=current_plan.host_fingerprint,
        workload_profile_hash=current_plan.workload_profile_hash,
        plan_digest=current_plan.digest,
        journal_taxonomy_digest=current_plan.journal_taxonomy_digest,
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
                latency_end_monotonic_ns=1_100_100_000,
                staleness_source_monotonic_ns=observed_ns - 200_000,
                staleness_observed_monotonic_ns=observed_ns,
            ),
        ),
        research_samples=(),
        resource_samples=(
            ResourceTargetHostSample(
                sample_id="resource-1",
                monotonic_ns=1_200_000_000,
                phase="steady",
                metrics={"memory_rss_bytes": 4096},
            ),
        ),
    )


class RuntimeTargetHostMeasurementAuthorityTests(unittest.TestCase):
    def _context(self):
        temporary = TemporaryDirectory()
        journal = JournalStore(f"{temporary.name}/journal.sqlite3")
        current_spec = spec()
        current_plan = plan(current_spec)
        with patch(
            "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
            return_value=1_000_000_000,
        ):
            cut = begin_runtime_campaign(
                journal=journal,
                spec=current_spec,
                plan=current_plan,
            )
        return temporary, journal, current_spec, current_plan, cut

    def test_external_release_uuid_must_match_before_mechanics_dispatch(self):
        temporary, journal, current_spec, current_plan, cut = self._context()
        self.addCleanup(temporary.cleanup)
        value = artifact(current_plan, cut)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_measurement_authority."
            "collect_runtime_campaign_evidence_from_measurement_artifact"
        ) as mechanics, self.assertRaisesRegex(
            RuntimeTargetHostMeasurementError,
            "another delivered release artifact",
        ):
            collect_release_bound_target_host_evidence(
                journal=journal,
                spec=current_spec,
                plan=current_plan,
                cut=cut,
                measurement=value,
                expected_release_artifact_id=OTHER_RELEASE_ID,
            )
        mechanics.assert_not_called()

    def test_staleness_observation_before_campaign_cut_fails_closed(self):
        temporary, journal, current_spec, current_plan, cut = self._context()
        self.addCleanup(temporary.cleanup)
        value = artifact(current_plan, cut, observed_ns=999_900_000)
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_measurement_authority."
            "collect_runtime_campaign_evidence_from_measurement_artifact"
        ) as mechanics, self.assertRaisesRegex(
            RuntimeTargetHostMeasurementError,
            "staleness observation occurs before",
        ):
            collect_release_bound_target_host_evidence(
                journal=journal,
                spec=current_spec,
                plan=current_plan,
                cut=cut,
                measurement=value,
                expected_release_artifact_id=RELEASE_ID,
            )
        mechanics.assert_not_called()

    def test_valid_external_authority_snapshots_then_delegates(self):
        temporary, journal, current_spec, current_plan, cut = self._context()
        self.addCleanup(temporary.cleanup)
        value = artifact(current_plan, cut)
        sentinel = object()
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_measurement_authority."
            "collect_runtime_campaign_evidence_from_measurement_artifact",
            return_value=sentinel,
        ) as mechanics:
            result = collect_release_bound_target_host_evidence(
                journal=journal,
                spec=current_spec,
                plan=current_plan,
                cut=cut,
                measurement=value,
                expected_release_artifact_id=RELEASE_ID,
                max_events=123,
            )
        self.assertIs(result, sentinel)
        mechanics.assert_called_once()
        kwargs = mechanics.call_args.kwargs
        self.assertIs(kwargs["journal"], journal)
        self.assertIs(kwargs["spec"], current_spec)
        self.assertIs(kwargs["plan"], current_plan)
        self.assertIs(kwargs["cut"], cut)
        self.assertEqual(kwargs["measurement"], value)
        self.assertIsNot(kwargs["measurement"], value)
        self.assertEqual(kwargs["max_events"], 123)


if __name__ == "__main__":
    unittest.main()
