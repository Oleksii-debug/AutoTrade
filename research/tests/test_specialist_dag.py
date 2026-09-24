from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from autotrade_research.agents.dag import (
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


class SpecialistDagTests(unittest.TestCase):
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

    def test_correlated_clones_do_not_outvote_independent_group(self):
        specs = [
            spec("clone1", "same-source"),
            spec("clone2", "same-source"),
            spec("independent", "independent-source"),
        ]
        result = aggregate_specialists(
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
        result = aggregate_specialists(
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
        result = aggregate_specialists(
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
        result = aggregate_specialists(
            specs,
            [run("admitted", "-0.2"), run("rejected", "1")],
            plan=plan,
            decision_deadline=NOW,
        )
        self.assertEqual(result.direction, "SHORT")
        self.assertEqual(result.accepted_roles, ("admitted",))
        self.assertIn(("rejected", "not_scheduled"), result.rejected_roles)

    def test_missing_planned_result_is_explicit_evidence(self):
        specs = [spec("a", "g1"), spec("b", "g2")]
        result = aggregate_specialists(
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
        )
        with self.assertRaisesRegex(SpecialistDagError, "reserved cost"):
            aggregate_specialists(
                specs,
                [run("a", "1")],
                plan=tampered,
                decision_deadline=NOW,
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
        result = aggregate_specialists(
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
