from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.runtime_load_measurement as measurement_module
import mvp.autotrade_mvp.runtime_target_host_runner as runner_module
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_campaign import capture_runtime_host_identity
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp.runtime_target_host_runner import (
    RuntimeTargetHostRunnerError,
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


def runtime_spec() -> RuntimeBudgetSpec:
    host = host_identity_fingerprint(capture_runtime_host_identity())
    return RuntimeBudgetSpec(
        scenario_id="wp65-callback-authority",
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


def expected(event_id: str) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type="QualificationEvent",
        aggregate_type="risk_decision",
        aggregate_id=event_id,
        aggregate_version=1,
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
    def _declare(self, journal: JournalStore, spec: RuntimeBudgetSpec) -> None:
        declare_runtime_event_plan(
            journal,
            plan_id="runner-callback-authority-plan",
            spec=spec,
            expected_events=(expected("fin-1"),),
        )

    def _run(
        self,
        journal: JournalStore,
        spec: RuntimeBudgetSpec,
        *,
        operation,
        research_operation=lambda: None,
        resource_probe=None,
    ):
        clock = FakeClock()
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
                "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                side_effect=clock,
            ),
        ):
            return run_declared_target_host_campaign(
                journal=journal,
                spec=spec,
                declared_plan_id="runner-callback-authority-plan",
                release_artifact_id=RELEASE_ID,
                release_artifact_sha256=RELEASE_SHA,
                declared_duration_ms=1_000,
                operations={"fin-1": operation},
                research_operations=(("contention", research_operation),),
                resource_probe=resource_probe,
            )

    def test_research_callback_cannot_retarget_terminal_monotonic_clock(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declare(journal, spec)

            def attack() -> None:
                runner_module.time.monotonic_ns = lambda: 1

            with self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                r"measurement authority: time\.monotonic_ns",
            ):
                self._run(
                    journal,
                    spec,
                    operation=lambda: append_expected(journal, "fin-1"),
                    research_operation=attack,
                )

            self.assertIsNone(journal.get_event("fin-1"))

    def test_financial_operation_cannot_retarget_perf_counter_before_end_sample(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declare(journal, spec)

            def attack() -> None:
                append_expected(journal, "fin-1")
                measurement_module.perf_counter_ns = lambda: 1

            with self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                r"measurement authority: runtime_load_measurement\.perf_counter_ns",
            ):
                self._run(journal, spec, operation=attack)

            self.assertIsNotNone(journal.get_event("fin-1"))
            self.assertIsNone(
                journal.get_event(
                    runner_module.measure_declared_financial_operation.__globals__[
                        "_measurement_event_id"
                    ](
                        "runner-callback-authority-plan",
                        "fin-1",
                    )
                )
            )

    def test_resource_probe_cannot_retarget_runner_owned_cpu_clock(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declare(journal, spec)

            def attack_probe() -> dict[str, int]:
                runner_module.time.process_time_ns = lambda: 0
                return {"working_set_bytes": 1234}

            with self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                r"measurement authority: time\.process_time_ns",
            ):
                self._run(
                    journal,
                    spec,
                    operation=lambda: append_expected(journal, "fin-1"),
                    resource_probe=attack_probe,
                )

            self.assertIsNone(journal.get_event("fin-1"))

    def test_operation_key_validation_rejects_executable_key_before_hash_dispatch(self) -> None:
        class HostileKey(str):
            hash_called = False

            def __hash__(self) -> int:
                type(self).hash_called = True
                return super().__hash__()

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            self._declare(journal, spec)
            key = HostileKey("fin-1")
            operations = {key: lambda: append_expected(journal, "fin-1")}
            HostileKey.hash_called = False

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_runner._require_shared_clock_contract",
                    return_value=None,
                ),
                self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "exact built-in str event ids",
                ),
            ):
                run_declared_target_host_campaign(
                    journal=journal,
                    spec=spec,
                    declared_plan_id="runner-callback-authority-plan",
                    release_artifact_id=RELEASE_ID,
                    release_artifact_sha256=RELEASE_SHA,
                    declared_duration_ms=1_000,
                    operations=operations,
                    research_operations=(("contention", lambda: None),),
                )

            self.assertFalse(HostileKey.hash_called)
            self.assertIsNone(journal.get_event("fin-1"))


if __name__ == "__main__":
    unittest.main()
