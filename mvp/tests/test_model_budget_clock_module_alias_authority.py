from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
ORIGINAL_BUDGET_CLASS = budget_module.DurableModelBudget


class ModelBudgetClockModuleAliasAuthorityTests(unittest.TestCase):
    def test_clock_cannot_poison_durable_budget_module_alias(self):
        class DecoyBudget:
            restore_calls = 0

            @staticmethod
            def _restore_clock_authority(*_args, **_kwargs):
                DecoyBudget.restore_calls += 1
                raise AssertionError("clock redirected budget authority cleanup")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.DurableModelBudget = DecoyBudget
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    ORIGINAL_BUDGET_CLASS(
                        journal=journal,
                        budget_id="clock-module-alias-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                # Isolate the regression even against a vulnerable implementation.
                budget_module.DurableModelBudget = ORIGINAL_BUDGET_CLASS

            self.assertEqual(DecoyBudget.restore_calls, 0)
            self.assertIs(
                budget_module.DurableModelBudget,
                ORIGINAL_BUDGET_CLASS,
            )
            self.assertEqual(
                journal.load_events("model_budget", "clock-module-alias-budget"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
