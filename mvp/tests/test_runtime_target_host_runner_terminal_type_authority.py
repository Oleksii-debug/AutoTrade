from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_campaign import capture_runtime_host_identity
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_campaign import ParsedRuntimeTargetHostCampaign
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp import runtime_target_host_runner as runner


SOURCE_SHA = "8" * 40
CONFIG = "sha256:" + "9" * 64
RELEASE_ID = "75000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "a" * 64


class FakeClock:
    def __init__(self) -> None:
        self.value = 6_000_000_000

    def __call__(self) -> int:
        self.value += 100_000
        return self.value


def runtime_spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-terminal-type-authority",
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


def append_expected(journal: JournalStore) -> None:
    payload = {"event_id": "fin-1", "kind": "risk_decision"}
    journal.append_event(
        {
            "event_id": "fin-1",
            "event_type": "QualificationEvent",
            "aggregate_type": "risk_decision",
            "aggregate_id": "fin-1",
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-04T01:00:00Z",
        }
    )


def declare_one(journal: JournalStore, spec: RuntimeBudgetSpec) -> None:
    declare_runtime_event_plan(
        journal,
        plan_id="terminal-type-plan",
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


def run_with_research(journal: JournalStore, spec: RuntimeBudgetSpec, research) -> None:
    clock = FakeClock()
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
    ):
        runner.run_declared_target_host_campaign(
            journal=journal,
            spec=spec,
            declared_plan_id="terminal-type-plan",
            release_artifact_id=RELEASE_ID,
            release_artifact_sha256=RELEASE_SHA,
            declared_duration_ms=1_000,
            operations={"fin-1": lambda: append_expected(journal)},
            research_operations=(("retarget-type", research),),
        )


class RuntimeTargetHostRunnerTerminalTypeAuthorityTests(unittest.TestCase):
    def test_callback_cannot_rebind_runtime_observation_create_descriptor(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            original_descriptor = RuntimeLoadObservation.__dict__["create"]
            original_create = RuntimeLoadObservation.create
            forged_calls = 0

            def forged(cls, **kwargs):
                nonlocal forged_calls
                forged_calls += 1
                return original_create(**kwargs)

            def research() -> None:
                RuntimeLoadObservation.create = classmethod(forged)

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"runner callback authority changed: RuntimeLoadObservation\.create",
                ):
                    run_with_research(journal, spec, research)
            finally:
                RuntimeLoadObservation.create = original_descriptor

            self.assertEqual(forged_calls, 0)

    def test_callback_cannot_rebind_terminal_parser_parse_descriptor(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            original_descriptor = ParsedRuntimeTargetHostCampaign.__dict__["parse"]
            original_parse = ParsedRuntimeTargetHostCampaign.parse
            forged_calls = 0

            def forged(cls, payload):
                nonlocal forged_calls
                forged_calls += 1
                return original_parse(payload)

            def research() -> None:
                ParsedRuntimeTargetHostCampaign.parse = classmethod(forged)

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"runner callback authority changed: ParsedRuntimeTargetHostCampaign\.parse",
                ):
                    run_with_research(journal, spec, research)
            finally:
                ParsedRuntimeTargetHostCampaign.parse = original_descriptor

            self.assertEqual(forged_calls, 0)

    def test_callback_cannot_mutate_terminal_parser_parse_code_in_place(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            descriptor = ParsedRuntimeTargetHostCampaign.__dict__["parse"]
            method = descriptor.__func__
            original_code = method.__code__

            def forged(cls, _payload):
                raise AssertionError("forged parser executed")

            self.assertEqual(forged.__code__.co_freevars, ())

            def research() -> None:
                method.__code__ = forged.__code__

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"runner callback authority changed: ParsedRuntimeTargetHostCampaign\.parse",
                ):
                    run_with_research(journal, spec, research)
            finally:
                method.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
