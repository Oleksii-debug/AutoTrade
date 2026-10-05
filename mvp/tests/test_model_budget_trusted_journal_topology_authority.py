from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
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


    def test_clock_cannot_poison_topology_guard_module_bindings(self):
        original_authority = budget_module._MODEL_BUDGET_JOURNAL_CLASS_AUTHORITY
        original_changes = budget_module._model_budget_journal_class_authority_changes
        original_require = budget_module._require_model_budget_journal_class_authority
        module_state = vars(budget_module)
        vars_existed = "vars" in module_state
        original_vars = module_state.get("vars")
        forged_calls = []

        def forged_changes(*, restore):
            forged_calls.append(("changes", restore))
            return []

        def forged_require():
            forged_calls.append(("require", None))

        def forged_vars(*_args, **_kwargs):
            forged_calls.append(("vars", None))
            raise AssertionError("hostile vars reached topology guard")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module._MODEL_BUDGET_JOURNAL_CLASS_AUTHORITY = ()
                budget_module._model_budget_journal_class_authority_changes = (
                    forged_changes
                )
                budget_module._require_model_budget_journal_class_authority = (
                    forged_require
                )
                budget_module.vars = forged_vars
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget clock mutated authority",
                ) as caught:
                    DurableModelBudget(
                        journal=journal,
                        budget_id="trusted-topology-guard-binding-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                budget_module._MODEL_BUDGET_JOURNAL_CLASS_AUTHORITY = (
                    original_authority
                )
                budget_module._model_budget_journal_class_authority_changes = (
                    original_changes
                )
                budget_module._require_model_budget_journal_class_authority = (
                    original_require
                )
                if vars_existed:
                    budget_module.vars = original_vars
                elif "vars" in vars(budget_module):
                    delattr(budget_module, "vars")

            message = str(caught.exception)
            self.assertIn(
                "module._MODEL_BUDGET_JOURNAL_CLASS_AUTHORITY",
                message,
            )
            self.assertIn(
                "module._model_budget_journal_class_authority_changes",
                message,
            )
            self.assertIn(
                "module._require_model_budget_journal_class_authority",
                message,
            )
            self.assertIn("module.vars", message)
            self.assertEqual(forged_calls, [])
            self.assertIs(
                budget_module._MODEL_BUDGET_JOURNAL_CLASS_AUTHORITY,
                original_authority,
            )
            self.assertIs(
                budget_module._model_budget_journal_class_authority_changes,
                original_changes,
            )
            self.assertIs(
                budget_module._require_model_budget_journal_class_authority,
                original_require,
            )
            self.assertEqual("vars" in vars(budget_module), vars_existed)
            if vars_existed:
                self.assertIs(vars(budget_module)["vars"], original_vars)
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "trusted-topology-guard-binding-budget",
                ),
                [],
            )



if __name__ == "__main__":
    unittest.main()
