from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
CLOCK_NOW_DECOY_CALLS = 0
SNAPSHOT_DECOY_CALLS = 0
SAFE_STATE_DECOY_CALLS = 0


def _poisoned_clock_now(_self):
    global CLOCK_NOW_DECOY_CALLS
    CLOCK_NOW_DECOY_CALLS += 1
    raise AssertionError("poisoned _clock_now reached a later budget call")


def _poisoned_snapshot(_self):
    global SNAPSHOT_DECOY_CALLS
    SNAPSHOT_DECOY_CALLS += 1
    raise AssertionError("poisoned clock authority snapshot reached a later budget call")


def _poisoned_safe_state(_value, *, subject):
    global SAFE_STATE_DECOY_CALLS
    SAFE_STATE_DECOY_CALLS += 1
    raise AssertionError("poisoned safe authority state reached: " + subject)


class DecoyJournalStore:
    pass


class ModelBudgetClockSnapshotAuthorityTests(unittest.TestCase):
    @staticmethod
    def _assert_clean_follow_up(journal: JournalStore, budget_id: str) -> None:
        stable = budget_module.DurableModelBudget(
            journal=journal,
            budget_id=budget_id,
            ceiling="5",
            environment="PAPER",
            clock=lambda: NOW_TEXT,
        )
        self_events = journal.load_events("model_budget", budget_id)
        if stable.snapshot().ceiling != 5 or len(self_events) != 1:
            raise AssertionError("clean follow-up budget did not initialize exactly once")
        if self_events[0]["committed_at"] != NOW_TEXT:
            raise AssertionError("clean follow-up budget used the wrong clock authority")

    def test_clock_cannot_poison_clock_now_code_for_later_calls(self):
        global CLOCK_NOW_DECOY_CALLS
        CLOCK_NOW_DECOY_CALLS = 0
        clock_now = budget_module.DurableModelBudget._clock_now
        original_code = clock_now.__code__
        original_defaults = clock_now.__defaults__
        original_kwdefaults = clock_now.__kwdefaults__
        restored_before_test_cleanup = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                clock_now.__code__ = _poisoned_clock_now.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-now-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
                restored_before_test_cleanup = (
                    clock_now.__code__ is original_code
                    and clock_now.__defaults__ is original_defaults
                    and clock_now.__kwdefaults__ is original_kwdefaults
                )
            finally:
                clock_now.__code__ = original_code
                clock_now.__defaults__ = original_defaults
                clock_now.__kwdefaults__ = original_kwdefaults

            self.assertTrue(restored_before_test_cleanup)
            self.assertEqual(CLOCK_NOW_DECOY_CALLS, 0)
            self.assertEqual(
                journal.load_events("model_budget", "clock-now-function-budget"),
                [],
            )
            self._assert_clean_follow_up(journal, "clock-now-function-follow-up")

    def test_clock_cannot_poison_snapshot_code_for_later_calls(self):
        global SNAPSHOT_DECOY_CALLS
        SNAPSHOT_DECOY_CALLS = 0
        snapshot = budget_module.DurableModelBudget._clock_authority_snapshot
        original_code = snapshot.__code__
        original_defaults = snapshot.__defaults__
        original_kwdefaults = snapshot.__kwdefaults__
        restored_before_test_cleanup = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                snapshot.__code__ = _poisoned_snapshot.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-snapshot-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
                restored_before_test_cleanup = (
                    snapshot.__code__ is original_code
                    and snapshot.__defaults__ is original_defaults
                    and snapshot.__kwdefaults__ is original_kwdefaults
                )
            finally:
                snapshot.__code__ = original_code
                snapshot.__defaults__ = original_defaults
                snapshot.__kwdefaults__ = original_kwdefaults

            self.assertTrue(restored_before_test_cleanup)
            self.assertEqual(SNAPSHOT_DECOY_CALLS, 0)
            self.assertEqual(
                journal.load_events("model_budget", "clock-snapshot-function-budget"),
                [],
            )
            self._assert_clean_follow_up(journal, "clock-snapshot-function-follow-up")

    def test_clock_cannot_poison_safe_state_code_for_later_calls(self):
        global SAFE_STATE_DECOY_CALLS
        SAFE_STATE_DECOY_CALLS = 0
        safe_state = budget_module.DurableModelBudget._safe_authority_state
        original_code = safe_state.__code__
        original_defaults = safe_state.__defaults__
        original_kwdefaults = safe_state.__kwdefaults__
        restored_before_test_cleanup = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                safe_state.__code__ = _poisoned_safe_state.__code__
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-safe-state-function-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
                restored_before_test_cleanup = (
                    safe_state.__code__ is original_code
                    and safe_state.__defaults__ is original_defaults
                    and safe_state.__kwdefaults__ is original_kwdefaults
                )
            finally:
                safe_state.__code__ = original_code
                safe_state.__defaults__ = original_defaults
                safe_state.__kwdefaults__ = original_kwdefaults

            self.assertTrue(restored_before_test_cleanup)
            self.assertEqual(SAFE_STATE_DECOY_CALLS, 0)
            self.assertEqual(
                journal.load_events("model_budget", "clock-safe-state-function-budget"),
                [],
            )
            self._assert_clean_follow_up(journal, "clock-safe-state-function-follow-up")

    def test_clock_cannot_shadow_snapshot_only_journalstore_global(self):
        original_journal_store = budget_module.JournalStore
        alias_restored_before_test_cleanup = False

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.JournalStore = DecoyJournalStore
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ):
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-journalstore-global-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
                alias_restored_before_test_cleanup = (
                    budget_module.JournalStore is original_journal_store
                )
            finally:
                budget_module.JournalStore = original_journal_store

            self.assertTrue(alias_restored_before_test_cleanup)
            self.assertEqual(
                journal.load_events("model_budget", "clock-journalstore-global-budget"),
                [],
            )
            self._assert_clean_follow_up(journal, "clock-journalstore-global-follow-up")


if __name__ == "__main__":
    unittest.main()
