from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.model_budget_journal as budget_module
from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.persistence import JournalStore


NOW_TEXT = "2026-09-25T10:00:00+00:00"
PREPOISONED_GET_EVENT_CALLS = 0
HOSTILE_SCOPE_TEXT_CALLS = 0


def _poisoned_get_event(*_args, **_kwargs):
    global PREPOISONED_GET_EVENT_CALLS
    PREPOISONED_GET_EVENT_CALLS += 1
    raise AssertionError("pre-poisoned JournalStore.get_event code executed")


class HostileScopeText(str):
    def __str__(self):
        global HOSTILE_SCOPE_TEXT_CALLS
        HOSTILE_SCOPE_TEXT_CALLS += 1
        raise AssertionError("hostile scope text __str__ executed")

    def __eq__(self, _other):
        global HOSTILE_SCOPE_TEXT_CALLS
        HOSTILE_SCOPE_TEXT_CALLS += 1
        raise AssertionError("hostile scope text __eq__ executed")

    def __hash__(self):
        global HOSTILE_SCOPE_TEXT_CALLS
        HOSTILE_SCOPE_TEXT_CALLS += 1
        raise AssertionError("hostile scope text __hash__ executed")

    def strip(self, *_args, **_kwargs):
        global HOSTILE_SCOPE_TEXT_CALLS
        HOSTILE_SCOPE_TEXT_CALLS += 1
        raise AssertionError("hostile scope text strip executed")


class HostileJournalStore(JournalStore):
    def load_events(self, *_args, **_kwargs):
        raise AssertionError("JournalStore subclass dispatch reached model budget")


class HostileDurableModelBudget(DurableModelBudget):
    pass


