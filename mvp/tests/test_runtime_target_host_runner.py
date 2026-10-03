from __future__ import annotations

from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.runtime_load_measurement as measurement_module
import mvp.autotrade_mvp.runtime_target_host_runner as runner_module
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.runtime_load_measurement import RuntimeLoadMeasurementError
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_campaign import capture_runtime_host_identity
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp.runtime_target_host_runner import (
    RuntimeTargetHostRunnerError,
    _require_shared_clock_contract,
    run_cpu_pressure_probe,
    run_declared_target_host_campaign,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + "b" * 64
RELEASE_ID = "70000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "c" * 64


class FakeClock:
    def __init__(self, start: int = 1_000_000_000, tick: int = 100_000):
        self.value = start
        self.tick = tick

    def __call__(self) -> int:
        self.value += self.tick
        return self.value

    def advance(self, nanoseconds: int) -> None:
        self.value += nanoseconds


def runtime_spec(*, financial_samples: int = 2) -> RuntimeBudgetSpec:
    host = host_identity_fingerprint(capture_runtime_host_identity())
    return RuntimeBudgetSpec(
        scenario_id="wp65-executable-target-host",
        release_sha=SOURCE_SHA,
        configuration_hash=CONFIG,
        host_fingerprint=host,
        strategy_horizon_us=10_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=financial_samples,
        min_research_samples=1,
    )


def expected(event_id: str) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type="QualificationEvent",
        aggregate_type="risk_decision",
        aggregate_id=event_id,
        aggregate_version=1,
    )


def append_expected(
    journal: JournalStore,
    event_id: str,
    *,
    outbox_topic: str | None = None,
) -> None:
    payload = {"event_id": event_id, "kind": "risk_decision"}
    journal.append_event(
        {
            "event_id": event_id,
            "event_type": "QualificationEvent",
            "aggregate_type": "risk_decision",
            "aggregate_id": event_id,
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T21:00:00Z",
        },
        outbox_topic=outbox_topic,
    )


