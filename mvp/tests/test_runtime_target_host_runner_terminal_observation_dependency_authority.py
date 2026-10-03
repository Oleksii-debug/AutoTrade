from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_campaign import capture_runtime_host_identity
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_qualification import RuntimeCampaignEvidence
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp import runtime_target_host_runner as runner


SOURCE_SHA = "5" * 40
CONFIG = "sha256:" + "6" * 64
RELEASE_ID = "74000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "7" * 64


class FakeClock:
    def __init__(self) -> None:
        self.value = 5_000_000_000

    def __call__(self) -> int:
        self.value += 100_000
        return self.value


def runtime_spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-terminal-observation-dependency-authority",
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
            "committed_at": "2026-10-04T00:50:00Z",
        }
    )


def declare_one(journal: JournalStore, spec: RuntimeBudgetSpec) -> None:
    declare_runtime_event_plan(
        journal,
        plan_id="terminal-observation-dependency-plan",
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


def run_with_callback(
    *,
    journal: JournalStore,
    spec: RuntimeBudgetSpec,
    callback,
) -> None:
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
            declared_plan_id="terminal-observation-dependency-plan",
            release_artifact_id=RELEASE_ID,
            release_artifact_sha256=RELEASE_SHA,
            declared_duration_ms=1_000,
            operations={"fin-1": lambda: append_expected(journal)},
            research_operations=(("retarget-dependency", callback),),
        )


class RuntimeTargetHostRunnerTerminalObservationDependencyAuthorityTests(unittest.TestCase):
    def test_callback_cannot_rebind_observation_constructor_dependency(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            method = RuntimeCampaignEvidence.to_observation
            namespace = method.__globals__
            original = namespace["RuntimeLoadObservation"]
            forged_calls = 0

            class ForgedObservation:
                @classmethod
                def create(cls, **kwargs):
                    nonlocal forged_calls
                    forged_calls += 1
                    return original.create(**kwargs)

            def research() -> None:
                namespace["RuntimeLoadObservation"] = ForgedObservation

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"runner callback authority changed: RuntimeCampaignEvidence\.to_observation dependency changed: RuntimeLoadObservation",
                ):
                    run_with_callback(journal=journal, spec=spec, callback=research)
            finally:
                namespace["RuntimeLoadObservation"] = original

            self.assertEqual(forged_calls, 0)

    def test_callback_cannot_rebind_observation_replace_dependency(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            method = RuntimeCampaignEvidence.to_observation
            namespace = method.__globals__
            original = namespace["replace"]
            forged_calls = 0

            def forged(value, *args, **kwargs):
                nonlocal forged_calls
                forged_calls += 1
                return original(value, *args, **kwargs)

            def research() -> None:
                namespace["replace"] = forged

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"runner callback authority changed: RuntimeCampaignEvidence\.to_observation dependency changed: replace",
                ):
                    run_with_callback(journal=journal, spec=spec, callback=research)
            finally:
                namespace["replace"] = original

            self.assertEqual(forged_calls, 0)


if __name__ == "__main__":
    unittest.main()
