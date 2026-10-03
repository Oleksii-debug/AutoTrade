import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetError,
    RuntimeBudgetSpec,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
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


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-end-cut-race",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
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
        "committed_at": "2026-10-03T04:20:00+00:00",
    }


class RuntimeCampaignEndCutRaceTests(unittest.TestCase):
    def test_post_end_clock_financial_commit_cannot_receive_timed_campaign_pass(self):
        """A durable event committed after the end-clock sample is outside that duration.

        The current collector samples monotonic end time before capturing the journal
        end cut. This oracle inserts the expected financial event in that exact gap.
        A repaired collector may reject the raced cut or evaluate it non-PASS, but it
        must never credit the event to the already-ended measured interval.
        """

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            plan = RuntimeCampaignPlan.create(
                spec=spec,
                workload_profile_hash=WORKLOAD,
                declared_duration_ms=1,
                expected_financial_event_ids=("fin-race",),
                financial_aggregate_types=("risk_decision",),
            )
            cut = begin_runtime_campaign(
                journal=journal,
                spec=spec,
                plan=plan,
                monotonic_ns=lambda: 1_000_000_000,
            )

            original_current = JournalStore.current_journal_sequence
            original_load = JournalStore.load_events_after_journal_sequence
            end_clock_sampled = False
            injected = False

            def inject_once(store: JournalStore) -> None:
                nonlocal injected
                if store is journal and end_clock_sampled and not injected:
                    injected = True
                    store.append_event(_envelope("fin-race"))

            def ending_clock() -> int:
                nonlocal end_clock_sampled
                end_clock_sampled = True
                return 1_000_001_000

            def current_with_race(store: JournalStore) -> int:
                inject_once(store)
                return original_current(store)

            def load_with_race(store: JournalStore, *args, **kwargs):
                # If a repair captures the durable end cut before the clock, make
                # the same concurrent event land after that end-clock sample and
                # before the readback. It must then be excluded/rejected, not timed.
                inject_once(store)
                return original_load(store, *args, **kwargs)

            try:
                with patch.object(
                    JournalStore,
                    "current_journal_sequence",
                    new=current_with_race,
                ), patch.object(
                    JournalStore,
                    "load_events_after_journal_sequence",
                    new=load_with_race,
                ):
                    evidence = collect_runtime_campaign_evidence(
                        journal=journal,
                        spec=spec,
                        plan=plan,
                        cut=cut,
                        financial_latency_us=(100,),
                        financial_staleness_us=(100,),
                        research_interference_us=(100,),
                        resource_evidence_hash=RESOURCE,
                        resource_metrics={"cpu_peak_millis": 1},
                        monotonic_ns=ending_clock,
                    )
            except RuntimeBudgetError:
                # Fail-closed cut rejection is a valid repair outcome.
                return

            decision = evaluate_runtime_campaign(spec, evidence)
            self.assertNotEqual(
                decision.status,
                "PASS",
                "a financial commit after the end-clock sample was credited to "
                "the already-ended campaign duration",
            )

    def test_post_end_clock_backlog_drain_cannot_manufacture_pass(self):
        """Reconnect backlog present at terminal cut remains part of that evidence."""

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            plan = RuntimeCampaignPlan.create(
                spec=spec,
                workload_profile_hash=WORKLOAD,
                declared_duration_ms=1,
                expected_financial_event_ids=("fin-backlog",),
                financial_aggregate_types=("risk_decision",),
            )
            cut = begin_runtime_campaign(
                journal=journal,
                spec=spec,
                plan=plan,
                monotonic_ns=lambda: 1_000_000_000,
            )
            journal.append_event(
                _envelope("fin-backlog"),
                outbox_topic="runtime-qualification",
            )
            pending = journal.pending_outbox(limit=10)
            self.assertEqual(len(pending), 1)

            def ending_clock() -> int:
                journal.mark_outbox_delivered(
                    pending[0]["outbox_id"],
                    expected_envelope_hash=pending[0]["envelope_hash"],
                )
                return 1_000_001_000

            evidence = collect_runtime_campaign_evidence(
                journal=journal,
                spec=spec,
                plan=plan,
                cut=cut,
                financial_latency_us=(100,),
                financial_staleness_us=(100,),
                research_interference_us=(100,),
                resource_evidence_hash=RESOURCE,
                resource_metrics={"cpu_peak_millis": 1},
                monotonic_ns=ending_clock,
            )

            # The outbox did drain after the terminal-state snapshot. That later
            # success must not retroactively rewrite the already-ended campaign.
            self.assertEqual(journal.pending_outbox_count(), 0)
            self.assertEqual(evidence.reconnect_backlog_remaining, 1)
            decision = evaluate_runtime_campaign(spec, evidence)
            self.assertEqual(decision.status, "FAIL")
            self.assertIn("reconnect_backlog_not_drained", decision.reasons)


if __name__ == "__main__":
    unittest.main()
