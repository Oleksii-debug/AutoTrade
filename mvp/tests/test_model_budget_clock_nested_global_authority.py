from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
DECOY_OBJECT_CALLS = 0
ORIGINAL_DECIMAL = budget_module.Decimal


class DecoyObject:
    @staticmethod
    def __getattribute__(*_args):
        global DECOY_OBJECT_CALLS
        DECOY_OBJECT_CALLS += 1
        raise AssertionError("module object shadow reached trusted recovery")


class ModelBudgetClockNestedGlobalAuthorityTests(unittest.TestCase):
    def test_clock_cannot_shadow_nested_restore_object_global(self):
        global DECOY_OBJECT_CALLS
        DECOY_OBJECT_CALLS = 0
        had_object_binding = "object" in vars(budget_module)
        original_object_binding = vars(budget_module).get("object")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.object = DecoyObject
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-nested-object-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                if had_object_binding:
                    budget_module.object = original_object_binding
                elif "object" in vars(budget_module):
                    delattr(budget_module, "object")

            self.assertEqual(DECOY_OBJECT_CALLS, 0)
            self.assertEqual("object" in vars(budget_module), had_object_binding)
            if had_object_binding:
                self.assertIs(vars(budget_module)["object"], original_object_binding)
            self.assertEqual(
                journal.load_events("model_budget", "clock-nested-object-budget"),
                [],
            )

    def test_clock_cannot_poison_nested_restore_decimal_global(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.Decimal = str
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-nested-decimal-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                budget_module.Decimal = ORIGINAL_DECIMAL

            self.assertIs(budget_module.Decimal, ORIGINAL_DECIMAL)
            self.assertEqual(
                journal.load_events("model_budget", "clock-nested-decimal-budget"),
                [],
            )

    def test_clock_cannot_shadow_exception_handler_before_raising(self):
        had_exception_binding = "Exception" in vars(budget_module)
        original_exception_binding = vars(budget_module).get("Exception")
        alias_restored = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.Exception = int
                raise RuntimeError("hostile clock failure")

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ) as captured:
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-exception-shadow-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
                self.assertIsInstance(captured.exception.__cause__, RuntimeError)
            finally:
                if had_exception_binding:
                    alias_restored = (
                        vars(budget_module).get("Exception")
                        is original_exception_binding
                    )
                    budget_module.Exception = original_exception_binding
                else:
                    alias_restored = "Exception" not in vars(budget_module)
                    if "Exception" in vars(budget_module):
                        delattr(budget_module, "Exception")

            self.assertTrue(alias_restored)
            self.assertEqual(
                journal.load_events("model_budget", "clock-exception-shadow-budget"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
