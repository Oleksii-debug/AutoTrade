from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as terminal_module
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
WORKLOAD = "sha256:" + "d" * 64
RELEASE_ID = "60000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "e" * 64


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="plan-bound-cut-snapshot",
        release_sha=SOURCE_SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _event() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="cut-snapshot-financial-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="plan-bound-cut-snapshot",
        aggregate_version=1,
    )


class RuntimeTargetHostPlanBoundCutSnapshotTests(unittest.TestCase):
    def _prepared(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = JournalStore(Path(temporary.name) / "journal.sqlite3")
        spec = _spec()
        event = _event()
        declared = declare_runtime_event_plan(
            store,
            plan_id="cut-snapshot-plan",
            spec=spec,
            expected_events=(event,),
        )
        campaign_plan = RuntimeCampaignPlan.create(
            spec=spec,
            workload_profile_hash=WORKLOAD,
            declared_duration_ms=1_000,
            expected_financial_event_ids=(event.event_id,),
            financial_aggregate_types=("risk_decision",),
            release_artifact_sha256=RELEASE_SHA,
        )
        with patch(
            "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
            return_value=1_000_000_000,
        ):
            cut = begin_runtime_campaign(
                journal=store,
                spec=spec,
                plan=campaign_plan,
            )
        return temporary.name, store, spec, declared, campaign_plan, cut

    def test_caller_cut_mutation_after_terminal_snapshot_cannot_change_composed_cut(self):
        root, store, spec, declared, campaign_plan, cut = self._prepared()
        original_started = cut.started_monotonic_ns
        accepted = object()

        def composed(receipt, **kwargs):
            detached = kwargs["campaign_cut"]
            self.assertIsNot(detached, cut)
            object.__setattr__(cut, "started_monotonic_ns", 1_900_000_000)
            self.assertEqual(detached.started_monotonic_ns, original_started)
            return accepted

        with patch.object(
            terminal_module,
            "verify_composed_runtime_target_host_qualification",
            side_effect=composed,
        ) as verifier:
            result = terminal_module.verify_declared_plan_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root=root,
                journal_store=store,
                plan_id=declared.plan_id,
                spec=spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=campaign_plan,
                campaign_cut=cut,
                measurement=object(),
            )

        self.assertIs(result, accepted)
        verifier.assert_called_once()
        self.assertNotEqual(cut.started_monotonic_ns, original_started)

    def test_post_issuance_executable_cut_field_fails_before_composed_dispatch(self):
        root, store, spec, declared, campaign_plan, cut = self._prepared()

        class ExecutableStarted:
            def __lt__(self, other):
                raise AssertionError("executable campaign cut field must not run")

        object.__setattr__(cut, "started_monotonic_ns", ExecutableStarted())
        with patch.object(
            terminal_module,
            "verify_composed_runtime_target_host_qualification",
        ) as verifier, self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "started_monotonic_ns must remain a non-negative integer",
        ):
            terminal_module.verify_declared_plan_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root=root,
                journal_store=store,
                plan_id=declared.plan_id,
                spec=spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=campaign_plan,
                campaign_cut=cut,
                measurement=object(),
            )
        verifier.assert_not_called()


if __name__ == "__main__":
    unittest.main()
