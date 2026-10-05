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

    def test_clock_cannot_poison_financial_module_dependencies(self):
        original_budget_ledger = budget_module.BudgetLedger
        original_aggregate_type = budget_module._AGGREGATE_TYPE
        original_command_actor = budget_module._COMMAND_ACTOR
        original_idempotency_key = budget_module._idempotency_key
        original_route_model = budget_module.route_model

        class DecoyBudgetLedger:
            def __init__(self, *_args, **_kwargs):
                self.fail_if_used = True

        def hostile_idempotency_key(**_kwargs):
            return "hostile-idempotency"

        def hostile_route_model(*_args, **_kwargs):
            self.fail("hostile route_model survived clock cleanup")

        armed = False
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                if armed:
                    budget_module.BudgetLedger = DecoyBudgetLedger
                    budget_module._AGGREGATE_TYPE = "hostile-model-budget"
                    budget_module._COMMAND_ACTOR = "hostile-clock-actor"
                    budget_module._idempotency_key = hostile_idempotency_key
                    budget_module.route_model = hostile_route_model
                return NOW_TEXT

            budget = budget_module.DurableModelBudget(
                journal=journal,
                budget_id="clock-financial-dependency-budget",
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
                    budget.reserve("clock-financial-dependency-request", "0.2")
            finally:
                budget_module.BudgetLedger = original_budget_ledger
                budget_module._AGGREGATE_TYPE = original_aggregate_type
                budget_module._COMMAND_ACTOR = original_command_actor
                budget_module._idempotency_key = original_idempotency_key
                budget_module.route_model = original_route_model

            message = str(caught.exception)
            self.assertIn("module.BudgetLedger", message)
            self.assertIn("module._AGGREGATE_TYPE", message)
            self.assertIn("module._COMMAND_ACTOR", message)
            self.assertIn("module._idempotency_key", message)
            self.assertIn("module.route_model", message)
            self.assertIs(budget_module.BudgetLedger, original_budget_ledger)
            self.assertEqual(
                budget_module._AGGREGATE_TYPE,
                original_aggregate_type,
            )
            self.assertEqual(
                budget_module._COMMAND_ACTOR,
                original_command_actor,
            )
            self.assertIs(
                budget_module._idempotency_key,
                original_idempotency_key,
            )
            self.assertIs(budget_module.route_model, original_route_model)
            self.assertEqual(budget.snapshot().reserved, 0)
            events = journal.load_events(
                "model_budget",
                "clock-financial-dependency-budget",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "ModelBudgetInitialized")

    def test_clock_cannot_shadow_post_clock_builtin_resolution(self):
        builtin_names = ("enumerate", "isinstance", "len", "int", "str")
        originals = {
            name: vars(budget_module).get(name)
            for name in builtin_names
        }
        existed = {
            name: name in vars(budget_module)
            for name in builtin_names
        }

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.enumerate = lambda *_args, **_kwargs: ()
                budget_module.isinstance = lambda *_args, **_kwargs: True
                budget_module.len = lambda *_args, **_kwargs: 0
                budget_module.int = lambda *_args, **_kwargs: 1
                budget_module.str = lambda *_args, **_kwargs: "hostile"
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ) as caught:
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-builtin-resolution-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                for name in builtin_names:
                    if existed[name]:
                        setattr(budget_module, name, originals[name])
                    elif name in vars(budget_module):
                        delattr(budget_module, name)

            message = str(caught.exception)
            for name in builtin_names:
                self.assertIn("module." + name, message)
                self.assertEqual(
                    name in vars(budget_module),
                    existed[name],
                )
                if existed[name]:
                    self.assertIs(vars(budget_module)[name], originals[name])
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "clock-builtin-resolution-budget",
                ),
                [],
            )

    def test_financial_dependency_mutation_blocks_initialization_before_write(self):
        original_budget_ledger = budget_module.BudgetLedger
        original_aggregate_type = budget_module._AGGREGATE_TYPE

        class DecoyBudgetLedger:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("poisoned BudgetLedger reached replay")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")

            def hostile_clock():
                budget_module.BudgetLedger = DecoyBudgetLedger
                budget_module._AGGREGATE_TYPE = "hostile-model-budget"
                return NOW_TEXT

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "model budget clock mutated authority",
                ) as caught:
                    budget_module.DurableModelBudget(
                        journal=journal,
                        budget_id="clock-init-financial-dependency-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=hostile_clock,
                    )
            finally:
                budget_module.BudgetLedger = original_budget_ledger
                budget_module._AGGREGATE_TYPE = original_aggregate_type

            self.assertIn("module.BudgetLedger", str(caught.exception))
            self.assertIn("module._AGGREGATE_TYPE", str(caught.exception))
            self.assertIs(budget_module.BudgetLedger, original_budget_ledger)
            self.assertEqual(
                budget_module._AGGREGATE_TYPE,
                original_aggregate_type,
            )
            self.assertEqual(
                journal.load_events(
                    "model_budget",
                    "clock-init-financial-dependency-budget",
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
