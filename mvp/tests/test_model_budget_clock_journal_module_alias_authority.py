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
                    r"model budget clock mutated authority:.*module\.JournalStore",
                ):
                    ORIGINAL_BUDGET_CLASS(
                        journal=journal,
                        budget_id="clock-journal-module-alias-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                budget_module.JournalStore = ORIGINAL_JOURNAL_CLASS

            self.assertIs(budget_module.JournalStore, ORIGINAL_JOURNAL_CLASS)
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "clock-journal-module-alias-budget",
                ),
                [],
            )

            stable = ORIGINAL_BUDGET_CLASS(
                journal=journal,
                budget_id="clock-journal-module-alias-stable-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            self.assertEqual(stable.snapshot().ceiling, 5)
            stable_events = journal.load_events(
                "model_budget",
                "clock-journal-module-alias-stable-budget",
            )
            self.assertEqual(len(stable_events), 1)
            self.assertEqual(stable_events[0]["committed_at"], NOW_TEXT)

    def test_clock_exception_still_restores_journalstore_module_alias(self):
        class DecoyJournalStore:
            pass

        class ClockFailure(RuntimeError):
            pass

        with TemporaryDirectory() as directory:
            journal = ORIGINAL_JOURNAL_CLASS(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.JournalStore = DecoyJournalStore
                raise ClockFailure("clock exploded after alias mutation")

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority:.*module\.JournalStore",
                ) as caught:
                    ORIGINAL_BUDGET_CLASS(
                        journal=journal,
                        budget_id="clock-journal-module-alias-exception-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                budget_module.JournalStore = ORIGINAL_JOURNAL_CLASS

            self.assertIsInstance(caught.exception.__cause__, ClockFailure)
            self.assertIs(budget_module.JournalStore, ORIGINAL_JOURNAL_CLASS)
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "clock-journal-module-alias-exception-budget",
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
