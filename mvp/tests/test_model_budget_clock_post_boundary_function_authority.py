from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
REPLAY_DECOY_CALLS = 0
APPEND_DECOY_CALLS = 0


def _poisoned_replay(_self):
    global REPLAY_DECOY_CALLS
    REPLAY_DECOY_CALLS += 1
    raise AssertionError("poisoned budget replay executed after hostile clock")


def _poisoned_append(*_args, **_kwargs):
    global APPEND_DECOY_CALLS
    APPEND_DECOY_CALLS += 1
    raise AssertionError("poisoned journal append executed after hostile clock")


class ModelBudgetClockPostBoundaryFunctionAuthorityTests(unittest.TestCase):
    def test_clock_cannot_poison_budget_replay_before_constructor_rebuild(self):
        global REPLAY_DECOY_CALLS
        REPLAY_DECOY_CALLS = 0
        replay = budget_module.DurableModelBudget._replay
        original_code = replay.__code__
        original_defaults = replay.__defaults__
        original_kwdefaults = replay.__kwdefaults__
        restored_before_test_cleanup = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                replay.__code__ = _poisoned_replay.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-replay-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
                restored_before_test_cleanup = (
                    replay.__code__ is original_code
                    and replay.__defaults__ is original_defaults
                    and replay.__kwdefaults__ is original_kwdefaults
                )
            finally:
                replay.__code__ = original_code
                replay.__defaults__ = original_defaults
                replay.__kwdefaults__ = original_kwdefaults

            self.assertTrue(restored_before_test_cleanup)
            self.assertEqual(REPLAY_DECOY_CALLS, 0)
            self.assertEqual(
                journal.load_events("model_budget", "clock-replay-function-budget"),
                [],
            )

    def test_clock_cannot_poison_journal_append_before_durable_write(self):
        global APPEND_DECOY_CALLS
        APPEND_DECOY_CALLS = 0
        append_event = JournalStore.append_event
        original_code = append_event.__code__
        original_defaults = append_event.__defaults__
        original_kwdefaults = append_event.__kwdefaults__
        restored_before_test_cleanup = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                append_event.__code__ = _poisoned_append.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-journal-append-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
                restored_before_test_cleanup = (
                    append_event.__code__ is original_code
                    and append_event.__defaults__ is original_defaults
                    and append_event.__kwdefaults__ is original_kwdefaults
                )
            finally:
                append_event.__code__ = original_code
                append_event.__defaults__ = original_defaults
                append_event.__kwdefaults__ = original_kwdefaults

            self.assertTrue(restored_before_test_cleanup)
            self.assertEqual(APPEND_DECOY_CALLS, 0)
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "clock-journal-append-function-budget",
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
