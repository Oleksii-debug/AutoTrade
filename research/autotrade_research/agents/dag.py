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
    if not isinstance(value, datetime) or value.tzinfo is None:
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
        object.__setattr__(
            self,
            "required_inputs",
            tuple(_text(item, "required input") for item in self.required_inputs),
        )
        object.__setattr__(
            self,
            "dependencies",
            tuple(_text(item, "dependency") for item in self.dependencies),
        )
        if self.role_id in self.dependencies:
            raise SpecialistDagError("specialist cannot depend on itself")


@dataclass(frozen=True)
class SpecialistRun:
    role_id: str
    direction: Literal["LONG", "SHORT", "FLAT"]
    score: Decimal
    confidence: Decimal
    evidence_refs: tuple[str, ...]
    cost: Decimal
    completed_at: datetime
    critique: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role_id", _text(self.role_id, "role_id"))
        if self.direction not in {"LONG", "SHORT", "FLAT"}:
            raise SpecialistDagError("direction must be LONG, SHORT or FLAT")
        score = _decimal(self.score, "score")
        confidence = _decimal(self.confidence, "confidence", non_negative=True)
        if confidence > 1:
            raise SpecialistDagError("confidence cannot exceed one")
        if abs(score) > 1:
            raise SpecialistDagError("score magnitude cannot exceed one")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "confidence", confidence)
        refs = tuple(_text(item, "evidence reference") for item in self.evidence_refs)
        if not refs:
            raise SpecialistDagError("specialist output requires evidence")
        if len(set(refs)) != len(refs):
            raise SpecialistDagError("duplicate evidence references are not allowed")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "cost", _decimal(self.cost, "cost", non_negative=True))
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))
        object.__setattr__(
            self, "critique", tuple(_text(item, "critique") for item in self.critique)
        )


@dataclass(frozen=True)
class DagPlan:
    scheduled_roles: tuple[str, ...]
    skipped_roles: tuple[tuple[str, str], ...]
    reserved_cost: Decimal


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
    available_inputs: Iterable[str],
    total_budget,
) -> DagPlan:
    """Choose useful dependency-satisfied roles without exceeding the hard budget."""

    budget = _decimal(total_budget, "total_budget", non_negative=True)
    items = tuple(specs)
    by_id = {item.role_id: item for item in items}
    if len(by_id) != len(items):
        raise SpecialistDagError("specialist role ids must be unique")

    available = set(available_inputs)
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

    return DagPlan(tuple(scheduled), tuple(skipped), reserved)


def aggregate_specialists(
    specs: Iterable[SpecialistSpec],
    runs: Iterable[SpecialistRun],
    *,
    plan: DagPlan,
    decision_deadline: datetime,
    blocking_critique_terms: Iterable[str] = (),
) -> AggregatedProposal:
    """Aggregate by correlation group so cloned/correlated roles cannot outvote evidence."""

    deadline = _utc(decision_deadline, "decision_deadline")
    by_id = {spec.role_id: spec for spec in specs}
    if not by_id:
        raise SpecialistDagError("at least one specialist specification is required")
    if not isinstance(plan, DagPlan):
        raise SpecialistDagError("aggregation requires the exact DagPlan")
    scheduled_roles = set(plan.scheduled_roles)
    if len(scheduled_roles) != len(plan.scheduled_roles):
        raise SpecialistDagError("DagPlan scheduled roles must be unique")
    unknown_scheduled = scheduled_roles - set(by_id)
    if unknown_scheduled:
        raise SpecialistDagError(
            f"DagPlan contains unknown scheduled roles: {sorted(unknown_scheduled)}"
        )
    expected_reserved = sum(
        (by_id[role_id].max_cost for role_id in scheduled_roles),
        Decimal("0"),
    )
    if plan.reserved_cost != expected_reserved:
        raise SpecialistDagError("DagPlan reserved cost does not match scheduled roles")

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
        if run.role_id not in scheduled_roles:
            rejected.append((run.role_id, "not_scheduled"))
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
