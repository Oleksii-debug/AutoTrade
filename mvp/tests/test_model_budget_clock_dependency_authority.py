from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
ORIGINAL_DATETIME = budget_module.datetime
ORIGINAL_TIMEDELTA = budget_module.timedelta
ORIGINAL_TIMEZONE = budget_module.timezone


class ModelBudgetClockDependencyAuthorityTests(unittest.TestCase):
    def test_clock_cannot_poison_clock_normalizer_dependencies(self):
        class DecoyDateTime:
            calls = 0

            @classmethod
            def fromisoformat(cls, _value):
                cls.calls += 1
                raise AssertionError("hostile datetime reached clock normalization")

        hostile_timedelta = object()
        hostile_timezone = object()

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.datetime = DecoyDateTime
                budget_module.timedelta = hostile_timedelta
                budget_module.timezone = hostile_timezone
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-dependency-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                # Isolate the regression even against a vulnerable implementation.
                budget_module.datetime = ORIGINAL_DATETIME
                budget_module.timedelta = ORIGINAL_TIMEDELTA
                budget_module.timezone = ORIGINAL_TIMEZONE

            self.assertEqual(DecoyDateTime.calls, 0)
            self.assertIs(budget_module.datetime, ORIGINAL_DATETIME)
            self.assertIs(budget_module.timedelta, ORIGINAL_TIMEDELTA)
            self.assertIs(budget_module.timezone, ORIGINAL_TIMEZONE)
            self.assertEqual(
                journal.load_events("model_budget", "clock-dependency-budget"),
                [],
            )

            stable = budget_module.DurableModelBudget(
                journal=journal,
                budget_id="clock-dependency-stable-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            self.assertEqual(stable.snapshot().ceiling, 5)
            events = journal.load_events(
                "model_budget",
                "clock-dependency-stable-budget",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["committed_at"], NOW_TEXT)


if __name__ == "__main__":
    unittest.main()
