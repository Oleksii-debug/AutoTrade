from copy import copy as real_copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from mvp.autotrade_mvp import runtime_target_host_composed_qualification as composed_module
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
    verify_composed_runtime_target_host_qualification,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResearchInterferenceSample,
    ResourceTargetHostSample,
    TargetHostMeasurementArtifact,
)


SOURCE = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
WORKLOAD = "sha256:" + "d" * 64
JOURNAL = "sha256:" + "e" * 64
RELEASE_ID = "50000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "f" * 64


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-composed-cut",
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


def _plan(spec: RuntimeBudgetSpec) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1_000,
        expected_financial_event_ids=("event-1",),
        financial_aggregate_types=("risk_decision",),
        release_artifact_sha256=RELEASE_SHA,
    )


def _measurement(
    spec: RuntimeBudgetSpec,
    plan: RuntimeCampaignPlan,
) -> TargetHostMeasurementArtifact:
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=WORKLOAD,
        plan_digest=plan.digest,
        journal_taxonomy_digest=plan.journal_taxonomy_digest,
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
                metrics={"memory_rss_bytes": 4096},
            ),
        ),
    )


class RuntimeTargetHostComposedCutSnapshotTests(unittest.TestCase):
    def _prepared(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = JournalStore(Path(temporary.name) / "journal.sqlite3")
        spec = _spec()
        plan = _plan(spec)
        cut = begin_runtime_campaign(journal=store, spec=spec, plan=plan)
        return store, spec, plan, cut, _measurement(spec, plan)

    def test_direct_composed_entry_detaches_cut_before_durable_dispatch(self) -> None:
        store, spec, plan, cut, measurement = self._prepared()
        original_started = cut.started_monotonic_ns

        def stop_after_snapshot(**kwargs):
            detached = kwargs["campaign_cut"]
            self.assertIsNot(detached, cut)
            object.__setattr__(cut, "started_monotonic_ns", original_started + 10_000)
            self.assertEqual(detached.started_monotonic_ns, original_started)
            raise RuntimeError("stop-after-cut-snapshot")

        with patch.object(
            composed_module,
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
            side_effect=stop_after_snapshot,
        ) as durable, self.assertRaisesRegex(
            RuntimeError,
            "stop-after-cut-snapshot",
        ):
            verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=store,
                spec=spec,
                campaign_plan=plan,
                campaign_cut=cut,
                declared_plan_id="plan-1",
                measurement=measurement,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        durable.assert_called_once()
        self.assertNotEqual(cut.started_monotonic_ns, original_started)

    def test_direct_composed_entry_validates_cut_mutated_during_copy(self) -> None:
        store, spec, plan, cut, measurement = self._prepared()

        class ExecutableStarted:
            pass

        hostile = ExecutableStarted()

        def mutating_copy(value):
            object.__setattr__(cut, "started_monotonic_ns", hostile)
            return real_copy(value)

        with patch.object(
            composed_module,
            "copy",
            side_effect=mutating_copy,
        ), patch.object(
            composed_module,
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
        ) as durable, self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "started_monotonic_ns must remain a non-negative integer",
        ):
            verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=store,
                spec=spec,
                campaign_plan=plan,
                campaign_cut=cut,
                declared_plan_id="plan-1",
                measurement=measurement,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        durable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
