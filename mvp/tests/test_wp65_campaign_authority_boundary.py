import unittest
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetError, RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignCut,
    RuntimeCampaignEvidence,
    RuntimeCampaignPlan,
    begin_runtime_campaign,
    collect_runtime_campaign_evidence,
    evaluate_runtime_campaign,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
WORKLOAD = "sha256:" + ("d" * 64)


def runtime_spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-authority-boundary",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def runtime_plan(spec: RuntimeBudgetSpec) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1000,
        expected_financial_event_ids=("financial-1",),
        financial_aggregate_types=("risk_decision",),
    )


class JournalStoreSubclass(JournalStore):
    pass


class RuntimeBudgetSpecSubclass(RuntimeBudgetSpec):
    pass


class RuntimeCampaignPlanSubclass(RuntimeCampaignPlan):
    pass


class RuntimeCampaignCutSubclass(RuntimeCampaignCut):
    pass


class RuntimeCampaignEvidenceSubclass(RuntimeCampaignEvidence):
    pass


class RuntimeCampaignAuthorityBoundaryTests(unittest.TestCase):
    def test_begin_rejects_polymorphic_journal_store_before_dispatch(self):
        with TemporaryDirectory() as directory:
            journal = JournalStoreSubclass(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=runtime_plan(spec),
                )

    def test_begin_rejects_instance_method_shadow_before_dispatch(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            journal.current_journal_sequence = lambda: 0
            spec = runtime_spec()
            with self.assertRaisesRegex(TypeError, "instance state is shadowed"):
                begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=runtime_plan(spec),
                )

    def test_plan_factory_rejects_polymorphic_budget_spec(self):
        exact = runtime_spec()
        polymorphic = RuntimeBudgetSpecSubclass(
            scenario_id=exact.scenario_id,
            release_sha=exact.release_sha,
            configuration_hash=exact.configuration_hash,
            host_fingerprint=exact.host_fingerprint,
            strategy_horizon_us=exact.strategy_horizon_us,
            max_p95_financial_latency_us=exact.max_p95_financial_latency_us,
            max_financial_staleness_us=exact.max_financial_staleness_us,
            max_research_interference_us=exact.max_research_interference_us,
            min_financial_samples=exact.min_financial_samples,
            min_research_samples=exact.min_research_samples,
        )
        with self.assertRaisesRegex(TypeError, "spec must be RuntimeBudgetSpec"):
            runtime_plan(polymorphic)

    def test_begin_rejects_polymorphic_campaign_plan(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            exact = runtime_plan(spec)
            polymorphic = RuntimeCampaignPlanSubclass(
                scenario_id=exact.scenario_id,
                spec_digest=exact.spec_digest,
                release_sha=exact.release_sha,
                configuration_hash=exact.configuration_hash,
                host_fingerprint=exact.host_fingerprint,
                workload_profile_hash=exact.workload_profile_hash,
                declared_duration_ms=exact.declared_duration_ms,
                expected_financial_event_ids=exact.expected_financial_event_ids,
                financial_aggregate_types=exact.financial_aggregate_types,
                release_artifact_id=exact.release_artifact_id,
                release_artifact_sha256=exact.release_artifact_sha256,
                journal_taxonomy_digest=exact.journal_taxonomy_digest,
            )
            with self.assertRaisesRegex(TypeError, "exact RuntimeCampaignPlan"):
                begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=polymorphic,
                )

    def test_collect_rejects_polymorphic_campaign_cut_before_read(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            plan = runtime_plan(spec)
            exact = begin_runtime_campaign(journal=journal, spec=spec, plan=plan)
            polymorphic = object.__new__(RuntimeCampaignCutSubclass)
            object.__setattr__(polymorphic, "plan_digest", exact.plan_digest)
            object.__setattr__(polymorphic, "spec_digest", exact.spec_digest)
            object.__setattr__(
                polymorphic,
                "start_journal_sequence",
                exact.start_journal_sequence,
            )
            object.__setattr__(
                polymorphic,
                "started_monotonic_ns",
                exact.started_monotonic_ns,
            )
            with self.assertRaisesRegex(TypeError, "exact RuntimeCampaignCut"):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=plan,
                    cut=polymorphic,
                    financial_latency_us=(),
                    financial_staleness_us=(),
                    research_interference_us=(1,),
                    resource_evidence_hash="sha256:" + ("e" * 64),
                    resource_metrics={"cpu_peak_millis": 1},
                )

    def test_evaluate_rejects_polymorphic_campaign_evidence_before_use(self):
        polymorphic = object.__new__(RuntimeCampaignEvidenceSubclass)
        with self.assertRaisesRegex(
            TypeError,
            "exact RuntimeCampaignEvidence",
        ):
            evaluate_runtime_campaign(runtime_spec(), polymorphic)

    def test_begin_revalidates_post_construction_plan_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            plan = runtime_plan(spec)
            object.__setattr__(plan, "declared_duration_ms", 0)
            with self.assertRaisesRegex(RuntimeBudgetError, "declared_duration_ms"):
                begin_runtime_campaign(journal=journal, spec=spec, plan=plan)

    def test_collect_revalidates_post_construction_cut_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            plan = runtime_plan(spec)
            cut = begin_runtime_campaign(journal=journal, spec=spec, plan=plan)
            object.__setattr__(cut, "start_journal_sequence", -1)
            with self.assertRaisesRegex(RuntimeBudgetError, "start_journal_sequence"):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=plan,
                    cut=cut,
                    financial_latency_us=(),
                    financial_staleness_us=(),
                    research_interference_us=(1,),
                    resource_evidence_hash="sha256:" + ("e" * 64),
                    resource_metrics={"cpu_peak_millis": 1},
                )

    def test_evaluate_revalidates_post_construction_evidence_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            plan = runtime_plan(spec)
            cut = begin_runtime_campaign(journal=journal, spec=spec, plan=plan)
            evidence = collect_runtime_campaign_evidence(
                journal=journal,
                spec=spec,
                plan=plan,
                cut=cut,
                financial_latency_us=(),
                financial_staleness_us=(),
                research_interference_us=(1,),
                resource_evidence_hash="sha256:" + ("e" * 64),
                resource_metrics={"cpu_peak_millis": 1},
            )
            object.__setattr__(evidence, "reconnect_backlog_remaining", -1)
            with self.assertRaisesRegex(RuntimeBudgetError, "reconnect_backlog_remaining"):
                evaluate_runtime_campaign(spec, evidence)


if __name__ == "__main__":
    unittest.main()
