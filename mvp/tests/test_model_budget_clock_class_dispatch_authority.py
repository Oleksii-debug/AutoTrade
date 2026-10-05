from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"


class ModelBudgetClockClassDispatchAuthorityTests(unittest.TestCase):
    def test_clock_restores_rebound_journal_base_before_commit(self):
        armed = False
        canonical_base = JournalStore.__bases__[0]
        forged_commit_calls = []

        def hostile_commit_command(*_args, **_kwargs):
            forged_commit_calls.append("forged")
            self.fail("injected journal base reached durable budget commit")

        HostileJournalBase = type(
            "HostileJournalBase",
            (canonical_base,),
            {"commit_command": hostile_commit_command},
        )

        def hostile_clock():
            nonlocal armed
            if armed:
                JournalStore.__bases__ = (HostileJournalBase,)
            return NOW_TEXT

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="clock-base-rebinding-budget",
                ceiling="5",
                environment="PAPER",
                clock=hostile_clock,
            )
            armed = True
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority:.*"
                    r"JournalStore\.__bases__",
                ):
                    budget.reserve("clock-base-request", "0.2")

                self.assertEqual(forged_commit_calls, [])
                self.assertEqual(JournalStore.__bases__, (canonical_base,))
                self.assertEqual(budget.snapshot().reserved, 0)
            finally:
                JournalStore.__bases__ = (canonical_base,)

    def test_clock_restores_rebound_recovery_and_journal_dispatch_before_effect(self):
        armed = False
        restore_descriptor = vars(DurableModelBudget)["_restore_clock_authority"]
        canonical_load_events = JournalStore.load_events

        def hostile_clock():
            nonlocal armed
            if armed:
                DurableModelBudget._restore_clock_authority = (
                    lambda *_args, **_kwargs: self.fail(
                        "rebound budget-clock restore intercepted recovery"
                    )
                )
                JournalStore.load_events = (
                    lambda *_args, **_kwargs: self.fail(
                        "rebound journal load reached durable budget authority"
                    )
                )
            return NOW_TEXT

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="clock-class-rebinding-budget",
                ceiling="5",
                environment="PAPER",
                clock=hostile_clock,
            )
            armed = True
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ) as caught:
                    budget.reserve("clock-rebind-request", "0.2")

                self.assertIn(
                    "DurableModelBudget._restore_clock_authority",
                    str(caught.exception),
                )
                self.assertIn("JournalStore.load_events", str(caught.exception))
                self.assertIs(
                    vars(DurableModelBudget)["_restore_clock_authority"],
                    restore_descriptor,
                )
                self.assertIs(JournalStore.load_events, canonical_load_events)
                self.assertNotIn("load_events", JournalStore.__dict__)
                self.assertEqual(budget.snapshot().reserved, 0)
            finally:
                type.__setattr__(
                    DurableModelBudget,
                    "_restore_clock_authority",
                    restore_descriptor,
                )
                if "load_events" in JournalStore.__dict__:
                    type.__delattr__(JournalStore, "load_events")


if __name__ == "__main__":
    unittest.main()
