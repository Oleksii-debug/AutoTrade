from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.agent_coordinator import (
    AgentRole,
    AgentTask,
    SpecialistResult,
    aggregate_specialist_results,
    build_agent_plan,
)


NOW = datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)


class AgentCoordinatorTests(unittest.TestCase):
    def test_dependency_waves_are_deterministic(self):
        roles = [AgentRole("technical", True), AgentRole("critic", False)]
        tasks = [
            AgentTask("critic", "critic", "cut-1", depends_on=("features",), budget=Decimal("0.2")),
            AgentTask("features", "technical", "cut-1", budget=Decimal("0")),
        ]
        plan = build_agent_plan(
            roles=roles, tasks=tasks, maximum_budget=Decimal("1"), now_utc=NOW
        )
        self.assertEqual(
            [[step.task_id for step in wave] for wave in plan.waves],
            [["features"], ["critic"]],
        )
        self.assertEqual(plan.total_budget, Decimal("0.2"))

    def test_unknown_dependency_cycle_and_budget_fail_closed(self):
        roles = [AgentRole("r", True)]
        with self.assertRaisesRegex(ValueError, "unknown dependencies"):
            build_agent_plan(
                roles=roles,
                tasks=[AgentTask("a", "r", "v", depends_on=("missing",))],
                maximum_budget=Decimal("1"),
                now_utc=NOW,
            )
        with self.assertRaisesRegex(ValueError, "cycle"):
            build_agent_plan(
                roles=roles,
                tasks=[
                    AgentTask("a", "r", "v", depends_on=("b",)),
                    AgentTask("b", "r", "v", depends_on=("a",)),
                ],
                maximum_budget=Decimal("1"),
                now_utc=NOW,
            )
        with self.assertRaisesRegex(ValueError, "hard budget"):
            build_agent_plan(
                roles=roles,
                tasks=[AgentTask("a", "r", "v", budget=Decimal("2"))],
                maximum_budget=Decimal("1"),
                now_utc=NOW,
            )

    def test_required_expired_task_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "deadline expired"):
            build_agent_plan(
                roles=[AgentRole("r", True)],
                tasks=[
                    AgentTask(
                        "a",
                        "r",
                        "v",
                        deadline_utc=NOW - timedelta(seconds=1),
                    )
                ],
                maximum_budget=Decimal("1"),
                now_utc=NOW,
            )

    def test_non_execution_role_cannot_smuggle_execution_proposal(self):
        plan = build_agent_plan(
            roles=[AgentRole("critic", False, can_propose_execution=False)],
            tasks=[AgentTask("c", "critic", "cut")],
            maximum_budget=Decimal("0"),
            now_utc=NOW,
        )
        with self.assertRaisesRegex(ValueError, "cannot produce execution proposal"):
            aggregate_specialist_results(
                plan=plan,
                results=[
                    SpecialistResult(
                        "c", "cut", ("e1",), "avoid", Decimal("0.8"), {"side": "BUY"}
                    )
                ],
            )

    def test_aggregation_exposes_disagreement_and_missing_evidence(self):
        plan = build_agent_plan(
            roles=[AgentRole("a", True), AgentRole("b", True)],
            tasks=[
                AgentTask("a1", "a", "cut"),
                AgentTask("b1", "b", "cut"),
            ],
            maximum_budget=Decimal("0"),
            now_utc=NOW,
        )
        result = aggregate_specialist_results(
            plan=plan,
            results=[
                SpecialistResult("a1", "cut", ("e1",), "risk-high", Decimal("0.7")),
                SpecialistResult("b1", "cut", (), "risk-low", Decimal("0.6")),
            ],
        )
        self.assertEqual(result.status, "MISSING_EVIDENCE")
        self.assertEqual(result.disagreements, ("cut",))
        self.assertEqual(result.missing_evidence_tasks, ("b1",))

    def test_execution_proposal_is_only_structured_output_not_authority(self):
        plan = build_agent_plan(
            roles=[AgentRole("planner", False, can_propose_execution=True)],
            tasks=[AgentTask("p", "planner", "cut")],
            maximum_budget=Decimal("0"),
            now_utc=NOW,
        )
        result = aggregate_specialist_results(
            plan=plan,
            results=[
                SpecialistResult(
                    "p", "cut", ("e",), "proposal", Decimal("0.5"), {"side": "SELL"}
                )
            ],
        )
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.execution_proposals, ({"side": "SELL"},))


if __name__ == "__main__":
    unittest.main()
