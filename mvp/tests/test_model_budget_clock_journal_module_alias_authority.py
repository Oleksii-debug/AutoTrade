from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module


NOW_TEXT = "2026-09-25T10:00:00+00:00"
ORIGINAL_BUDGET_CLASS = budget_module.DurableModelBudget
ORIGINAL_JOURNAL_CLASS = budget_module.JournalStore


class ModelBudgetClockJournalModuleAliasAuthorityTests(unittest.TestCase):
    def test_clock_cannot_poison_journalstore_module_alias(self):
        class DecoyJournalStore:
            pass

        with TemporaryDirectory() as directory:
            journal = ORIGINAL_JOURNAL_CLASS(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.JournalStore = DecoyJournalStore
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    ORIGINAL_BUDGET_CLASS(
                        journal=journal,
                        budget_id="clock-journal-module-alias-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                # Isolate the regression against the vulnerable pre-fix source,
                # which currently leaves this imported trust-root alias poisoned.
                budget_module.JournalStore = ORIGINAL_JOURNAL_CLASS

            self.assertIs(budget_module.JournalStore, ORIGINAL_JOURNAL_CLASS)
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "clock-journal-module-alias-budget",
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
