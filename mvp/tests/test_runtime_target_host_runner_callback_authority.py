from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_campaign import capture_runtime_host_identity
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp import runtime_target_host_runner as runner


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + "b" * 64
RELEASE_ID = "71000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "d" * 64


class FakeClock:
    def __init__(self, start: int = 2_000_000_000, tick: int = 100_000):
        self.value = start
        self.tick = tick

    def __call__(self) -> int:
        self.value += self.tick
        return self.value


class HostileOperationKey(str):
    calls = 0

    def __hash__(self) -> int:
        type(self).calls += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).calls += 1
        return super().__eq__(other)


def runtime_spec() -> RuntimeBudgetSpec:
    host = host_identity_fingerprint(capture_runtime_host_identity())
    return RuntimeBudgetSpec(
        scenario_id="wp65-runner-callback-authority",
        release_sha=SOURCE_SHA,
        configuration_hash=CONFIG,
        host_fingerprint=host,
        strategy_horizon_us=10_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def append_expected(journal: JournalStore, event_id: str) -> None:
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
            "committed_at": "2026-10-04T00:20:00Z",
        }
    )


class RuntimeTargetHostRunnerCallbackAuthorityTests(unittest.TestCase):
    def _declared(self, journal: JournalStore, spec: RuntimeBudgetSpec) -> None:
        declare_runtime_event_plan(
            journal,
            plan_id="callback-authority-plan",
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

    def test_operation_keys_reject_executable_text_subclasses_before_hash_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declared(journal, spec)
            key = HostileOperationKey("fin-1")
            operations = {key: lambda: append_expected(journal, "fin-1")}
            HostileOperationKey.calls = 0
            before = journal.current_journal_sequence()

            with (
                patch.object(runner, "_require_shared_clock_contract", return_value=None),
                self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"operation keys must be exact strings",
                ),
            ):
                runner.run_declared_target_host_campaign(
                    journal=journal,
                    spec=spec,
                    declared_plan_id="callback-authority-plan",
                    release_artifact_id=RELEASE_ID,
                    release_artifact_sha256=RELEASE_SHA,
                    declared_duration_ms=1_000,
                    operations=operations,
                    research_operations=(("contention", lambda: None),),
                )

            self.assertEqual(HostileOperationKey.calls, 0)
            self.assertEqual(journal.current_journal_sequence(), before)

    def test_resource_probe_cannot_rebind_pending_outbox_authority(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            original = JournalStore.pending_outbox_count
            forged_calls = 0

            def forged(_store: JournalStore) -> int:
                nonlocal forged_calls
                forged_calls += 1
                return 0

            def probe() -> dict[str, int]:
                JournalStore.pending_outbox_count = forged
                return {"working_set_bytes": 1}

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"resource metric authority changed: JournalStore\.pending_outbox_count",
                ):
                    runner._capture_resource_metrics(journal, probe)
            finally:
                JournalStore.pending_outbox_count = original

            self.assertEqual(forged_calls, 0)

    def test_research_callback_cannot_retarget_runner_monotonic_clock(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declared(journal, spec)
            clock = FakeClock()
            original = runner.time.monotonic_ns
            forged_calls = 0

            def forged() -> int:
                nonlocal forged_calls
                forged_calls += 1
                return 0

            def research() -> None:
                runner.time.monotonic_ns = forged

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
                        r"runner callback authority changed: time\.monotonic_ns",
                    ),
                ):
                    runner.run_declared_target_host_campaign(
                        journal=journal,
                        spec=spec,
                        declared_plan_id="callback-authority-plan",
                        release_artifact_id=RELEASE_ID,
                        release_artifact_sha256=RELEASE_SHA,
                        declared_duration_ms=1_000,
                        operations={"fin-1": lambda: append_expected(journal, "fin-1")},
                        research_operations=(("retarget-clock", research),),
                    )
            finally:
                runner.time.monotonic_ns = original

            self.assertEqual(forged_calls, 0)

    def test_financial_callback_cannot_retarget_terminal_budget_evaluator(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declared(journal, spec)
            clock = FakeClock()
            original = runner.evaluate_runtime_budget
            forged_calls = 0

            def forged(*_args, **_kwargs):
                nonlocal forged_calls
                forged_calls += 1
                raise AssertionError("forged evaluator executed")

            def financial() -> None:
                append_expected(journal, "fin-1")
                runner.evaluate_runtime_budget = forged

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
                        r"runner callback authority changed: evaluate_runtime_budget",
                    ),
                ):
                    runner.run_declared_target_host_campaign(
                        journal=journal,
                        spec=spec,
                        declared_plan_id="callback-authority-plan",
                        release_artifact_id=RELEASE_ID,
                        release_artifact_sha256=RELEASE_SHA,
                        declared_duration_ms=1_000,
                        operations={"fin-1": financial},
                        research_operations=(("contention", lambda: None),),
                    )
            finally:
                runner.evaluate_runtime_budget = original

            self.assertEqual(forged_calls, 0)


if __name__ == "__main__":
    unittest.main()