class ModelBudgetTrustedJournalTopologyAuthorityTests(unittest.TestCase):
    def test_constructor_rejects_model_budget_subclass_before_durable_read(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            with self.assertRaisesRegex(
                TypeError,
                r"model budget must be exact DurableModelBudget",
            ):
                HostileDurableModelBudget(
                    journal=journal,
                    budget_id="exact-budget-type",
                    ceiling="5",
                    environment="PAPER",
                    clock=lambda: NOW_TEXT,
                )
            self.assertEqual(
                journal.load_events("model_budget", "exact-budget-type"),
                [],
            )

    def test_constructor_rejects_journal_store_subclass_before_dispatch(self):
        with TemporaryDirectory() as directory:
            journal = HostileJournalStore(Path(directory) / "journal.db")
            with self.assertRaisesRegex(
                TypeError,
                r"model budget journal must be exact JournalStore",
            ):
                DurableModelBudget(
                    journal=journal,
                    budget_id="exact-journal-type-budget",
                    ceiling="5",
                    environment="PAPER",
                    clock=lambda: NOW_TEXT,
                )

    def test_constructor_rejects_instance_method_shadow_before_dispatch(self):
        forged_calls = []

        def hostile_load_events(*_args, **_kwargs):
            forged_calls.append("forged")
            self.fail("instance-shadowed load_events reached model budget")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            journal.load_events = hostile_load_events
            try:
                with self.assertRaisesRegex(
                    TypeError,
                    r"instance state is shadowed",
                ):
                    DurableModelBudget(
                        journal=journal,
                        budget_id="instance-shadow-budget",
                        ceiling="5",
                        environment="PAPER",
                        clock=lambda: NOW_TEXT,
                    )
            finally:
                del journal.load_events
            self.assertEqual(forged_calls, [])
            self.assertEqual(
                journal.load_events("model_budget", "instance-shadow-budget"),
                [],
            )

    def test_reinitialization_is_rejected_before_authority_state_mutation(self):
        with TemporaryDirectory() as directory:
            first = JournalStore(Path(directory) / "first.db")
            second = JournalStore(Path(directory) / "second.db")
            budget = DurableModelBudget(
                journal=first,
                budget_id="reinit-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            original_state = dict(vars(budget))

            with self.assertRaisesRegex(
                ValueError,
                r"authority is already established",
            ):
                budget.__init__(
                    journal=second,
                    budget_id="retargeted-budget",
                    ceiling="500",
                    environment="LIVE",
                    clock=lambda: "2026-09-25T11:00:00+00:00",
                )

            self.assertEqual(vars(budget), original_state)
            self.assertTrue(budget.reserve("reinit-request", "0.2"))
            self.assertEqual(budget.snapshot().reserved, Decimal("0.2"))
            self.assertEqual(
                second.load_events("model_budget", "retargeted-budget"),
                [],
            )

    def test_budget_rejects_post_construction_journal_method_shadow(self):
        forged_calls = []

        def hostile_get_event(*_args, **_kwargs):
            forged_calls.append("forged")
            self.fail("post-construction get_event shadow reached dispatch")

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="post-shadow-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            journal.get_event = hostile_get_event
            try:
                with self.assertRaisesRegex(
                    TypeError,
                    r"instance state is shadowed",
                ):
                    budget.reserve("post-shadow-request", "0.2")
            finally:
                del journal.get_event

            self.assertEqual(forged_calls, [])
            self.assertEqual(budget.snapshot().reserved, 0)

    def test_budget_detects_in_place_journal_generation_identity_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="identity-mutation-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            raw_identity = vars(journal)["_store_identity"]
            if raw_identity.identity_source == "posix_stat":
                field = "filesystem_inode"
            else:
                field = "windows_file_index_low"
            original = getattr(raw_identity, field)
            self.assertIsInstance(original, int)
            object.__setattr__(raw_identity, field, original + 1)
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"JournalStore generation changed",
                ):
                    budget.reserve("identity-mutation-request", "0.2")
            finally:
                object.__setattr__(raw_identity, field, original)

            self.assertEqual(budget.snapshot().reserved, 0)

    def test_budget_rejects_journal_retarget_after_construction(self):
        with TemporaryDirectory() as directory:
            first = JournalStore(Path(directory) / "first.db")
            second = JournalStore(Path(directory) / "second.db")
            budget = DurableModelBudget(
                journal=first,
                budget_id="retarget-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            object.__setattr__(budget, "journal", second)

            with self.assertRaisesRegex(
                ValueError,
                r"journal authority changed after construction",
            ):
                budget.reserve("retarget-request", "0.2")

            self.assertEqual(
                first.load_events("model_budget", "retarget-budget")[-1]["event_type"],
                "ModelBudgetInitialized",
            )
            self.assertEqual(
                second.load_events("model_budget", "retarget-budget"),
                [],
            )

    def test_scope_mutation_is_rejected_before_hostile_text_callback(self):
        global HOSTILE_SCOPE_TEXT_CALLS
        HOSTILE_SCOPE_TEXT_CALLS = 0
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="inert-scope-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            object.__setattr__(
                budget,
                "budget_id",
                HostileScopeText("hostile-budget-id"),
            )

            with self.assertRaisesRegex(
                ValueError,
                r"scope authority changed after construction",
            ):
                budget.reserve("inert-scope-request", "0.2")

            self.assertEqual(HOSTILE_SCOPE_TEXT_CALLS, 0)
            events = journal.load_events("model_budget", "inert-scope-budget")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "ModelBudgetInitialized")

    def test_budget_rejects_scope_retarget_after_construction(self):
        mutations = (
            ("budget_id", "other-budget"),
            ("environment", "LIVE"),
            ("_ceiling", Decimal("500")),
            ("_clock", lambda: "2026-09-25T11:00:00+00:00"),
        )
        for index, (name, value) in enumerate(mutations):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                journal = JournalStore(Path(directory) / "journal.db")
                budget_id = f"scope-budget-{index}"
                budget = DurableModelBudget(
                    journal=journal,
                    budget_id=budget_id,
                    ceiling="5",
                    environment="PAPER",
                    clock=lambda: NOW_TEXT,
                )
                object.__setattr__(budget, name, value)

                with self.assertRaisesRegex(
                    ValueError,
                    r"scope authority changed after construction",
                ):
                    budget.reserve(f"scope-request-{index}", "0.2")

                events = journal.load_events("model_budget", budget_id)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "ModelBudgetInitialized")

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

    def test_prepoisoned_journal_method_code_is_restored_before_dispatch(self):
        global PREPOISONED_GET_EVENT_CALLS
        PREPOISONED_GET_EVENT_CALLS = 0
        canonical_get_event = JournalStore.get_event
        original_code = canonical_get_event.__code__
        original_defaults = canonical_get_event.__defaults__
        original_kwdefaults = canonical_get_event.__kwdefaults__

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="trusted-topology-method-code-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: NOW_TEXT,
            )
            try:
                canonical_get_event.__code__ = _poisoned_get_event.__code__
                with self.assertRaisesRegex(
                    ValueError,
                    r"model budget journal class authority is invalid:.*"
                    r"journal\.function\..*get_event\.__code__",
                ):
                    budget.reserve("trusted-topology-method-code-request", "0.2")
            finally:
                canonical_get_event.__code__ = original_code
                canonical_get_event.__defaults__ = original_defaults
                canonical_get_event.__kwdefaults__ = original_kwdefaults

            self.assertEqual(PREPOISONED_GET_EVENT_CALLS, 0)
            self.assertIs(canonical_get_event.__code__, original_code)
            self.assertEqual(budget.snapshot().reserved, 0)

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
