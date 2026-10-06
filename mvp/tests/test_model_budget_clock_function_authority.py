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


def _poisoned_commit_command(*_args, **_kwargs):
    raise AssertionError("poisoned journal commit executed")


def _poisoned_budget_reserve(*_args, **_kwargs):
    raise AssertionError("poisoned budget reserve executed")


class DecoyBudget:
    pass


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

    def test_clock_cannot_replace_restore_helper_defaults_in_place(self):
        budget_class = budget_module.DurableModelBudget
        restore = budget_class._restore_clock_authority
        original_defaults = restore.__defaults__
        original_kwdefaults = restore.__kwdefaults__
        alias_restored = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                restore.__defaults__ = ({},)
                budget_module.DurableModelBudget = DecoyBudget
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_class(
                        journal=journal,
                        budget_id="clock-restore-defaults-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                alias_restored = budget_module.DurableModelBudget is budget_class
                budget_module.DurableModelBudget = budget_class
                restore.__defaults__ = original_defaults
                restore.__kwdefaults__ = original_kwdefaults

            self.assertTrue(alias_restored)
            self.assertIs(restore.__defaults__, original_defaults)
            self.assertIs(restore.__kwdefaults__, original_kwdefaults)
            self.assertEqual(
                journal.load_events("model_budget", "clock-restore-defaults-budget"),
                [],
            )

    def test_clock_cannot_replace_journal_commit_code_in_place(self):
        commit = JournalStore.commit_command
        original_code = commit.__code__
        original_defaults = commit.__defaults__
        original_kwdefaults = commit.__kwdefaults__

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            armed = False

            def hostile_clock():
                if armed:
                    commit.__code__ = _poisoned_commit_command.__code__
                return NOW_TEXT

            budget = budget_module.DurableModelBudget(
                journal=journal,
                budget_id="clock-journal-commit-function-budget",
                ceiling="5",
                environment="PAPER",
                clock=hostile_clock,
            )
            armed = True
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority:.*"
                    r"JournalStore\.commit_command.*__code__",
                ):
                    budget.reserve("clock-journal-commit-request", "0.2")
            finally:
                commit.__code__ = original_code
                commit.__defaults__ = original_defaults
                commit.__kwdefaults__ = original_kwdefaults

            self.assertIs(commit.__code__, original_code)
            self.assertEqual(budget.snapshot().reserved, 0)
            events = journal.load_events(
                "model_budget",
                "clock-journal-commit-function-budget",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "ModelBudgetInitialized")

    def test_clock_cannot_replace_budget_ledger_reserve_code_in_place(self):
        reserve = budget_module.BudgetLedger.reserve
        original_code = reserve.__code__
        original_defaults = reserve.__defaults__
        original_kwdefaults = reserve.__kwdefaults__

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                reserve.__code__ = _poisoned_budget_reserve.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority:.*"
                    r"BudgetLedger\.reserve.*__code__",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-budget-ledger-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                reserve.__code__ = original_code
                reserve.__defaults__ = original_defaults
                reserve.__kwdefaults__ = original_kwdefaults

            self.assertIs(reserve.__code__, original_code)
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "clock-budget-ledger-function-budget",
                ),
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
