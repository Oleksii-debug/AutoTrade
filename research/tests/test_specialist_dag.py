from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, Inexact, Rounded, localcontext
import unittest

from autotrade_research.agents.dag import (
    AggregatedProposal,
    DagPlan,
    SpecialistDagError,
    SpecialistRun,
    SpecialistSpec,
    aggregate_specialists,
    marginal_value,
    plan_specialists,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)


def spec(role, group, cost="1", value="1", inputs=(), dependencies=()):
    return SpecialistSpec(
        role_id=role,
        correlation_group=group,
        max_cost=cost,
        expected_incremental_value=value,
        required_inputs=tuple(inputs),
        dependencies=tuple(dependencies),
    )


def run(role, score, *, confidence="1", cost="1", late=False, critique=()):
    return SpecialistRun(
        role_id=role,
        input_snapshot_id="cut-1",
        direction="LONG" if Decimal(score) > 0 else "SHORT" if Decimal(score) < 0 else "FLAT",
        score=Decimal(score),
        confidence=Decimal(confidence),
        evidence_refs=(f"evidence:{role}",),
        cost=Decimal(cost),
        completed_at=NOW + (timedelta(seconds=1) if late else timedelta(0)),
        critique=tuple(critique),
    )


def full_plan(specs, *, inputs=(), budget="100"):
    return plan_specialists(
        specs,
        input_snapshot_id="cut-1",
        available_inputs=inputs,
        total_budget=budget,
    )


def aggregate(
    specs,
    runs,
    *,
    plan,
    inputs=(),
    budget="100",
    snapshot_id="cut-1",
    decision_deadline=NOW,
    blocking_critique_terms=(),
):
    return aggregate_specialists(
        specs,
        runs,
        plan=plan,
        input_snapshot_id=snapshot_id,
        available_inputs=inputs,
        total_budget=budget,
        decision_deadline=decision_deadline,
        blocking_critique_terms=blocking_critique_terms,
    )


