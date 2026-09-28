"""Budgeted specialist DAG and evidence-bound aggregation.

This research primitive schedules only roles with measured positive incremental
value, caps correlated influence, rejects late/unevidenced outputs, and never
grants risk or trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal, Mapping


class SpecialistDagError(ValueError):
    pass


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecialistDagError(f"{name} is required")
    return value.strip()


def _decimal(value, name: str, *, non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise SpecialistDagError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise SpecialistDagError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise SpecialistDagError(f"{name} must be a finite decimal")
    if non_negative and result < 0:
        raise SpecialistDagError(f"{name} cannot be negative")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SpecialistDagError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class SpecialistSpec:
    role_id: str
    correlation_group: str
    max_cost: Decimal
    expected_incremental_value: Decimal
    required_inputs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role_id", _text(self.role_id, "role_id"))
        object.__setattr__(
            self, "correlation_group", _text(self.correlation_group, "correlation_group")
        )
        object.__setattr__(self, "max_cost", _decimal(self.max_cost, "max_cost", non_negative=True))
        object.__setattr__(
            self,
            "expected_incremental_value",
            _decimal(self.expected_incremental_value, "expected_incremental_value"),
        )
        if not isinstance(self.required_inputs, tuple):
            raise SpecialistDagError("required_inputs must be a tuple")
        if not isinstance(self.dependencies, tuple):
            raise SpecialistDagError("dependencies must be a tuple")
        required_inputs = tuple(
            _text(item, "required input") for item in self.required_inputs
        )
        dependencies = tuple(
            _text(item, "dependency") for item in self.dependencies
        )
        if len(required_inputs) != len(set(required_inputs)):
            raise SpecialistDagError("required_inputs must not contain duplicates")
        if len(dependencies) != len(set(dependencies)):
            raise SpecialistDagError("dependencies must not contain duplicates")
        object.__setattr__(self, "required_inputs", required_inputs)
        object.__setattr__(self, "dependencies", dependencies)
        if self.role_id in self.dependencies:
            raise SpecialistDagError("specialist cannot depend on itself")


@dataclass(frozen=True)
class SpecialistRun:
    role_id: str
    input_snapshot_id: str
    direction: Literal["LONG", "SHORT", "FLAT"]
    score: Decimal
    confidence: Decimal
    evidence_refs: tuple[str, ...]
    cost: Decimal
    completed_at: datetime
    critique: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role_id", _text(self.role_id, "role_id"))
        object.__setattr__(
            self,
            "input_snapshot_id",
            _text(self.input_snapshot_id, "input_snapshot_id"),
        )
        if self.direction not in {"LONG", "SHORT", "FLAT"}:
            raise SpecialistDagError("direction must be LONG, SHORT or FLAT")
        score = _decimal(self.score, "score")
        confidence = _decimal(self.confidence, "confidence", non_negative=True)
        if confidence > 1:
            raise SpecialistDagError("confidence cannot exceed one")
        if abs(score) > 1:
            raise SpecialistDagError("score magnitude cannot exceed one")
        expected_direction = "LONG" if score > 0 else "SHORT" if score < 0 else "FLAT"
        if self.direction != expected_direction:
            raise SpecialistDagError("direction must match score sign")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "confidence", confidence)
        if not isinstance(self.evidence_refs, tuple):
            raise SpecialistDagError("evidence_refs must be a tuple")
        refs = tuple(_text(item, "evidence reference") for item in self.evidence_refs)
        if not refs:
            raise SpecialistDagError("specialist output requires evidence")
        if len(set(refs)) != len(refs):
            raise SpecialistDagError("duplicate evidence references are not allowed")
        if not isinstance(self.critique, tuple):
            raise SpecialistDagError("critique must be a tuple")
        critique = tuple(_text(item, "critique") for item in self.critique)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "cost", _decimal(self.cost, "cost", non_negative=True))
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))
        object.__setattr__(self, "critique", critique)


@dataclass(frozen=True)
class DagPlan:
    input_snapshot_id: str
    scheduled_roles: tuple[str, ...]
    skipped_roles: tuple[tuple[str, str], ...]
    reserved_cost: Decimal
    available_inputs: tuple[str, ...]
    total_budget: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_snapshot_id",
            _text(self.input_snapshot_id, "input_snapshot_id"),
        )
        for name in ("scheduled_roles", "skipped_roles", "available_inputs"):
            if not isinstance(getattr(self, name), tuple):
                raise SpecialistDagError(f"{name} must be a tuple")
        scheduled = tuple(_text(item, "scheduled role") for item in self.scheduled_roles)
        if len(scheduled) != len(set(scheduled)):
            raise SpecialistDagError("scheduled_roles must not contain duplicates")
        available = tuple(_text(item, "available input") for item in self.available_inputs)
        if len(available) != len(set(available)):
            raise SpecialistDagError("available_inputs must not contain duplicates")
        skipped: list[tuple[str, str]] = []
        for item in self.skipped_roles:
            if not isinstance(item, tuple) or len(item) != 2:
                raise SpecialistDagError("skipped_roles entries must be role/reason tuples")
            skipped.append(
                (_text(item[0], "skipped role"), _text(item[1], "skip reason"))
            )
        reserved = _decimal(self.reserved_cost, "reserved_cost", non_negative=True)
        budget = _decimal(self.total_budget, "total_budget", non_negative=True)
        if reserved > budget:
            raise SpecialistDagError("reserved_cost cannot exceed total_budget")
        object.__setattr__(self, "scheduled_roles", scheduled)
        object.__setattr__(self, "skipped_roles", tuple(skipped))
        object.__setattr__(self, "available_inputs", available)
        object.__setattr__(self, "reserved_cost", reserved)
        object.__setattr__(self, "total_budget", budget)


@dataclass(frozen=True)
class AggregatedProposal:
    direction: Literal["LONG", "SHORT", "FLAT"]
    score: Decimal
    accepted_roles: tuple[str, ...]
    rejected_roles: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]
    total_cost: Decimal
    live_authority_granted: bool = False


def plan_specialists(
    specs: Iterable[SpecialistSpec],
    *,
    input_snapshot_id: str,
    available_inputs: Iterable[str],
    total_budget,
) -> DagPlan:
    """Choose useful dependency-satisfied roles without exceeding the hard budget."""

    snapshot_id = _text(input_snapshot_id, "input_snapshot_id")
    budget = _decimal(total_budget, "total_budget", non_negative=True)
    items = tuple(specs)
    by_id = {item.role_id: item for item in items}
    if len(by_id) != len(items):
        raise SpecialistDagError("specialist role ids must be unique")

    available = {_text(item, "available input") for item in available_inputs}
    scheduled: list[str] = []
    skipped: list[tuple[str, str]] = []
    reserved = Decimal("0")
    unresolved = set(by_id)

    while unresolved:
        progressed = False
        for role_id in sorted(tuple(unresolved)):
            spec = by_id[role_id]
            unknown_dependencies = set(spec.dependencies) - set(by_id)
            if unknown_dependencies:
                raise SpecialistDagError(
                    f"unknown dependencies for {role_id}: {sorted(unknown_dependencies)}"
                )
            if any(dep in unresolved for dep in spec.dependencies):
                continue
            progressed = True
            unresolved.remove(role_id)
            if any(dep not in scheduled for dep in spec.dependencies):
                skipped.append((role_id, "dependency_not_scheduled"))
                continue
            missing = sorted(set(spec.required_inputs) - available)
            if missing:
                skipped.append((role_id, "missing_inputs"))
                continue
            if spec.expected_incremental_value <= 0:
                skipped.append((role_id, "non_positive_incremental_value"))
                continue
            if reserved + spec.max_cost > budget:
                skipped.append((role_id, "budget_exceeded"))
                continue
            scheduled.append(role_id)
            reserved += spec.max_cost
        if not progressed:
            raise SpecialistDagError("specialist dependency graph contains a cycle")

    return DagPlan(
        snapshot_id,
        tuple(scheduled),
        tuple(skipped),
        reserved,
        tuple(sorted(available)),
        budget,
    )


def aggregate_specialists(
    specs: Iterable[SpecialistSpec],
    runs: Iterable[SpecialistRun],
    *,
    plan: DagPlan,
    input_snapshot_id: str,
    available_inputs: Iterable[str],
    total_budget,
    decision_deadline: datetime,
    blocking_critique_terms: Iterable[str] = (),
) -> AggregatedProposal:
    """Aggregate only outputs admitted by the exact canonical specialist plan."""

    deadline = _utc(decision_deadline, "decision_deadline")
    spec_items = tuple(specs)
    by_id = {spec.role_id: spec for spec in spec_items}
    if not by_id:
        raise SpecialistDagError("at least one specialist specification is required")
    if len(by_id) != len(spec_items):
        raise SpecialistDagError("specialist role ids must be unique")
    if not isinstance(plan, DagPlan):
        raise SpecialistDagError("plan must be a DagPlan")

    snapshot_id = _text(input_snapshot_id, "input_snapshot_id")
    canonical_plan = plan_specialists(
        spec_items,
        input_snapshot_id=snapshot_id,
        available_inputs=available_inputs,
        total_budget=total_budget,
    )
    if plan != canonical_plan:
        raise SpecialistDagError(
            "DagPlan does not match canonical planner output for this context"
        )
    scheduled = tuple(plan.scheduled_roles)
    scheduled_set = set(scheduled)

    seen: set[str] = set()
    accepted: list[tuple[SpecialistSpec, SpecialistRun]] = []
    rejected: list[tuple[str, str]] = []
    blockers = {str(item).strip().lower() for item in blocking_critique_terms if str(item).strip()}

    for run in runs:
        if run.role_id in seen:
            raise SpecialistDagError("duplicate specialist result")
        seen.add(run.role_id)
        spec = by_id.get(run.role_id)
        if spec is None:
            rejected.append((run.role_id, "unknown_role"))
            continue
        if run.role_id not in scheduled_set:
            rejected.append((run.role_id, "not_scheduled"))
            continue
        if run.input_snapshot_id != snapshot_id:
            rejected.append((run.role_id, "input_snapshot_mismatch"))
            continue
        if run.completed_at > deadline:
            rejected.append((run.role_id, "late"))
            continue
        if run.cost > spec.max_cost:
            rejected.append((run.role_id, "cost_exceeded"))
            continue
        if blockers and any(
            any(term in critique.lower() for term in blockers)
            for critique in run.critique
        ):
            rejected.append((run.role_id, "blocking_critique"))
            continue
        accepted.append((spec, run))

    for role_id in scheduled:
        if role_id not in seen:
            rejected.append((role_id, "missing_result"))

    if not accepted:
        return AggregatedProposal(
            direction="FLAT",
            score=Decimal("0"),
            accepted_roles=(),
            rejected_roles=tuple(rejected),
            evidence_refs=(),
            total_cost=Decimal("0"),
        )

    grouped: dict[str, list[SpecialistRun]] = {}
    for spec, run in accepted:
        grouped.setdefault(spec.correlation_group, []).append(run)

    group_scores: list[Decimal] = []
    for group_runs in grouped.values():
        weight_sum = sum((run.confidence for run in group_runs), Decimal("0"))
        if weight_sum == 0:
            group_scores.append(Decimal("0"))
            continue
        group_scores.append(
            sum((run.score * run.confidence for run in group_runs), Decimal("0"))
            / weight_sum
        )

    aggregate = sum(group_scores, Decimal("0")) / Decimal(len(group_scores))
    if aggregate > 0:
        direction: Literal["LONG", "SHORT", "FLAT"] = "LONG"
    elif aggregate < 0:
        direction = "SHORT"
    else:
        direction = "FLAT"

    refs = tuple(sorted({ref for _, run in accepted for ref in run.evidence_refs}))
    total_cost = sum((run.cost for _, run in accepted), Decimal("0"))
    return AggregatedProposal(
        direction=direction,
        score=aggregate,
        accepted_roles=tuple(sorted(run.role_id for _, run in accepted)),
        rejected_roles=tuple(rejected),
        evidence_refs=refs,
        total_cost=total_cost,
        live_authority_granted=False,
    )


def marginal_value(
    *,
    full_utility,
    ablated_utility,
    incremental_cost,
) -> Decimal:
    """Measured net contribution used for future role scheduling, never for authority."""

    return (
        _decimal(full_utility, "full_utility")
        - _decimal(ablated_utility, "ablated_utility")
        - _decimal(incremental_cost, "incremental_cost", non_negative=True)
    )
