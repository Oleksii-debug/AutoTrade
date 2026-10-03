from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import performance_qualification as performance
from mvp.autotrade_mvp import runtime_target_host_campaign as campaign_module
from mvp.autotrade_mvp import runtime_target_host_runner as runner
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_runtime_target_host_runner_terminal_type_authority import (
    declare_one,
    run_with_research,
    runtime_spec,
)


class RuntimeTargetHostRunnerTerminalTypeDependencyAuthorityTests(unittest.TestCase):
    def test_callback_cannot_rebind_observation_validation_dependency(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
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
                    r"runner callback authority changed: .*_positive_int",
                ):
                    run_with_research(journal, spec, research)
            finally:
                performance._positive_int = original

            self.assertEqual(forged_calls, 0)

    def test_callback_cannot_mutate_observation_validation_dependency_code(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
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
                    r"runner callback authority changed: .*_positive_int",
                ):
                    run_with_research(journal, spec, research)
            finally:
                helper.__code__ = original_code

    def test_callback_cannot_rebind_terminal_parser_helper(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            original = campaign_module._parse_json
            forged_calls = 0

            def forged(raw):
                nonlocal forged_calls
                forged_calls += 1
                return original(raw)

            def research() -> None:
                campaign_module._parse_json = forged

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"runner callback authority changed: .*_parse_json",
                ):
                    run_with_research(journal, spec, research)
            finally:
                campaign_module._parse_json = original

            self.assertEqual(forged_calls, 0)

    def test_callback_cannot_mutate_terminal_parser_helper_code(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = runtime_spec()
            declare_one(journal, spec)
            helper = campaign_module._parse_json
            original_code = helper.__code__

            def forged(_raw):
                raise AssertionError("forged parser helper executed")

            self.assertEqual(forged.__code__.co_freevars, ())

            def research() -> None:
                helper.__code__ = forged.__code__

            try:
                with self.assertRaisesRegex(
                    runner.RuntimeTargetHostRunnerError,
                    r"runner callback authority changed: .*_parse_json",
                ):
                    run_with_research(journal, spec, research)
            finally:
                helper.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
