from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import performance_qualification as performance
from mvp.autotrade_mvp import runtime_target_host_runner as runner
from mvp.autotrade_mvp.performance_qualification import RuntimeLoadObservation
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.tests.test_runtime_target_host_runner import (
    FakeClock,
    RELEASE_ID,
    RELEASE_SHA,
    append_expected,
    expected,
    runtime_spec,
)


def run_one(journal: JournalStore, spec, research) -> None:
    declare_runtime_event_plan(
        journal,
        plan_id="terminal-observation-type-plan",
        spec=spec,
        expected_events=(expected("fin-1"),),
    )
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
            declared_plan_id="terminal-observation-type-plan",
            release_artifact_id=RELEASE_ID,
            release_artifact_sha256=RELEASE_SHA,
            declared_duration_ms=1_000,
            operations={"fin-1": lambda: append_expected(journal, "fin-1")},
            research_operations=(("retarget-observation-type", research),),
        )


class RuntimeTargetHostRunnerTerminalObservationTypeAuthorityTests(unittest.TestCase):
    def test_callback_cannot_replace_observation_create_descriptor(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
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
                    r"RuntimeLoadObservation\.create class member changed",
                ):
                    run_one(journal, spec, research)
            finally:
                RuntimeLoadObservation.create = original_descriptor

            self.assertEqual(forged_calls, 0)

    def test_callback_cannot_mutate_observation_create_code_in_place(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            descriptor = RuntimeLoadObservation.__dict__["create"]
            method = descriptor.__func__
            original_code = method.__code__

            def forged(cls, **_kwargs):
                raise AssertionError("forged observation constructor executed")

            self.assertEqual(forged.__code__.co_freevars, ())

            def research() -> None:
                method.__code__ = forged.__code__

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"RuntimeLoadObservation\.create executable authority changed",
                ):
                    run_one(journal, spec, research)
            finally:
                method.__code__ = original_code

    def test_callback_cannot_rebind_observation_validation_helper(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            original = performance._positive_int
            forged_calls = 0

            def forged(value, *, name, allow_zero=False):
                nonlocal forged_calls
                forged_calls += 1
                return original(value, name=name, allow_zero=allow_zero)

            def research() -> None:
                performance._positive_int = forged

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"RuntimeLoadObservation\..*global dependency changed.*_positive_int",
                ):
                    run_one(journal, spec, research)
            finally:
                performance._positive_int = original

            self.assertEqual(forged_calls, 0)

    def test_callback_cannot_mutate_observation_validation_helper_code(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec(financial_samples=1)
            helper = performance._positive_int
            original_code = helper.__code__

            def forged(value, *, name, allow_zero=False):
                raise AssertionError("forged observation validator executed")

            self.assertEqual(forged.__code__.co_freevars, ())

            def research() -> None:
                helper.__code__ = forged.__code__

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"RuntimeLoadObservation\..*executable authority changed",
                ):
                    run_one(journal, spec, research)
            finally:
                helper.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
