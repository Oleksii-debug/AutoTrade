import unittest
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetError,
    RuntimeBudgetSpec,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
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
RESOURCE = "sha256:" + ("e" * 64)


def runtime_spec(*, samples: int = 2) -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-bounded-load",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=samples,
        min_research_samples=1,
    )


def envelope(
    event_id: str,
    *,
    aggregate_type: str = "financial",
    aggregate_id: str | None = None,
) -> dict[str, object]:
    payload = {"event_id": event_id, "kind": aggregate_type}
    return {
        "event_id": event_id,
        "event_type": "QualificationEvent",
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id or event_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-09-25T09:00:00+00:00",
    }


def plan(spec: RuntimeBudgetSpec, *event_ids: str) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1000,
        expected_financial_event_ids=event_ids,
        financial_aggregate_types=("financial",),
    )


class RuntimeLoadQualificationTests(unittest.TestCase):
    def test_campaign_derives_event_conservation_from_journal_and_reopens_identically(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = JournalStore(path)
            journal.append_event(envelope("pre-existing", aggregate_type="research"))

            spec = runtime_spec()
            current_plan = plan(spec, "fin-1", "fin-2")
            cut = begin_runtime_campaign(
                journal=journal,
                spec=spec,
                plan=current_plan,
                monotonic_ns=lambda: 1_000_000_000,
            )

            journal.append_event(envelope("fin-1"))
            journal.append_event(envelope("research-1", aggregate_type="research"))
            journal.append_event(envelope("fin-2"))

            kwargs = {
                "spec": spec,
                "plan": current_plan,
                "cut": cut,
                "financial_latency_us": (100, 120),
                "financial_staleness_us": (80, 90),
                "research_interference_us": (50,),
                "resource_evidence_hash": RESOURCE,
                "resource_metrics": {
                    "cpu_peak_millis": 800,
                    "memory_peak_bytes": 1024,
                    "disk_fsync_p95_us": 200,
                },
                "monotonic_ns": lambda: 1_900_000_000,
            }
            first = collect_runtime_campaign_evidence(journal=journal, **kwargs)
            self.assertEqual(first.start_journal_sequence, 1)
            self.assertEqual(first.end_journal_sequence, 4)
            self.assertEqual(first.recovered_financial_event_ids, ("fin-1", "fin-2"))
            self.assertEqual(evaluate_runtime_campaign(spec, first).status, "PASS")

            reopened = JournalStore(path)
            second = collect_runtime_campaign_evidence(journal=reopened, **kwargs)
            self.assertEqual(second.digest, first.digest)
            self.assertEqual(
                second.recovered_financial_event_bindings,
                first.recovered_financial_event_bindings,
            )

    def test_campaign_monotonic_duration_enforces_declared_throughput(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(samples=1)
            current_plan = plan(spec, "fin-1")
            cut = begin_runtime_campaign(
                journal=journal,
                spec=spec,
                plan=current_plan,
                monotonic_ns=lambda: 10_000_000_000,
            )
            journal.append_event(envelope("fin-1"))

            evidence = collect_runtime_campaign_evidence(
                journal=journal,
                spec=spec,
                plan=current_plan,
                cut=cut,
                financial_latency_us=(100,),
                financial_staleness_us=(80,),
                research_interference_us=(50,),
                resource_evidence_hash=RESOURCE,
                resource_metrics={"cpu_peak_millis": 500},
                monotonic_ns=lambda: 11_100_000_000,
            )
            self.assertEqual(evidence.declared_duration_us, 1_000_000)
            self.assertEqual(evidence.observed_duration_us, 1_100_000)
            decision = evaluate_runtime_campaign(spec, evidence)
            self.assertEqual(decision.status, "FAIL")
            self.assertIn("declared_throughput_not_met", decision.reasons)

    def test_campaign_rejects_monotonic_clock_regression(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(samples=1)
            current_plan = plan(spec, "fin-1")
            cut = begin_runtime_campaign(
                journal=journal,
                spec=spec,
                plan=current_plan,
                monotonic_ns=lambda: 2_000,
            )
            journal.append_event(envelope("fin-1"))
            with self.assertRaisesRegex(RuntimeBudgetError, "moved backwards"):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    financial_latency_us=(100,),
                    financial_staleness_us=(80,),
                    research_interference_us=(50,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 500},
                    monotonic_ns=lambda: 1_999,
                )

    def test_missing_expected_financial_event_is_a_budget_failure_not_a_caller_count(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = plan(spec, "fin-1", "fin-2")
            cut = begin_runtime_campaign(journal=journal, spec=spec, plan=current_plan)
            journal.append_event(envelope("fin-1"))

            evidence = collect_runtime_campaign_evidence(
                journal=journal,
                spec=spec,
                plan=current_plan,
                cut=cut,
                financial_latency_us=(100, 120),
                financial_staleness_us=(80, 90),
                research_interference_us=(50,),
                resource_evidence_hash=RESOURCE,
                resource_metrics={"cpu_peak_millis": 500},
            )
            decision = evaluate_runtime_campaign(spec, evidence)
            self.assertEqual(decision.status, "FAIL")
            self.assertIn("financial_event_loss", decision.reasons)
            self.assertEqual(decision.metrics["expected_financial_events"], 2)
            self.assertEqual(decision.metrics["recovered_financial_events"], 1)

    def test_undeclared_financial_event_cannot_be_hidden_from_campaign_cut(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            current_plan = plan(spec, "fin-1")
            cut = begin_runtime_campaign(journal=journal, spec=spec, plan=current_plan)
            journal.append_event(envelope("fin-1"))
            journal.append_event(envelope("fin-extra"))

            with self.assertRaisesRegex(
                RuntimeBudgetError,
                "undeclared financial event identities",
            ):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    financial_latency_us=(100, 120),
                    financial_staleness_us=(80, 90),
                    research_interference_us=(50,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 500},
                )

    def test_pending_outbox_keeps_reconnect_qualification_failed(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(samples=1)
            current_plan = plan(spec, "fin-1")
            cut = begin_runtime_campaign(journal=journal, spec=spec, plan=current_plan)
            journal.append_event(envelope("fin-1"), outbox_topic="financial.events")

            evidence = collect_runtime_campaign_evidence(
                journal=journal,
                spec=spec,
                plan=current_plan,
                cut=cut,
                financial_latency_us=(100,),
                financial_staleness_us=(80,),
                research_interference_us=(50,),
                resource_evidence_hash=RESOURCE,
                resource_metrics={"cpu_peak_millis": 500},
            )
            decision = evaluate_runtime_campaign(spec, evidence)
            self.assertEqual(decision.status, "FAIL")
            self.assertIn("reconnect_backlog_not_drained", decision.reasons)
            self.assertEqual(evidence.reconnect_backlog_remaining, 1)

    def test_plan_factory_rejects_scalar_text_as_event_or_aggregate_collection(self):
        spec = runtime_spec(samples=1)
        with self.assertRaisesRegex(RuntimeBudgetError, "expected_financial_event_ids"):
            RuntimeCampaignPlan.create(
                spec=spec,
                workload_profile_hash=WORKLOAD,
                declared_duration_ms=1000,
                expected_financial_event_ids="fin-1",
                financial_aggregate_types=("financial",),
            )
        with self.assertRaisesRegex(RuntimeBudgetError, "financial_aggregate_types"):
            RuntimeCampaignPlan.create(
                spec=spec,
                workload_profile_hash=WORKLOAD,
                declared_duration_ms=1000,
                expected_financial_event_ids=("fin-1",),
                financial_aggregate_types="financial",
            )

    def test_campaign_evidence_cannot_be_directly_self_asserted(self):
        spec = runtime_spec(samples=1)
        with self.assertRaisesRegex(
            RuntimeBudgetError,
            "must be derived from a JournalStore cut",
        ):
            RuntimeCampaignEvidence(
                plan_digest="sha256:" + ("f" * 64),
                spec_digest=spec.digest,
                release_sha=spec.release_sha,
                configuration_hash=spec.configuration_hash,
                host_fingerprint=spec.host_fingerprint,
                declared_duration_us=1_000_000,
                observed_duration_us=900_000,
                start_journal_sequence=0,
                end_journal_sequence=0,
                expected_financial_event_ids=("fin-1",),
                recovered_financial_event_bindings=(),
                financial_latency_us=(100,),
                financial_staleness_us=(80,),
                research_interference_us=(50,),
                reconnect_backlog_remaining=0,
                resource_evidence_hash=RESOURCE,
                resource_metrics={"cpu_peak_millis": 500},
            )


if __name__ == "__main__":
    unittest.main()
