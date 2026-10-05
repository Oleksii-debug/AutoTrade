from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as model_budget_module
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
ORIGINAL_BUDGET_CLASS = model_budget_module.DurableModelBudget


class ModelBudgetModuleAliasAuthorityTests(unittest.TestCase):
    def test_clock_restores_module_budget_alias_before_durable_effect(self):
        armed = False

        class DecoyBudget:
            restore_calls = 0

            @staticmethod
            def _restore_clock_authority(*_args, **_kwargs):
                DecoyBudget.restore_calls += 1
                raise AssertionError("poisoned module alias redirected restore")

        def hostile_clock():
            nonlocal armed
            if armed:
                model_budget_module.DurableModelBudget = DecoyBudget
            return NOW_TEXT

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = ORIGINAL_BUDGET_CLASS(
                journal=journal,
                budget_id="clock-module-alias-budget",
                ceiling="5",
                environment="PAPER",
                clock=hostile_clock,
            )
            armed = True
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority:.*module\.DurableModelBudget",
                ):
                    budget.reserve("clock-module-alias-request", "0.2")
            finally:
                model_budget_module.DurableModelBudget = ORIGINAL_BUDGET_CLASS

            self.assertEqual(DecoyBudget.restore_calls, 0)
            self.assertIs(
                model_budget_module.DurableModelBudget,
                ORIGINAL_BUDGET_CLASS,
            )
            self.assertEqual(budget.snapshot().reserved, 0)


if __name__ == "__main__":
    unittest.main()
