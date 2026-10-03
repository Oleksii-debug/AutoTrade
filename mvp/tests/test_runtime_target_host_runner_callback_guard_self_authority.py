from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_campaign import capture_runtime_host_identity
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp import runtime_target_host_runner as runner


SOURCE_SHA = "e" * 40
CONFIG = "sha256:" + "f" * 64
RELEASE_ID = "72000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "1" * 64


class FakeClock:
    def __init__(self) -> None:
        self.value = 3_000_000_000

    def __call__(self) -> int:
        self.value += 100_000
        return self.value


def runtime_spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-runner-guard-self-authority",
        release_sha=SOURCE_SHA,
        configuration_hash=CONFIG,
        host_fingerprint=host_identity_fingerprint(capture_runtime_host_identity()),
        strategy_horizon_us=10_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def declare_one(journal: JournalStore, spec: RuntimeBudgetSpec) -> None:
    declare_runtime_event_plan(
        journal,
        plan_id="guard-self-plan",
        spec=spec,
        expected_events=(
            ExpectedJournalEvent(
                event_id="fin-1",
                event_type="QualificationEvent",
                aggregate_type="risk_decision",
                aggregate_id="fin-1",
                aggregate_version=1,
            ),
        ),
    )


class RuntimeTargetHostRunnerCallbackGuardSelfAuthorityTests(unittest.TestCase):
    def test_resource_probe_cannot_disable_guard_helper_then_retarget_metric_reader(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            original_helper = runner._require_callable_binding
            original_pending = JournalStore.pending_outbox_count
            forged_calls = 0

            def forged_pending(_store: JournalStore) -> int:
                nonlocal forged_calls
                forged_calls += 1
                return 0

            def disabled_guard(**_kwargs) -> None:
                return None

            def probe() -> dict[str, int]:
                runner._require_callable_binding = disabled_guard
                JournalStore.pending_outbox_count = forged_pending
                return {}

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"resource metric authority changed: _require_callable_binding",
                ):
                    runner._capture_resource_metrics(journal, probe)
            finally:
                runner._require_callable_binding = original_helper
                JournalStore.pending_outbox_count = original_pending

            self.assertEqual(forged_calls, 0)

    def test_research_callback_cannot_preseed_forged_resource_reader_for_next_sample(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            clock = FakeClock()
            original_pending = JournalStore.pending_outbox_count
            forged_calls = 0

            def forged_pending(_store: JournalStore) -> int:
                nonlocal forged_calls
                forged_calls += 1
                return 0

            def research() -> None:
                JournalStore.pending_outbox_count = forged_pending

            try:
                with (
                    patch.object(runner, "_require_shared_clock_contract", return_value=None),
                    patch.object(runner.time, "monotonic_ns", side_effect=clock),
                    patch(
                        "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                        side_effect=clock,
                    ),
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=clock,
                    ),
                    self.assertRaisesRegex(
                        runner.RuntimeTargetHostRunnerError,
                        r"runner callback authority changed: JournalStore\.pending_outbox_count",
                    ),
                ):
                    runner.run_declared_target_host_campaign(
                        journal=journal,
                        spec=spec,
                        declared_plan_id="guard-self-plan",
                        release_artifact_id=RELEASE_ID,
                        release_artifact_sha256=RELEASE_SHA,
                        declared_duration_ms=1_000,
                        operations={"fin-1": lambda: None},
                        research_operations=(("preseed-resource-reader", research),),
                    )
            finally:
                JournalStore.pending_outbox_count = original_pending

            self.assertEqual(forged_calls, 0)


if __name__ == "__main__":
    unittest.main()
