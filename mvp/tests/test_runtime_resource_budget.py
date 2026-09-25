from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetError,
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
    evaluate_runtime_budget,
    nearest_rank_percentile,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


RELEASE_SHA = "1" * 40
CONFIG_HASH = "sha256:" + "a" * 64
HOST_HASH = "sha256:" + "b" * 64


class RuntimeResourceBudgetTests(unittest.TestCase):
    def spec(self, **overrides):
        values = dict(
            scenario_id="declared-host-load-a",
            release_sha=RELEASE_SHA,
            configuration_hash=CONFIG_HASH,
            host_fingerprint=HOST_HASH,
            strategy_horizon_us=5_000_000,
            max_p95_financial_latency_us=2_000_000,
            max_financial_staleness_us=2_000_000,
            max_research_interference_us=2_000_000,
            min_financial_samples=20,
            min_research_samples=1,
        )
        values.update(overrides)
        return RuntimeBudgetSpec(**values)

    def observation(self, **overrides):
        values = dict(
            scenario_id="declared-host-load-a",
            spec_digest=self.spec().digest,
            release_sha=RELEASE_SHA,
            configuration_hash=CONFIG_HASH,
            host_fingerprint=HOST_HASH,
            expected_financial_events=20,
            recovered_financial_events=20,
            financial_latency_us=[100_000] * 20,
            financial_staleness_us=[200_000] * 20,
            research_interference_us=[300_000],
            reconnect_backlog_remaining=0,
        )
        values.update(overrides)
        return RuntimeLoadObservation.create(**values)

    def test_nearest_rank_percentile_is_integer_and_deterministic(self):
        self.assertEqual(nearest_rank_percentile([1, 2, 3, 4, 5], 95), 5)
        self.assertEqual(nearest_rank_percentile([1] * 19 + [9], 95), 1)
        self.assertEqual(nearest_rank_percentile([1] * 18 + [8, 9], 95), 8)

    def test_budget_cannot_be_looser_than_strategy_horizon(self):
        with self.assertRaisesRegex(RuntimeBudgetError, "horizon"):
            self.spec(max_p95_financial_latency_us=5_000_001)
        with self.assertRaisesRegex(RuntimeBudgetError, "horizon"):
            self.spec(max_financial_staleness_us=5_000_001)

    def test_research_interference_budget_cannot_exceed_strategy_horizon(self):
        with self.assertRaisesRegex(RuntimeBudgetError, "research interference"):
            self.spec(max_research_interference_us=5_000_001)

    def test_declared_load_can_pass_without_universal_claim(self):
        decision = evaluate_runtime_budget(self.spec(), self.observation())
        self.assertEqual(decision.status, "PASS")
        self.assertEqual(decision.scenario_id, "declared-host-load-a")
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.metrics["p95_financial_latency_us"], 100_000)

    def test_event_loss_fails_even_when_latency_is_fast(self):
        decision = evaluate_runtime_budget(
            self.spec(),
            self.observation(recovered_financial_events=19),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertIn("financial_event_loss", decision.reasons)

    def test_reconnection_backlog_must_drain(self):
        decision = evaluate_runtime_budget(
            self.spec(),
            self.observation(reconnect_backlog_remaining=1),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertIn("reconnect_backlog_not_drained", decision.reasons)

    def test_slow_disk_like_latency_fails_declared_budget(self):
        samples = [100_000] * 18 + [2_500_000, 3_000_000]
        decision = evaluate_runtime_budget(
            self.spec(),
            self.observation(financial_latency_us=samples),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertIn("financial_latency_budget_exceeded", decision.reasons)

    def test_stale_financial_state_fails_even_if_processing_is_fast(self):
        stale = [100_000] * 19 + [2_500_000]
        decision = evaluate_runtime_budget(
            self.spec(),
            self.observation(financial_staleness_us=stale),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertIn("financial_staleness_budget_exceeded", decision.reasons)

    def test_research_contention_cannot_hide_financial_interference(self):
        decision = evaluate_runtime_budget(
            self.spec(),
            self.observation(research_interference_us=[2_500_000]),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertIn("research_interference_budget_exceeded", decision.reasons)

    def test_too_few_samples_is_inconclusive_not_pass(self):
        decision = evaluate_runtime_budget(
            self.spec(min_financial_samples=20),
            self.observation(
                financial_latency_us=[100_000] * 5,
                financial_staleness_us=[100_000] * 5,
            ),
        )
        self.assertEqual(decision.status, "INCONCLUSIVE")
        self.assertIn("insufficient_financial_latency_samples", decision.reasons)

    def test_failure_has_priority_over_insufficient_evidence(self):
        decision = evaluate_runtime_budget(
            self.spec(),
            self.observation(
                recovered_financial_events=19,
                financial_latency_us=[],
                financial_staleness_us=[],
            ),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertIn("financial_event_loss", decision.reasons)
        self.assertIn("insufficient_financial_latency_samples", decision.reasons)

    def test_scenario_identity_prevents_cross_load_evidence_reuse(self):
        with self.assertRaisesRegex(RuntimeBudgetError, "another declared scenario"):
            evaluate_runtime_budget(
                self.spec(),
                self.observation(scenario_id="different-load"),
            )

    def test_same_scenario_id_cannot_reuse_observation_after_spec_mutation(self):
        original = self.spec(max_p95_financial_latency_us=2_000_000)
        observation = self.observation(spec_digest=original.digest)
        changed = self.spec(max_p95_financial_latency_us=2_500_000)
        self.assertEqual(original.scenario_id, changed.scenario_id)
        self.assertEqual(original.release_sha, changed.release_sha)
        self.assertNotEqual(original.digest, changed.digest)
        with self.assertRaisesRegex(RuntimeBudgetError, "budget spec digest"):
            evaluate_runtime_budget(changed, observation)

    def test_evidence_cannot_be_reused_across_release_config_or_host(self):
        with self.assertRaisesRegex(RuntimeBudgetError, "another release SHA"):
            evaluate_runtime_budget(
                self.spec(),
                self.observation(release_sha="2" * 40),
            )
        with self.assertRaisesRegex(RuntimeBudgetError, "another configuration"):
            evaluate_runtime_budget(
                self.spec(),
                self.observation(configuration_hash="sha256:" + "c" * 64),
            )
        with self.assertRaisesRegex(RuntimeBudgetError, "another host"):
            evaluate_runtime_budget(
                self.spec(),
                self.observation(host_fingerprint="sha256:" + "d" * 64),
            )

    def test_exact_evidence_identity_is_validated(self):
        with self.assertRaisesRegex(RuntimeBudgetError, "40-hex"):
            self.spec(release_sha="main")
        with self.assertRaisesRegex(RuntimeBudgetError, "sha256"):
            self.observation(configuration_hash="config")
        with self.assertRaisesRegex(RuntimeBudgetError, "sha256"):
            self.observation(host_fingerprint="host")
        with self.assertRaisesRegex(RuntimeBudgetError, "sha256"):
            self.observation(spec_digest="budget-v1")

    def test_real_journal_burst_probe_recovers_every_financial_event(self):
        # This is wiring evidence only. The generous thresholds deliberately do
        # not turn shared CI hardware into a target-host performance claim.
        with TemporaryDirectory() as folder:
            store = JournalStore(Path(folder) / "runtime-budget.sqlite3")
            latencies = []
            count = 30
            for index in range(count):
                payload = {"sequence": index, "kind": "financial_probe"}
                envelope = {
                    "event_id": str(uuid4()),
                    "event_type": "FINANCIAL_PROBE",
                    "aggregate_type": "PERFORMANCE_PROBE",
                    "aggregate_id": "burst-a",
                    "aggregate_version": str(index + 1),
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": datetime.now(timezone.utc).isoformat(),
                }
                started = perf_counter_ns()
                store.append_event(envelope, outbox_topic="financial.probe")
                latencies.append((perf_counter_ns() - started) // 1_000)

            recovered = store.load_events("PERFORMANCE_PROBE", "burst-a")
            budget_spec = RuntimeBudgetSpec(
                scenario_id="journal-wiring-ci",
                release_sha=RELEASE_SHA,
                configuration_hash=CONFIG_HASH,
                host_fingerprint=HOST_HASH,
                strategy_horizon_us=60_000_000,
                max_p95_financial_latency_us=60_000_000,
                max_financial_staleness_us=60_000_000,
                max_research_interference_us=60_000_000,
                min_financial_samples=count,
                min_research_samples=1,
            )
            observation = RuntimeLoadObservation.create(
                scenario_id="journal-wiring-ci",
                spec_digest=budget_spec.digest,
                release_sha=RELEASE_SHA,
                configuration_hash=CONFIG_HASH,
                host_fingerprint=HOST_HASH,
                expected_financial_events=count,
                recovered_financial_events=len(recovered),
                financial_latency_us=latencies,
                financial_staleness_us=[0] * count,
                research_interference_us=[0],
                reconnect_backlog_remaining=0,
            )
            decision = evaluate_runtime_budget(
                budget_spec,
                observation,
            )
            self.assertEqual(len(store.pending_outbox(limit=100)), count)
            self.assertEqual(decision.status, "PASS")


if __name__ == "__main__":
    unittest.main()