class RuntimeTargetHostRunnerTests(unittest.TestCase):
    def _declare(self, journal: JournalStore, spec: RuntimeBudgetSpec, *ids: str) -> None:
        declare_runtime_event_plan(
            journal,
            plan_id="runner-plan",
            spec=spec,
            expected_events=tuple(expected(event_id) for event_id in ids),
        )

    def _run(self, journal, spec, operations, *, clock, research=None):
        if research is None:
            research = (("cpu-contention", lambda: run_cpu_pressure_probe(iterations=1)),)
        with (
            patch(
                "mvp.autotrade_mvp.runtime_target_host_runner._require_shared_clock_contract",
                return_value=None,
            ),
            patch(
                "mvp.autotrade_mvp.runtime_target_host_runner.time.monotonic_ns",
                side_effect=clock,
            ),
            patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                side_effect=clock,
            ),
            patch(
                "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                side_effect=clock,
            ),
        ):
            return run_declared_target_host_campaign(
                journal=journal,
                spec=spec,
                declared_plan_id="runner-plan",
                release_artifact_id=RELEASE_ID,
                release_artifact_sha256=RELEASE_SHA,
                declared_duration_ms=1_000,
                operations=operations,
                research_operations=research,
            )

    def test_runner_executes_exact_declared_financial_operations_and_builds_raw_artifact(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declare(journal, spec, "fin-1", "fin-2")
            clock = FakeClock()
            result = self._run(
                journal,
                spec,
                {
                    "fin-1": lambda: append_expected(journal, "fin-1"),
                    "fin-2": lambda: append_expected(journal, "fin-2"),
                },
                clock=clock,
            )

            self.assertEqual(result.budget_decision.status, "PASS")
            self.assertEqual(result.measurement.financial_event_ids, ("fin-1", "fin-2"))
            self.assertEqual(result.measurement.financial_latency_us, (100, 100))
            self.assertEqual(result.measurement.financial_staleness_us, (100, 100))
            self.assertEqual(result.measurement.research_interference_us, (100,))
            self.assertEqual(
                result.measurement.workload_profile_hash,
                result.declared_plan.digest,
            )
            self.assertEqual(
                result.retained_campaign.recovered_event_ids,
                ("fin-1", "fin-2"),
            )
            self.assertEqual(
                result.retained_campaign.observation.financial_latency_us,
                result.measurement.financial_latency_us,
            )
            self.assertTrue(result.measurement_bytes)
            self.assertTrue(result.retained_campaign_bytes)
            self.assertTrue(result.inventory_bytes)

    def test_operation_mapping_must_match_durable_plan_before_campaign_cut(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declare(journal, spec, "fin-1", "fin-2")
            before = journal.current_journal_sequence()
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_runner._require_shared_clock_contract",
                return_value=None,
            ), self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                "exactly match the durable pre-run event plan",
            ):
                run_declared_target_host_campaign(
                    journal=journal,
                    spec=spec,
                    declared_plan_id="runner-plan",
                    release_artifact_id=RELEASE_ID,
                    release_artifact_sha256=RELEASE_SHA,
                    declared_duration_ms=1_000,
                    operations={"fin-1": lambda: None},
                    research_operations=(("contention", lambda: None),),
                )
            self.assertEqual(journal.current_journal_sequence(), before)

    def test_above_budget_latency_fails_without_losing_financial_event(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()

            def slow_financial_operation() -> None:
                clock.advance(5_000_000)
                append_expected(journal, "fin-1")

            result = self._run(
                journal,
                spec,
                {"fin-1": slow_financial_operation},
                clock=clock,
            )
            self.assertEqual(result.budget_decision.status, "FAIL")
            self.assertIn(
                "financial_latency_p95_exceeds_budget",
                result.budget_decision.reasons,
            )
            self.assertEqual(
                result.budget_decision.metrics["expected_financial_events"],
                1,
            )
            self.assertEqual(
                result.budget_decision.metrics["recovered_financial_events"],
                1,
            )
            self.assertEqual(result.measurement.financial_event_ids, ("fin-1",))

    def test_reconnect_backlog_remains_a_fail_closed_budget_result(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            result = self._run(
                journal,
                spec,
                {
                    "fin-1": lambda: append_expected(
                        journal,
                        "fin-1",
                        outbox_topic="financial.events",
                    )
                },
                clock=clock,
            )
            self.assertEqual(result.budget_decision.status, "FAIL")
            self.assertIn(
                "reconnect_backlog_not_drained",
                result.budget_decision.reasons,
            )
            self.assertEqual(result.campaign_evidence.reconnect_backlog_remaining, 1)
            self.assertEqual(result.measurement.financial_event_ids, ("fin-1",))

    def test_research_callback_cannot_replace_terminal_budget_authority(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            original = runner_module.evaluate_runtime_budget

            def poison_budget_authority() -> None:
                runner_module.evaluate_runtime_budget = lambda *_args, **_kwargs: None

            try:
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "budget evaluator callable authority changed",
                ):
                    self._run(
                        journal,
                        spec,
                        {"fin-1": lambda: append_expected(journal, "fin-1")},
                        clock=clock,
                        research=(("poison-budget", poison_budget_authority),),
                    )
            finally:
                runner_module.evaluate_runtime_budget = original

            self.assertIsNone(journal.get_event("fin-1"))

    def test_research_callback_cannot_replace_evidence_class_member(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            original = runner_module.ResearchInterferenceSample.__post_init__

            def poison_research_sample_authority() -> None:
                runner_module.ResearchInterferenceSample.__post_init__ = lambda self: None

            try:
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "ResearchInterferenceSample.__post_init__ class member changed",
                ):
                    self._run(
                        journal,
                        spec,
                        {"fin-1": lambda: append_expected(journal, "fin-1")},
                        clock=clock,
                        research=(("poison-class", poison_research_sample_authority),),
                    )
            finally:
                runner_module.ResearchInterferenceSample.__post_init__ = original

            self.assertIsNone(journal.get_event("fin-1"))

    def test_research_callback_cannot_mutate_evidence_class_code_in_place(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            function = runner_module.ResearchInterferenceSample.__post_init__
            original_code = function.__code__

            def permissive_post_init(self) -> None:
                return None

            def poison_research_sample_code() -> None:
                function.__code__ = permissive_post_init.__code__

            try:
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "ResearchInterferenceSample.__post_init__ executable authority changed",
                ):
                    self._run(
                        journal,
                        spec,
                        {"fin-1": lambda: append_expected(journal, "fin-1")},
                        clock=clock,
                        research=(("poison-code", poison_research_sample_code),),
                    )
            finally:
                function.__code__ = original_code

            self.assertIsNone(journal.get_event("fin-1"))

    def test_research_callback_cannot_replace_budget_transitive_dependency(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            namespace = runner_module.evaluate_runtime_budget.__globals__
            original = namespace["nearest_rank_percentile"]

            def poison_budget_dependency() -> None:
                namespace["nearest_rank_percentile"] = lambda *_args, **_kwargs: 0

            try:
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "budget evaluator global dependency changed.*nearest_rank_percentile",
                ):
                    self._run(
                        journal,
                        spec,
                        {"fin-1": lambda: append_expected(journal, "fin-1")},
                        clock=clock,
                        research=(("poison-budget-dependency", poison_budget_dependency),),
                    )
            finally:
                namespace["nearest_rank_percentile"] = original

            self.assertIsNone(journal.get_event("fin-1"))

    def test_financial_operation_cannot_replace_latency_clock_authority(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()

            def poison_latency_clock() -> None:
                append_expected(journal, "fin-1")
                measurement_module.perf_counter_ns = lambda: 1

            with self.assertRaisesRegex(
                RuntimeLoadMeasurementError,
                "measurement authority changed during financial operation: "
                "perf_counter_ns",
            ):
                self._run(
                    journal,
                    spec,
                    {"fin-1": poison_latency_clock},
                    clock=clock,
                )

            self.assertIsNotNone(journal.get_event("fin-1"))


    def test_operation_mapping_rejects_string_subclass_before_hash_dispatch(self):
        class HostileOperationKey(str):
            calls = 0

            def __hash__(self):
                type(self).calls += 1
                return super().__hash__()

            def __eq__(self, other):
                type(self).calls += 1
                return super().__eq__(other)

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            key = HostileOperationKey("fin-1")
            operations = {key: lambda: append_expected(journal, "fin-1")}
            HostileOperationKey.calls = 0
            before = journal.current_journal_sequence()

            with (
                patch.object(
                    runner_module,
                    "_require_shared_clock_contract",
                    return_value=None,
                ),
                self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "operation keys must be exact strings",
                ),
            ):
                run_declared_target_host_campaign(
                    journal=journal,
                    spec=spec,
                    declared_plan_id="runner-plan",
                    release_artifact_id=RELEASE_ID,
                    release_artifact_sha256=RELEASE_SHA,
                    declared_duration_ms=1_000,
                    operations=operations,
                    research_operations=(("contention", lambda: None),),
                )

            self.assertEqual(HostileOperationKey.calls, 0)
            self.assertEqual(journal.current_journal_sequence(), before)

    def test_research_callback_cannot_mutate_input_budget_spec_instance(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            original = spec.max_p95_financial_latency_us

            def poison_input_spec() -> None:
                object.__setattr__(
                    spec,
                    "max_p95_financial_latency_us",
                    spec.strategy_horizon_us,
                )

            try:
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "input RuntimeBudgetSpec state changed during campaign callback: "
                    "max_p95_financial_latency_us",
                ):
                    self._run(
                        journal,
                        spec,
                        {"fin-1": lambda: append_expected(journal, "fin-1")},
                        clock=clock,
                        research=(("poison-budget-spec", poison_input_spec),),
                    )
            finally:
                object.__setattr__(
                    spec,
                    "max_p95_financial_latency_us",
                    original,
                )

            self.assertIsNone(journal.get_event("fin-1"))

    def test_research_callback_cannot_disable_callback_guard_helper(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            original = runner_module._require_callable_authority

            def disabled_guard(*_args, **_kwargs) -> None:
                return None

            def poison_guard_helper() -> None:
                runner_module._require_callable_authority = disabled_guard

            try:
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "runner callback guard helper authority changed: "
                    "_require_callable_authority",
                ):
                    self._run(
                        journal,
                        spec,
                        {"fin-1": lambda: append_expected(journal, "fin-1")},
                        clock=clock,
                        research=(("poison-guard-helper", poison_guard_helper),),
                    )
            finally:
                runner_module._require_callable_authority = original

            self.assertIsNone(journal.get_event("fin-1"))

    def test_research_callback_cannot_rebind_measurement_basis(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            original = runner_module.STALENESS_BASIS

            def poison_basis() -> None:
                runner_module.STALENESS_BASIS = "forged-staleness-basis"

            try:
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "staleness basis changed during campaign callback",
                ):
                    self._run(
                        journal,
                        spec,
                        {"fin-1": lambda: append_expected(journal, "fin-1")},
                        clock=clock,
                        research=(("poison-basis", poison_basis),),
                    )
            finally:
                runner_module.STALENESS_BASIS = original

            self.assertIsNone(journal.get_event("fin-1"))

    def test_resource_timestamp_and_metrics_share_one_durable_cut(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            clock = FakeClock()
            original_capture = runner_module._capture_resource_metrics
            raced = False

            def racing_capture(store, resource_probe, **kwargs):
                nonlocal raced
                metrics = original_capture(store, resource_probe, **kwargs)
                if not raced:
                    append_expected(store, "resource-race")
                    raced = True
                return metrics

            with (
                patch.object(
                    runner_module,
                    "_capture_resource_metrics",
                    side_effect=racing_capture,
                ),
                self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "resource sample crossed a durable JournalStore cut",
                ),
            ):
                self._run(
                    journal,
                    spec,
                    {"fin-1": lambda: append_expected(journal, "fin-1")},
                    clock=clock,
                )

            self.assertTrue(raced)
            self.assertIsNotNone(journal.get_event("resource-race"))

    @unittest.skipUnless(
        sys.version_info >= (3, 13),
        "language-level shared perf_counter/monotonic clock contract requires Python 3.13+",
    )
    def test_supported_runtime_executes_unpatched_shared_clock_campaign(self):
        _require_shared_clock_contract()
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            self._declare(journal, spec, "fin-1")
            result = run_declared_target_host_campaign(
                journal=journal,
                spec=spec,
                declared_plan_id="runner-plan",
                release_artifact_id=RELEASE_ID,
                release_artifact_sha256=RELEASE_SHA,
                declared_duration_ms=1_000,
                operations={"fin-1": lambda: append_expected(journal, "fin-1")},
                research_operations=(
                    ("cpu-contention", lambda: run_cpu_pressure_probe(iterations=1)),
                ),
            )
            self.assertEqual(result.measurement.financial_event_ids, ("fin-1",))
            self.assertEqual(len(result.measurement.financial_latency_us), 1)
            self.assertEqual(len(result.measurement.research_interference_us), 1)
            self.assertIn(result.budget_decision.status, {"PASS", "FAIL"})

    def test_cpu_pressure_probe_is_bounded_and_deterministic(self):
        first = run_cpu_pressure_probe(iterations=3)
        second = run_cpu_pressure_probe(iterations=3)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        with self.assertRaisesRegex(RuntimeTargetHostRunnerError, "iterations"):
            run_cpu_pressure_probe(iterations=0)


if __name__ == "__main__":
    unittest.main()
