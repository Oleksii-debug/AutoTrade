import unittest
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetError,
    RuntimeBudgetSpec,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
    collect_runtime_campaign_evidence,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
WORKLOAD = "sha256:" + ("d" * 64)
RESOURCE = "sha256:" + ("e" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-store-authority",
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


def _plan(spec: RuntimeBudgetSpec) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1,
        expected_financial_event_ids=("fin-expected",),
        financial_aggregate_types=("risk_decision",),
    )


def _envelope(event_id: str) -> dict[str, object]:
    payload = {"event_id": event_id, "kind": "risk_decision"}
    return {
        "event_id": event_id,
        "event_type": "QualificationEvent",
        "aggregate_type": "risk_decision",
        "aggregate_id": event_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-03T04:45:00+00:00",
    }


class _ForgedJournalStore(JournalStore):
    def current_journal_sequence(self) -> int:
        return 0

    def pending_outbox_count(self) -> int:
        return 0

    def load_events_after_journal_sequence(self, *args, **kwargs):
        return []


class RuntimeLoadQualificationStoreAuthorityTests(unittest.TestCase):
    def test_campaign_rejects_journal_store_subclass_authority(self):
        with TemporaryDirectory() as directory:
            journal = _ForgedJournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)
            clock_called = False

            def clock() -> int:
                nonlocal clock_called
                clock_called = True
                return 1_000_000_000

            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    monotonic_ns=clock,
                )
            self.assertFalse(clock_called)

    def test_terminal_clock_shadow_cannot_hide_undeclared_financial_event(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)
            cut = begin_runtime_campaign(
                journal=journal,
                spec=spec,
                plan=current_plan,
                monotonic_ns=lambda: 1_000_000_000,
            )
            journal.append_event(_envelope("fin-expected"))
            journal.append_event(_envelope("fin-extra"))

            def ending_clock() -> int:
                forged = _envelope("fin-expected")
                forged["journal_sequence"] = 2
                journal.load_events_after_journal_sequence = (  # type: ignore[method-assign]
                    lambda *args, **kwargs: [forged]
                )
                return 1_000_001_000

            with self.assertRaisesRegex(
                RuntimeBudgetError,
                "undeclared financial event identities.*fin-extra",
            ):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    financial_latency_us=(100,),
                    financial_staleness_us=(100,),
                    research_interference_us=(100,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 1},
                    monotonic_ns=ending_clock,
                )


if __name__ == "__main__":
    unittest.main()
