from copy import copy as real_copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

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
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResearchInterferenceSample,
    ResourceTargetHostSample,
    TargetHostMeasurementArtifact,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
OTHER_WORKLOAD = "sha256:" + "d" * 64
RELEASE_ID = "60000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "e" * 64
JOURNAL = "sha256:" + "f" * 64


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


def _measurement(
    spec: RuntimeBudgetSpec,
    declared,
    campaign_plan: RuntimeCampaignPlan,
) -> TargetHostMeasurementArtifact:
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE_SHA,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=declared.digest,
        plan_digest=campaign_plan.digest,
        journal_taxonomy_digest=campaign_plan.journal_taxonomy_digest,
        journal_store_identity_digest=JOURNAL,
        start_journal_sequence=0,
        end_journal_sequence=1,
        monotonic_clock_id="python-time.monotonic_ns",
        staleness_basis="host-monotonic-financial-state-age",
        research_interference_basis="host-monotonic-contention-delay",
        financial_samples=(
            FinancialTargetHostSample(
                sample_id="financial-1",
                event_id="cut-snapshot-financial-1",
                journal_sequence=1,
                latency_start_monotonic_ns=1_100_000_000,
                latency_end_monotonic_ns=1_100_100_000,
                staleness_source_monotonic_ns=1_799_900_000,
                staleness_observed_monotonic_ns=1_800_000_000,
            ),
        ),
        research_samples=(
            ResearchInterferenceSample(
                sample_id="research-1",
                phase="contention",
                start_monotonic_ns=1_900_000_000,
                end_monotonic_ns=1_900_050_000,
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
            workload_profile_hash=declared.digest,
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

        composed_verifier = Mock(side_effect=composed)
        verifier = terminal_module._build_chronology_free_verifier(
            verify_composed=composed_verifier,
        )
        result = verifier(
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
        composed_verifier.assert_called_once()
        self.assertNotEqual(cut.started_monotonic_ns, original_started)

    def test_cut_mutation_during_copy_is_validated_on_detached_snapshot(self):
        root, store, spec, declared, campaign_plan, cut = self._prepared()

        class ExecutableStarted:
            pass

        hostile = ExecutableStarted()

        def mutating_copy(value):
            object.__setattr__(cut, "started_monotonic_ns", hostile)
            return real_copy(value)

        composed_verifier = Mock()
        verifier = terminal_module._build_chronology_free_verifier(
            snapshot_campaign_cut=lambda value: terminal_module._snapshot_campaign_cut(
                value,
                _copy=mutating_copy,
            ),
            verify_composed=composed_verifier,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "started_monotonic_ns must remain a non-negative integer",
        ):
            verifier(
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
        composed_verifier.assert_not_called()

    def test_post_issuance_executable_cut_field_fails_before_composed_dispatch(self):
        root, store, spec, declared, campaign_plan, cut = self._prepared()

        class ExecutableStarted:
            def __lt__(self, other):
                raise AssertionError("executable campaign cut field must not run")

        object.__setattr__(cut, "started_monotonic_ns", ExecutableStarted())
        composed_verifier = Mock()
        verifier = terminal_module._build_chronology_free_verifier(
            verify_composed=composed_verifier,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "started_monotonic_ns must remain a non-negative integer",
        ):
            verifier(
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
        composed_verifier.assert_not_called()

    def test_campaign_workload_substitution_fails_before_composed_dispatch(self):
        root, store, spec, declared, campaign_plan, cut = self._prepared()
        self.assertNotEqual(OTHER_WORKLOAD, declared.digest)
        substituted = RuntimeCampaignPlan.create(
            spec=spec,
            workload_profile_hash=OTHER_WORKLOAD,
            declared_duration_ms=campaign_plan.declared_duration_ms,
            expected_financial_event_ids=campaign_plan.expected_financial_event_ids,
            financial_aggregate_types=campaign_plan.financial_aggregate_types,
            release_artifact_sha256=campaign_plan.release_artifact_sha256,
        )
        composed_verifier = Mock()
        verifier = terminal_module._build_chronology_free_verifier(
            verify_composed=composed_verifier,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "campaign workload identity does not match durable pre-run plan",
        ):
            verifier(
                object(),
                evidence_store=object(),
                evidence_root=root,
                journal_store=store,
                plan_id=declared.plan_id,
                spec=spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=substituted,
                campaign_cut=cut,
                measurement=object(),
            )
        composed_verifier.assert_not_called()

    def test_executable_campaign_workload_field_is_never_compared(self):
        root, store, spec, declared, campaign_plan, cut = self._prepared()

        class ExecutableHash:
            invoked = False

            def __eq__(self, other):
                self.invoked = True
                raise AssertionError("campaign workload equality executed caller code")

            def __ne__(self, other):
                self.invoked = True
                raise AssertionError("campaign workload inequality executed caller code")

        hostile = ExecutableHash()
        object.__setattr__(campaign_plan, "workload_profile_hash", hostile)
        composed_verifier = Mock()
        verifier = terminal_module._build_chronology_free_verifier(
            verify_composed=composed_verifier,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "campaign workload identity must remain exact canonical sha256 text",
        ):
            verifier(
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
        self.assertFalse(hostile.invoked)
        composed_verifier.assert_not_called()

    def test_executable_measurement_workload_field_is_never_compared(self):
        root, store, spec, declared, campaign_plan, cut = self._prepared()
        current_measurement = _measurement(spec, declared, campaign_plan)

        class ExecutableHash:
            invoked = False

            def __eq__(self, other):
                self.invoked = True
                raise AssertionError("measurement workload equality executed caller code")

            def __ne__(self, other):
                self.invoked = True
                raise AssertionError("measurement workload inequality executed caller code")

        hostile = ExecutableHash()
        object.__setattr__(current_measurement, "workload_profile_hash", hostile)
        composed_verifier = Mock()
        verifier = terminal_module._build_chronology_free_verifier(
            verify_composed=composed_verifier,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "measurement workload identity must remain exact canonical sha256 text",
        ):
            verifier(
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
                measurement=current_measurement,
            )
        self.assertFalse(hostile.invoked)
        composed_verifier.assert_not_called()

    def test_production_snapshot_helpers_ignore_rebound_globals(self):
        _, _, spec, _, _, cut = self._prepared()
        forged_copy = Mock(side_effect=AssertionError("rebound copy ran"))
        forged_error = type("ForgedCompositionError", (Exception,), {})

        with (
            patch.object(terminal_module, "RuntimeBudgetSpec", object),
            patch.object(terminal_module, "RuntimeCampaignCut", object),
            patch.object(terminal_module, "RuntimeTargetHostCompositionError", forged_error),
            patch.object(terminal_module, "copy", forged_copy),
        ):
            detached_spec = terminal_module._snapshot_budget_spec(spec)
            detached_cut = terminal_module._snapshot_campaign_cut(cut)
            with self.assertRaises(RuntimeTargetHostCompositionError):
                terminal_module._canonical_sha256_text(
                    "not-a-digest",
                    name="test digest",
                )

        self.assertIs(type(detached_spec), RuntimeBudgetSpec)
        self.assertEqual(detached_spec.digest, spec.digest)
        self.assertIsNot(detached_cut, cut)
        self.assertEqual(detached_cut.plan_digest, cut.plan_digest)
        forged_copy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