class SpecialistDagTests(unittest.TestCase):


    def test_outer_collection_subclasses_are_rejected_before_iteration(self):
        calls = []

        class HostileList(list):
            def __iter__(self):
                calls.append("iter")
                raise AssertionError("caller iterator must not execute")

        with self.assertRaisesRegex(
            SpecialistDagError,
            "specs must be an exact built-in list or tuple",
        ):
            plan_specialists(
                HostileList([spec("a", "g1")]),
                input_snapshot_id="cut-1",
                available_inputs=(),
                total_budget="1",
            )
        self.assertEqual(calls, [])

        with self.assertRaisesRegex(
            SpecialistDagError,
            "available_inputs must be an exact built-in list or tuple",
        ):
            plan_specialists(
                [spec("a", "g1")],
                input_snapshot_id="cut-1",
                available_inputs=HostileList(["price"]),
                total_budget="1",
            )
        self.assertEqual(calls, [])

        specs = [spec("a", "g1")]
        plan = full_plan(specs)
        with self.assertRaisesRegex(
            SpecialistDagError,
            "runs must be an exact built-in list or tuple",
        ):
            aggregate(
                specs,
                HostileList([run("a", "0.5")]),
                plan=plan,
            )
        self.assertEqual(calls, [])

        with self.assertRaisesRegex(
            SpecialistDagError,
            "blocking_critique_terms must be an exact built-in list or tuple",
        ):
            aggregate(
                specs,
                [run("a", "0.5")],
                plan=plan,
                blocking_critique_terms=HostileList(["future leakage"]),
            )
        self.assertEqual(calls, [])

    def test_generators_are_rejected_before_their_body_executes(self):
        calls = []

        def hostile_specs():
            calls.append("specs")
            yield spec("a", "g1")

        with self.assertRaisesRegex(
            SpecialistDagError,
            "specs must be an exact built-in list or tuple",
        ):
            plan_specialists(
                hostile_specs(),
                input_snapshot_id="cut-1",
                available_inputs=(),
                total_budget="1",
            )
        self.assertEqual(calls, [])

        def hostile_runs():
            calls.append("runs")
            yield run("a", "0.5")

        specs = [spec("a", "g1")]
        with self.assertRaisesRegex(
            SpecialistDagError,
            "runs must be an exact built-in list or tuple",
        ):
            aggregate(
                specs,
                hostile_runs(),
                plan=full_plan(specs),
            )
        self.assertEqual(calls, [])

    def test_time_ingress_rejects_custom_tzinfo_without_callbacks(self):
        calls = []

        class HostileTz(tzinfo):
            def utcoffset(self, dt):
                calls.append("utcoffset")
                raise AssertionError("caller timezone callback must not execute")

            def dst(self, dt):
                calls.append("dst")
                raise AssertionError("caller timezone callback must not execute")

            def fromutc(self, dt):
                calls.append("fromutc")
                raise AssertionError("caller timezone callback must not execute")

        hostile_time = datetime(2026, 9, 24, 18, tzinfo=HostileTz())
        with self.assertRaisesRegex(
            SpecialistDagError,
            "built-in timezone",
        ):
            SpecialistRun(
                role_id="hostile-time",
                input_snapshot_id="cut-1",
                direction="LONG",
                score="0.5",
                confidence="1",
                evidence_refs=("evidence:hostile-time",),
                cost="0",
                completed_at=hostile_time,
            )
        self.assertEqual(calls, [])

        specs = [spec("a", "g1")]
        with self.assertRaisesRegex(
            SpecialistDagError,
            "built-in timezone",
        ):
            aggregate(
                specs,
                [run("a", "0.5")],
                plan=full_plan(specs),
                decision_deadline=hostile_time,
            )
        self.assertEqual(calls, [])

    def test_time_ingress_preserves_builtin_fixed_offset_normalization(self):
        fixed = timezone(timedelta(hours=2))
        local_time = datetime(2026, 9, 24, 20, tzinfo=fixed)
        result = SpecialistRun(
            role_id="fixed-offset",
            input_snapshot_id="cut-1",
            direction="LONG",
            score="0.5",
            confidence="1",
            evidence_refs=("evidence:fixed-offset",),
            cost="0",
            completed_at=local_time,
        )
        self.assertEqual(result.completed_at, NOW)

    def test_mutated_run_custom_timezone_is_rejected_on_aggregation_readmission(self):
        calls = []

        class HostileTz(tzinfo):
            def utcoffset(self, dt):
                calls.append("utcoffset")
                raise AssertionError("caller timezone callback must not execute")

            def dst(self, dt):
                calls.append("dst")
                raise AssertionError("caller timezone callback must not execute")

        value = run("a", "0.5")
        object.__setattr__(
            value,
            "completed_at",
            datetime(2026, 9, 24, 18, tzinfo=HostileTz()),
        )
        specs = [spec("a", "g1")]
        with self.assertRaisesRegex(
            SpecialistDagError,
            "built-in timezone",
        ):
            aggregate(
                specs,
                [value],
                plan=full_plan(specs),
                decision_deadline=NOW,
            )
        self.assertEqual(calls, [])

    def test_string_collections_cannot_masquerade_as_specialist_evidence(self):
        with self.assertRaisesRegex(SpecialistDagError, "required_inputs must be a tuple"):
            SpecialistSpec(
                role_id="research",
                correlation_group="g",
                max_cost=Decimal("1"),
                expected_incremental_value=Decimal("1"),
                required_inputs="news",
            )
        with self.assertRaisesRegex(SpecialistDagError, "evidence_refs must be a tuple"):
            SpecialistRun(
                role_id="research",
                input_snapshot_id="cut-1",
                direction="LONG",
                score=Decimal("0.5"),
                confidence=Decimal("1"),
                evidence_refs="artifact:proof",
                cost=Decimal("0"),
                completed_at=NOW,
            )

    def test_numeric_authority_rejects_oversized_inputs_before_decimal_construction(self):
        oversized_text = "9" * 10_000
        oversized_integer = 1 << 40_000
        hostile_exponent = "1e999999999999999999999999999"

        for value in (oversized_text, oversized_integer, hostile_exponent):
            with self.subTest(value_type=type(value).__name__), self.assertRaisesRegex(
                SpecialistDagError,
                "bounded exact decimal input",
            ):
                SpecialistSpec(
                    role_id="resource-bound",
                    correlation_group="g",
                    max_cost=value,
                    expected_incremental_value="1",
                )

            with self.subTest(
                budget_type=type(value).__name__
            ), self.assertRaisesRegex(
                SpecialistDagError,
                "bounded exact decimal input",
            ):
                plan_specialists(
                    [spec("a", "g")],
                    input_snapshot_id="cut-1",
                    available_inputs=(),
                    total_budget=value,
                )

            with self.subTest(
                marginal_type=type(value).__name__
            ), self.assertRaisesRegex(
                SpecialistDagError,
                "bounded exact decimal input",
            ):
                marginal_value(
                    full_utility=value,
                    ablated_utility="0",
                    incremental_cost="0",
                )

    def test_decimal_subclass_cannot_invoke_caller_defined_numeric_semantics(self):
        calls = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                calls.append("is_finite")
                raise AssertionError("caller callback must not execute")

            def as_tuple(self):
                calls.append("as_tuple")
                raise AssertionError("caller callback must not execute")

        hostile = HostileDecimal("1")
        with self.assertRaisesRegex(
            SpecialistDagError,
            "bounded exact decimal input",
        ):
            SpecialistSpec(
                role_id="subclass",
                correlation_group="g",
                max_cost=hostile,
                expected_incremental_value="1",
            )
        self.assertEqual(calls, [])

    def test_shared_numeric_authority_preserves_valid_exact_inputs(self):
        planned = SpecialistSpec(
            role_id="valid",
            correlation_group="g",
            max_cost=2,
            expected_incremental_value="0.125",
        )
        self.assertEqual(planned.max_cost, Decimal("2"))
        self.assertEqual(planned.expected_incremental_value, Decimal("0.125"))
        self.assertEqual(
            marginal_value(
                full_utility=Decimal("1.25"),
                ablated_utility="0.5",
                incremental_cost=1,
            ),
            Decimal("-0.25"),
        )

    def test_dag_plan_rejects_non_exact_numeric_identity(self):
        canonical = full_plan([spec("a", "g1", cost="1")], budget="1")
        with self.assertRaisesRegex(SpecialistDagError, "exact decimal input"):
            type(canonical)(
                canonical.input_snapshot_id,
                canonical.scheduled_roles,
                canonical.skipped_roles,
                1.0,
                canonical.available_inputs,
                Decimal("1"),
            )

    def test_plan_runs_only_positive_value_roles_inside_budget(self):
        plan = plan_specialists(
            [
                spec("a", "price", cost="2", value="3", inputs=("price",)),
                spec("b", "news", cost="5", value="-1"),
                spec("c", "macro", cost="9", value="4"),
            ],
            input_snapshot_id="cut-1",
            available_inputs=("price",),
            total_budget="4",
        )
        self.assertEqual(plan.scheduled_roles, ("a",))
        self.assertIn(("b", "non_positive_incremental_value"), plan.skipped_roles)
        self.assertIn(("c", "budget_exceeded"), plan.skipped_roles)
        self.assertEqual(plan.reserved_cost, Decimal("2"))
        self.assertEqual(plan.available_inputs, ("price",))
        self.assertEqual(plan.total_budget, Decimal("4"))

    def test_dependency_cycle_fails_closed(self):
        with self.assertRaisesRegex(SpecialistDagError, "cycle"):
            plan_specialists(
                [spec("a", "g1", dependencies=("b",)), spec("b", "g2", dependencies=("a",))],
                input_snapshot_id="cut-1",
                available_inputs=(),
                total_budget="10",
            )

    def test_missing_dependency_or_input_cannot_sneak_through(self):
        plan = plan_specialists(
            [
                spec("base", "g1", inputs=("missing",)),
                spec("child", "g2", dependencies=("base",)),
            ],
            input_snapshot_id="cut-1",
            available_inputs=(),
            total_budget="10",
        )
        self.assertEqual(plan.scheduled_roles, ())
        self.assertEqual(
            plan.skipped_roles,
            (("base", "missing_inputs"), ("child", "dependency_not_scheduled")),
        )

    def test_planning_budget_is_independent_of_mutable_decimal_context(self):
        specs = [
            spec("a", "g1", cost="0.123456789", value="1"),
            spec("b", "g2", cost="0.123456789", value="1"),
        ]
        with localcontext() as context:
            context.prec = 4
            low_precision = full_plan(specs, budget="0.246913578")
        with localcontext() as context:
            context.prec = 50
            high_precision = full_plan(specs, budget="0.246913578")
        self.assertEqual(low_precision, high_precision)
        self.assertEqual(low_precision.scheduled_roles, ("a", "b"))
        self.assertEqual(low_precision.reserved_cost, Decimal("0.246913578"))

    def test_aggregation_is_independent_of_mutable_decimal_context(self):
        specs = [
            spec("a", "same"),
            spec("b", "same"),
        ]
        runs = [
            run("a", "1", confidence="0.5"),
            run("b", "0", confidence="1"),
        ]
        plan = full_plan(specs)
        with localcontext() as context:
            context.prec = 4
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            low_precision = aggregate(
                specs,
                runs,
                plan=plan,
                decision_deadline=NOW,
            )
        with localcontext() as context:
            context.prec = 50
            high_precision = aggregate(
                specs,
                runs,
                plan=plan,
                decision_deadline=NOW,
            )
        self.assertEqual(low_precision, high_precision)
        self.assertEqual(low_precision.direction, "LONG")
        self.assertGreater(low_precision.score, Decimal("0"))
        self.assertLess(low_precision.score, Decimal("1"))

    def test_confidence_probability_bounds_are_preserved(self):
        for confidence in ("0", "1"):
            with self.subTest(confidence=confidence):
                self.assertEqual(run("a", "1", confidence=confidence).confidence, Decimal(confidence))
        for confidence in ("-0.001", "1.0001", "2"):
            with self.subTest(confidence=confidence):
                with self.assertRaises(SpecialistDagError):
                    run("a", "1", confidence=confidence)

    def test_marginal_value_is_independent_of_mutable_decimal_context(self):
        with localcontext() as context:
            context.prec = 4
            low_precision = marginal_value(
                full_utility="1.000000009",
                ablated_utility="0.999999999",
                incremental_cost="0.000000001",
            )
        with localcontext() as context:
            context.prec = 50
            high_precision = marginal_value(
                full_utility="1.000000009",
                ablated_utility="0.999999999",
                incremental_cost="0.000000001",
            )
        self.assertEqual(low_precision, high_precision)
        self.assertEqual(low_precision, Decimal("0.000000009"))


    def test_zero_confidence_group_has_no_numerical_influence(self):
        specs = [
            spec("signal", "independent"),
            spec("zero", "zero-confidence"),
        ]
        result = aggregate(
            specs,
            [
                run("signal", "1", confidence="1"),
                run("zero", "-1", confidence="0"),
            ],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertEqual(result.score, Decimal("1"))
        self.assertEqual(result.direction, "LONG")
        self.assertEqual(result.accepted_roles, ("signal", "zero"))

    def test_all_zero_confidence_groups_are_flat_without_division(self):
        specs = [
            spec("long", "g1"),
            spec("short", "g2"),
        ]
        result = aggregate(
            specs,
            [
                run("long", "1", confidence="0"),
                run("short", "-1", confidence="0"),
            ],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertEqual(result.score, Decimal("0"))
        self.assertEqual(result.direction, "FLAT")
        self.assertEqual(result.accepted_roles, ("long", "short"))
        self.assertFalse(result.live_authority_granted)

    def test_correlated_clones_do_not_outvote_independent_group(self):
        specs = [
            spec("clone1", "same-source"),
            spec("clone2", "same-source"),
            spec("independent", "independent-source"),
        ]
        result = aggregate(
            specs,
            [run("clone1", "1"), run("clone2", "1"), run("independent", "-1")],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertEqual(result.score, Decimal("0"))
        self.assertEqual(result.direction, "FLAT")
        self.assertFalse(result.live_authority_granted)

    def test_late_and_over_budget_results_are_rejected(self):
        specs = [spec("late", "a"), spec("costly", "b", cost="1"), spec("ok", "c")]
        result = aggregate(
            specs,
            [
                run("late", "1", late=True),
                run("costly", "1", cost="2"),
                run("ok", "-0.5"),
            ],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertEqual(result.direction, "SHORT")
        self.assertIn(("late", "late"), result.rejected_roles)
        self.assertIn(("costly", "cost_exceeded"), result.rejected_roles)

    def test_blocking_critique_removes_affected_output(self):
        specs = [spec("a", "a"), spec("b", "b")]
        result = aggregate(
            specs,
            [
                run("a", "1", critique=("Future leakage detected",)),
                run("b", "-0.4"),
            ],
            plan=full_plan(specs),
            decision_deadline=NOW,
            blocking_critique_terms=("future leakage",),
        )
        self.assertEqual(result.direction, "SHORT")
        self.assertIn(("a", "blocking_critique"), result.rejected_roles)

    def test_unscheduled_known_role_cannot_influence_aggregation(self):
        specs = [
            spec("admitted", "independent", cost="1", value="1"),
            spec("rejected", "other", cost="1", value="-1"),
        ]
        plan = full_plan(specs)
        self.assertEqual(plan.scheduled_roles, ("admitted",))
        result = aggregate(
            specs,
            [run("admitted", "-0.2"), run("rejected", "1")],
            plan=plan,
            decision_deadline=NOW,
        )
        self.assertEqual(result.direction, "SHORT")
        self.assertEqual(result.accepted_roles, ("admitted",))
        self.assertIn(("rejected", "not_scheduled"), result.rejected_roles)

    def test_child_result_is_rejected_when_dependency_result_is_missing(self):
        specs = [
            spec("base", "g1"),
            spec("child", "g2", dependencies=("base",)),
        ]
        plan = full_plan(specs)
        self.assertEqual(plan.scheduled_roles, ("base", "child"))
        result = aggregate(
            specs,
            [run("child", "0.8")],
            plan=plan,
            decision_deadline=NOW,
        )
        self.assertEqual(result.accepted_roles, ())
        self.assertEqual(result.direction, "FLAT")
        self.assertIn(("base", "missing_result"), result.rejected_roles)
        self.assertIn(
            ("child", "dependency_result_unavailable"),
            result.rejected_roles,
        )

    def test_child_result_is_rejected_when_dependency_is_late(self):
        specs = [
            spec("base", "g1"),
            spec("child", "g2", dependencies=("base",)),
        ]
        result = aggregate(
            specs,
            [run("base", "0.5", late=True), run("child", "0.9")],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertEqual(result.accepted_roles, ())
        self.assertIn(("base", "late"), result.rejected_roles)
        self.assertIn(
            ("child", "dependency_result_unavailable"),
            result.rejected_roles,
        )

    def test_dependency_rejection_cascades_across_multiple_levels(self):
        specs = [
            spec("base", "g1"),
            spec("middle", "g2", dependencies=("base",)),
            spec("leaf", "g3", dependencies=("middle",)),
        ]
        result = aggregate(
            specs,
            [run("middle", "0.6"), run("leaf", "0.9")],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertEqual(result.accepted_roles, ())
        self.assertIn(("base", "missing_result"), result.rejected_roles)
        self.assertIn(
            ("middle", "dependency_result_unavailable"),
            result.rejected_roles,
        )
        self.assertIn(
            ("leaf", "dependency_result_unavailable"),
            result.rejected_roles,
        )

    def test_accepted_dependency_allows_child_to_contribute(self):
        specs = [
            spec("base", "same"),
            spec("child", "other", dependencies=("base",)),
        ]
        result = aggregate(
            specs,
            [run("child", "0.8"), run("base", "0.2")],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertEqual(result.accepted_roles, ("base", "child"))
        self.assertEqual(result.score, Decimal("0.5"))
        self.assertEqual(result.direction, "LONG")

    def test_missing_planned_result_is_explicit_evidence(self):
        specs = [spec("a", "g1"), spec("b", "g2")]
        result = aggregate(
            specs,
            [run("a", "0.4")],
            plan=full_plan(specs),
            decision_deadline=NOW,
        )
        self.assertIn(("b", "missing_result"), result.rejected_roles)

    def test_tampered_plan_cost_is_rejected(self):
        specs = [spec("a", "g1", cost="2")]
        plan = full_plan(specs)
        tampered = type(plan)(
            plan.input_snapshot_id,
            plan.scheduled_roles,
            plan.skipped_roles,
            Decimal("0"),
            plan.available_inputs,
            plan.total_budget,
        )
        with self.assertRaisesRegex(SpecialistDagError, "canonical planner output"):
            aggregate(
                specs,
                [run("a", "1")],
                plan=tampered,
                decision_deadline=NOW,
            )

    def test_forged_schedule_with_missing_required_input_is_rejected(self):
        specs = [spec("research", "g1", cost="1", value="2", inputs=("news",))]
        planned = full_plan(specs, inputs=(), budget="1")
        self.assertEqual(planned.scheduled_roles, ())
        forged = type(planned)(
            planned.input_snapshot_id,
            ("research",),
            (),
            Decimal("1"),
            planned.available_inputs,
            planned.total_budget,
        )
        with self.assertRaisesRegex(SpecialistDagError, "canonical planner output"):
            aggregate(
                specs,
                [run("research", "1")],
                plan=forged,
                budget="1",
                decision_deadline=NOW,
            )

    def test_forged_budget_context_is_rejected(self):
        specs = [spec("a", "g1", cost="2"), spec("b", "g2", cost="2")]
        plan = full_plan(specs, budget="2")
        self.assertEqual(plan.scheduled_roles, ("a",))
        forged = type(plan)(
            plan.input_snapshot_id,
            plan.scheduled_roles,
            plan.skipped_roles,
            plan.reserved_cost,
            plan.available_inputs,
            Decimal("4"),
        )
        with self.assertRaisesRegex(SpecialistDagError, "canonical planner output"):
            aggregate(
                specs,
                [run("a", "1")],
                plan=forged,
                budget="2",
                decision_deadline=NOW,
            )

    def test_coordinated_forged_budget_and_schedule_cannot_self_authenticate(self):
        specs = [spec("a", "g1", cost="2"), spec("b", "g2", cost="2")]
        trusted = full_plan(specs, budget="2")
        self.assertEqual(trusted.scheduled_roles, ("a",))
        forged = full_plan(specs, budget="4")
        self.assertEqual(forged.scheduled_roles, ("a", "b"))
        with self.assertRaisesRegex(SpecialistDagError, "canonical planner output"):
            aggregate(
                specs,
                [run("a", "0.4"), run("b", "0.6")],
                plan=forged,
                budget="2",
                decision_deadline=NOW,
            )

    def test_coordinated_forged_inputs_and_schedule_cannot_self_authenticate(self):
        specs = [spec("research", "g1", inputs=("news",))]
        trusted = full_plan(specs, inputs=(), budget="1")
        self.assertEqual(trusted.scheduled_roles, ())
        forged = full_plan(specs, inputs=("news",), budget="1")
        self.assertEqual(forged.scheduled_roles, ("research",))
        with self.assertRaisesRegex(SpecialistDagError, "canonical planner output"):
            aggregate(
                specs,
                [run("research", "0.5")],
                plan=forged,
                inputs=(),
                budget="1",
                decision_deadline=NOW,
            )

    def test_forged_plan_snapshot_identity_cannot_self_authenticate(self):
        specs = [spec("a", "g1")]
        forged = plan_specialists(
            specs,
            input_snapshot_id="cut-2",
            available_inputs=(),
            total_budget="100",
        )
        with self.assertRaisesRegex(SpecialistDagError, "canonical planner output"):
            aggregate(
                specs,
                [],
                plan=forged,
                snapshot_id="cut-1",
                decision_deadline=NOW,
            )

    def test_dag_plan_subclass_cannot_override_canonical_equality_fence(self):
        specs = [spec("a", "g1")]
        canonical = full_plan(specs)

        class ForgedPlan(DagPlan):
            def __eq__(self, other):
                return True

        forged = ForgedPlan(
            canonical.input_snapshot_id,
            ("forged",),
            (),
            Decimal("0"),
            canonical.available_inputs,
            canonical.total_budget,
        )
        with self.assertRaisesRegex(SpecialistDagError, "exact DagPlan"):
            aggregate(
                specs,
                [run("a", "0.5")],
                plan=forged,
                decision_deadline=NOW,
            )

    def test_dag_plan_class_equality_rebinding_cannot_self_authenticate(self):
        specs = [spec("a", "g1")]
        canonical = full_plan(specs)
        forged = DagPlan(
            canonical.input_snapshot_id,
            (),
            (("a", "budget_exceeded"),),
            Decimal("0"),
            canonical.available_inputs,
            canonical.total_budget,
        )
        calls = []
        original = type.__getattribute__(DagPlan, "__eq__")

        def hostile_eq(left, right):
            calls.append((left, right))
            return True

        type.__setattr__(DagPlan, "__eq__", hostile_eq)
        try:
            with self.assertRaisesRegex(
                SpecialistDagError,
                "canonical planner output",
            ):
                aggregate(
                    specs,
                    [run("a", "0.5")],
                    plan=forged,
                    decision_deadline=NOW,
                )
        finally:
            type.__setattr__(DagPlan, "__eq__", original)
        self.assertEqual(calls, [])

    def test_mutated_spec_is_readmitted_before_planning(self):
        value = spec("a", "g1", cost="1")
        object.__setattr__(value, "max_cost", "9" * 10_000)
        with self.assertRaisesRegex(
            SpecialistDagError,
            "bounded exact decimal input",
        ):
            full_plan([value])

    def test_specialist_subclasses_are_not_authority_values(self):
        class SpecSubclass(SpecialistSpec):
            pass

        class RunSubclass(SpecialistRun):
            pass

        forged_spec = SpecSubclass(
            role_id="a",
            correlation_group="g1",
            max_cost="1",
            expected_incremental_value="1",
        )
        with self.assertRaisesRegex(
            SpecialistDagError,
            "exact SpecialistSpec",
        ):
            full_plan([forged_spec])

        specs = [spec("a", "g1")]
        forged_run = RunSubclass(
            role_id="a",
            input_snapshot_id="cut-1",
            direction="LONG",
            score="0.5",
            confidence="1",
            evidence_refs=("evidence:a",),
            cost="1",
            completed_at=NOW,
        )
        with self.assertRaisesRegex(
            SpecialistDagError,
            "exact SpecialistRun",
        ):
            aggregate(
                specs,
                [forged_run],
                plan=full_plan(specs),
                decision_deadline=NOW,
            )

    def test_blocking_critique_terms_do_not_coerce_arbitrary_objects(self):
        specs = [spec("a", "g1")]

        class HostileTerm:
            def __str__(self):
                raise AssertionError("arbitrary str callback must not execute")

        with self.assertRaisesRegex(SpecialistDagError, "exact string"):
            aggregate(
                specs,
                [run("a", "0.5")],
                plan=full_plan(specs),
                decision_deadline=NOW,
                blocking_critique_terms=(HostileTerm(),),
            )

    def test_aggregated_proposal_can_never_claim_live_authority(self):
        with self.assertRaisesRegex(
            SpecialistDagError,
            "cannot grant live financial authority",
        ):
            AggregatedProposal(
                direction="FLAT",
                score=Decimal("0"),
                accepted_roles=(),
                rejected_roles=(),
                evidence_refs=(),
                total_cost=Decimal("0"),
                live_authority_granted=True,
            )

    def test_direction_must_match_score_sign(self):
        with self.assertRaisesRegex(SpecialistDagError, "direction must match score sign"):
            SpecialistRun(
                role_id="x",
                input_snapshot_id="cut-1",
                direction="SHORT",
                score=Decimal("0.4"),
                confidence=Decimal("1"),
                evidence_refs=("evidence:x",),
                cost=Decimal("0"),
                completed_at=NOW,
            )
        with self.assertRaisesRegex(SpecialistDagError, "direction must match score sign"):
            SpecialistRun(
                role_id="flat",
                input_snapshot_id="cut-1",
                direction="LONG",
                score=Decimal("0"),
                confidence=Decimal("1"),
                evidence_refs=("evidence:flat",),
                cost=Decimal("0"),
                completed_at=NOW,
            )

    def test_direct_aggregate_direction_matches_exact_score_sign(self):
        for score, expected in (("-1", "SHORT"), ("-1e-256", "SHORT"),
                                ("-0", "FLAT"), ("0", "FLAT"),
                                ("1e-256", "LONG"), ("1", "LONG")):
            for direction in ("LONG", "SHORT", "FLAT"):
                with self.subTest(score=score, direction=direction):
                    values = dict(direction=direction, score=Decimal(score),
                                  accepted_roles=(), rejected_roles=(),
                                  evidence_refs=(), total_cost=Decimal("0"))
                    if direction == expected:
                        self.assertEqual(AggregatedProposal(**values).direction, expected)
                    else:
                        with self.assertRaisesRegex(SpecialistDagError, "aggregate direction must match score sign"):
                            AggregatedProposal(**values)

    def test_output_requires_evidence_and_rejects_binary_floats(self):
        with self.assertRaises(SpecialistDagError):
            SpecialistRun(
                role_id="x",
                input_snapshot_id="cut-1",
                direction="LONG",
                score=Decimal("1"),
                confidence=Decimal("1"),
                evidence_refs=(),
                cost=Decimal("0"),
                completed_at=NOW,
            )
        with self.assertRaises(SpecialistDagError):
            SpecialistSpec(
                role_id="x",
                correlation_group="g",
                max_cost=1.0,
                expected_incremental_value=Decimal("1"),
            )

    def test_result_from_other_input_snapshot_is_rejected(self):
        specs = [spec("a", "g1")]
        plan = full_plan(specs)
        mismatched = SpecialistRun(
            role_id="a",
            input_snapshot_id="cut-2",
            direction="LONG",
            score=Decimal("0.4"),
            confidence=Decimal("1"),
            evidence_refs=("evidence:a",),
            cost=Decimal("0"),
            completed_at=NOW,
        )
        result = aggregate(
            specs,
            [mismatched],
            plan=plan,
            decision_deadline=NOW,
        )
        self.assertEqual(result.accepted_roles, ())
        self.assertIn(("a", "input_snapshot_mismatch"), result.rejected_roles)
        self.assertFalse(result.live_authority_granted)

    def test_marginal_value_accounts_for_incremental_cost(self):
        self.assertEqual(
            marginal_value(full_utility="10", ablated_utility="7", incremental_cost="1"),
            Decimal("2"),
        )


if __name__ == "__main__":
    unittest.main()
