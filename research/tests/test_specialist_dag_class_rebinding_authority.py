from datetime import datetime, timezone
from decimal import Decimal
import unittest

from autotrade_research.agents.dag import (
    DagPlan,
    SpecialistDagError,
    SpecialistRun,
    SpecialistSpec,
    aggregate_specialists,
    plan_specialists,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)


def _spec() -> SpecialistSpec:
    return SpecialistSpec(
        role_id="signal",
        correlation_group="independent",
        max_cost=Decimal("1"),
        expected_incremental_value=Decimal("1"),
        required_inputs=(),
        dependencies=(),
    )


def _run() -> SpecialistRun:
    return SpecialistRun(
        role_id="signal",
        input_snapshot_id="cut-1",
        direction="LONG",
        score=Decimal("0.5"),
        confidence=Decimal("1"),
        evidence_refs=("evidence:signal",),
        cost=Decimal("1"),
        completed_at=NOW,
        critique=(),
    )


def _plan() -> DagPlan:
    return DagPlan(
        input_snapshot_id="cut-1",
        scheduled_roles=("signal",),
        skipped_roles=(),
        reserved_cost=Decimal("1"),
        available_inputs=(),
        total_budget=Decimal("1"),
    )


class SpecialistDagClassRebindingAuthorityTests(unittest.TestCase):
    def _assert_rebound_accessor_is_never_executed(self, value_type, invoke):
        calls = []
        original = type.__getattribute__(value_type, "__getattribute__")

        def hostile_getattribute(instance, name):
            calls.append(name)
            raise AssertionError("rebound canonical class accessor must not execute")

        type.__setattr__(value_type, "__getattribute__", hostile_getattribute)
        try:
            with self.assertRaises(SpecialistDagError):
                invoke()
        finally:
            type.__setattr__(value_type, "__getattribute__", original)
        self.assertEqual(calls, [])

    def test_spec_accessor_rebinding_fails_closed_before_callback(self):
        value = _spec()
        self._assert_rebound_accessor_is_never_executed(
            SpecialistSpec,
            lambda: plan_specialists(
                [value],
                input_snapshot_id="cut-1",
                available_inputs=(),
                total_budget=Decimal("1"),
            ),
        )

    def test_run_accessor_rebinding_fails_closed_before_callback(self):
        value = _run()
        self._assert_rebound_accessor_is_never_executed(
            SpecialistRun,
            lambda: aggregate_specialists(
                [_spec()],
                [value],
                plan=_plan(),
                input_snapshot_id="cut-1",
                available_inputs=(),
                total_budget=Decimal("1"),
                decision_deadline=NOW,
            ),
        )

    def test_plan_accessor_rebinding_fails_closed_before_callback(self):
        value = _plan()
        self._assert_rebound_accessor_is_never_executed(
            DagPlan,
            lambda: aggregate_specialists(
                [_spec()],
                [_run()],
                plan=value,
                input_snapshot_id="cut-1",
                available_inputs=(),
                total_budget=Decimal("1"),
                decision_deadline=NOW,
            ),
        )


if __name__ == "__main__":
    unittest.main()
