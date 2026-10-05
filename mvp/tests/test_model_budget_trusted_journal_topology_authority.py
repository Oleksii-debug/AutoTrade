from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"


class ModelBudgetTrustedJournalTopologyAuthorityTests(unittest.TestCase):
    def test_prepoisoned_journal_base_is_restored_before_reserve_dispatch(self):
        canonical_base = JournalStore.__bases__[0]
        forged_get_event_calls = []

        def hostile_get_event(*_args, **_kwargs):
            forged_get_event_calls.append("forged")
            self.fail("pre-poisoned journal base reached model-budget get_event")

        HostileJournalBase = type(
            "HostileJournalBase",
            (canonical_base,),
            {"get_event": hostile_get_event},
        )

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="trusted-topology-reserve-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            try:
                JournalStore.__bases__ = (HostileJournalBase,)
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget journal class authority is invalid:.*"
                    r"JournalStore\.__bases__",
                ):
                    budget.reserve("trusted-topology-request", "0.2")

                self.assertEqual(forged_get_event_calls, [])
                self.assertEqual(JournalStore.__bases__, (canonical_base,))
                self.assertEqual(budget.snapshot().reserved, 0)
            finally:
                JournalStore.__bases__ = (canonical_base,)

    def test_prepoisoned_journal_method_shadow_is_removed_before_dispatch(self):
        canonical_get_event = JournalStore.get_event
        forged_get_event_calls = []

        def hostile_get_event(*_args, **_kwargs):
            forged_get_event_calls.append("forged")
            self.fail("pre-poisoned JournalStore.get_event reached model-budget dispatch")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="trusted-topology-method-shadow-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            try:
                JournalStore.get_event = hostile_get_event
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget journal class authority is invalid:.*"
                    r"JournalStore\.get_event",
                ):
                    budget.reserve("trusted-topology-method-shadow-request", "0.2")

                self.assertEqual(forged_get_event_calls, [])
                self.assertNotIn("get_event", JournalStore.__dict__)
                self.assertIs(JournalStore.get_event, canonical_get_event)
                self.assertEqual(budget.snapshot().reserved, 0)
            finally:
                if "get_event" in JournalStore.__dict__:
                    type.__delattr__(JournalStore, "get_event")

    def test_prepoisoned_journal_base_is_restored_before_budget_constructor_read(self):
        canonical_base = JournalStore.__bases__[0]
        forged_load_calls = []

        def hostile_load_events(*_args, **_kwargs):
            forged_load_calls.append("forged")
            self.fail("pre-poisoned journal base reached model-budget load_events")

        HostileJournalBase = type(
            "HostileJournalBaseAtConstruction",
            (canonical_base,),
            {"load_events": hostile_load_events},
        )

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            try:
                JournalStore.__bases__ = (HostileJournalBase,)
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget journal class authority is invalid:.*"
                    r"JournalStore\.__bases__",
                ):
                    DurableModelBudget(
                        journal=journal,
                        budget_id="trusted-topology-constructor-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=lambda: NOW_TEXT,
                    )

                self.assertEqual(forged_load_calls, [])
                self.assertEqual(JournalStore.__bases__, (canonical_base,))
                self.assertEqual(
                    journal.load_events(
                        "model_budget",
                        "trusted-topology-constructor-budget",
                    ),
                    [],
                )
            finally:
                JournalStore.__bases__ = (canonical_base,)


if __name__ == "__main__":
    unittest.main()
