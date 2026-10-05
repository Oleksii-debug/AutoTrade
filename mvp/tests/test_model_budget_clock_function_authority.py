from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
RESTORE_DECOY_CALLS = 0
CLOCK_TEXT_DECOY_CALLS = 0


def _poisoned_restore(*_args, **_kwargs):
    global RESTORE_DECOY_CALLS
    RESTORE_DECOY_CALLS += 1
    return []


def _poisoned_clock_text(_value):
    global CLOCK_TEXT_DECOY_CALLS
    CLOCK_TEXT_DECOY_CALLS += 1
    return NOW_TEXT


class ModelBudgetClockFunctionAuthorityTests(unittest.TestCase):
    def test_clock_cannot_replace_restore_helper_code_in_place(self):
        global RESTORE_DECOY_CALLS
        RESTORE_DECOY_CALLS = 0
        restore = budget_module.DurableModelBudget._restore_clock_authority
        original_code = restore.__code__
        original_defaults = restore.__defaults__
        original_kwdefaults = restore.__kwdefaults__

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                restore.__code__ = _poisoned_restore.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-restore-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                # Isolate the regression even against a vulnerable implementation.
                restore.__code__ = original_code
                restore.__defaults__ = original_defaults
                restore.__kwdefaults__ = original_kwdefaults

            self.assertEqual(RESTORE_DECOY_CALLS, 0)
            self.assertIs(restore.__code__, original_code)
            self.assertIs(restore.__defaults__, original_defaults)
            self.assertIs(restore.__kwdefaults__, original_kwdefaults)
            self.assertEqual(
                journal.load_events("model_budget", "clock-restore-function-budget"),
                [],
            )

    def test_clock_cannot_replace_clock_normalizer_code_in_place(self):
        global CLOCK_TEXT_DECOY_CALLS
        CLOCK_TEXT_DECOY_CALLS = 0
        clock_text = budget_module._clock_text
        original_code = clock_text.__code__
        original_defaults = clock_text.__defaults__
        original_kwdefaults = clock_text.__kwdefaults__

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                clock_text.__code__ = _poisoned_clock_text.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-normalizer-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                # Isolate the regression even against a vulnerable implementation.
                clock_text.__code__ = original_code
                clock_text.__defaults__ = original_defaults
                clock_text.__kwdefaults__ = original_kwdefaults

            self.assertEqual(CLOCK_TEXT_DECOY_CALLS, 0)
            self.assertIs(clock_text.__code__, original_code)
            self.assertIs(clock_text.__defaults__, original_defaults)
            self.assertIs(clock_text.__kwdefaults__, original_kwdefaults)
            self.assertEqual(
                journal.load_events("model_budget", "clock-normalizer-function-budget"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
