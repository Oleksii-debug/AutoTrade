"""Budgeted specialist DAG and evidence-bound aggregation.

This research primitive schedules only roles with measured positive incremental
value, caps correlated influence, rejects late/unevidenced outputs, and never
grants risk or trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Iterable, Literal, Mapping

from autotrade_numeric import (
    MAX_SCALE,
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    exact_abs,
    exact_add,
    exact_subtract,
    parse_bounded_exact_decimal,
    round_fraction_to_quantum,
)


class SpecialistDagError(ValueError):
    pass


def _text(value: str, name: str) -> str:
    if type(value) is not str:
        raise SpecialistDagError(f"{name} must be an exact string")
    normalized = value.strip()
    if not normalized:
        raise SpecialistDagError(f"{name} is required")
    return normalized


def _decimal(value, name: str, *, non_negative: bool = False) -> Decimal:
    """Admit specialist numerics through the shared bounded exact authority."""

    try:
        result = parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise SpecialistDagError(
            f"{name} must use bounded exact decimal input"
        ) from error
    if non_negative and result < 0:
        raise SpecialistDagError(f"{name} cannot be negative")
    return result


_SCORE_QUANTUM = Decimal((0, (1,), -MAX_SCALE))


def _exact_add(left: Decimal, right: Decimal, name: str) -> Decimal:
    try:
        return exact_add(left, right)
    except ExactDecimalError as error:
        raise SpecialistDagError(
            f"{name} exceeds the exact numeric resource envelope"
        ) from error


def _fraction(value: Decimal, name: str) -> Fraction:
    try:
        return as_fraction(value)
    except ExactDecimalError as error:
        raise SpecialistDagError(
            f"{name} exceeds the exact numeric resource envelope"
        ) from error


def _bounded_fraction(value: Fraction, name: str) -> Fraction:
    try:
        return bounded_fraction(value)
    except ExactDecimalError as error:
        raise SpecialistDagError(
            f"{name} exceeds the exact numeric resource envelope"
        ) from error


def _score_decimal(value: Fraction) -> Decimal:
    try:
        return round_fraction_to_quantum(
            bounded_fraction(value),
            _SCORE_QUANTUM,
            mode="HALF_EVEN",
        )
    except ExactDecimalError as error:
        raise SpecialistDagError(
            "aggregate score exceeds the exact numeric resource envelope"
        ) from error


def _utc(value: datetime, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SpecialistDagError(f"{name} must be an exact timezone-aware datetime")
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
        if type(self.required_inputs) is not tuple:
            raise SpecialistDagError("required_inputs must be a tuple")
        if type(self.dependencies) is not tuple:
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
        if type(self.direction) is not str or self.direction not in {"LONG", "SHORT", "FLAT"}:
            raise SpecialistDagError("direction must be exact LONG, SHORT or FLAT")
        score = _decimal(self.score, "score")
        confidence = _decimal(self.confidence, "confidence", non_negative=True)
        if confidence > 1:
            raise SpecialistDagError("confidence cannot exceed one")
        try:
            score_magnitude = exact_abs(score)
        except ExactDecimalError as error:
            raise SpecialistDagError(
                "score exceeds the exact numeric resource envelope"
            ) from error
        if score_magnitude > 1:
            raise SpecialistDagError("score magnitude cannot exceed one")
        expected_direction = "LONG" if score > 0 else "SHORT" if score < 0 else "FLAT"
        if self.direction != expected_direction:
            raise SpecialistDagError("direction must match score sign")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "confidence", confidence)
        if type(self.evidence_refs) is not tuple:
            raise SpecialistDagError("evidence_refs must be a tuple")
        refs = tuple(_text(item, "evidence reference") for item in self.evidence_refs)
        if not refs:
            raise SpecialistDagError("specialist output requires evidence")
        if len(set(refs)) != len(refs):
            raise SpecialistDagError("duplicate evidence references are not allowed")
        if type(self.critique) is not tuple:
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
            if type(getattr(self, name)) is not tuple:
                raise SpecialistDagError(f"{name} must be an exact tuple")
        scheduled = tuple(_text(item, "scheduled role") for item in self.scheduled_roles)
        if len(scheduled) != len(set(scheduled)):
            raise SpecialistDagError("scheduled_roles must not contain duplicates")
        available = tuple(_text(item, "available input") for item in self.available_inputs)
        if len(available) != len(set(available)):
            raise SpecialistDagError("available_inputs must not contain duplicates")
        skipped: list[tuple[str, str]] = []
        for item in self.skipped_roles:
            if type(item) is not tuple or len(item) != 2:
                raise SpecialistDagError("skipped_roles entries must be exact role/reason tuples")
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


def _readmit_spec(value: object) -> SpecialistSpec:
    if type(value) is not SpecialistSpec:
        raise SpecialistDagError("specialist specification must be exact SpecialistSpec")
    return SpecialistSpec(
        role_id=value.role_id,
        correlation_group=value.correlation_group,
        max_cost=value.max_cost,
        expected_incremental_value=value.expected_incremental_value,
        required_inputs=value.required_inputs,
        dependencies=value.dependencies,
    )


def _readmit_run(value: object) -> SpecialistRun:
    if type(value) is not SpecialistRun:
        raise SpecialistDagError("specialist result must be exact SpecialistRun")
    return SpecialistRun(
        role_id=value.role_id,
        input_snapshot_id=value.input_snapshot_id,
        direction=value.direction,
        score=value.score,
        confidence=value.confidence,
        evidence_refs=value.evidence_refs,
        cost=value.cost,
        completed_at=value.completed_at,
        critique=value.critique,
    )


def _readmit_plan(value: object) -> DagPlan:
    if type(value) is not DagPlan:
        raise SpecialistDagError("plan must be an exact DagPlan")
    return DagPlan(
        input_snapshot_id=value.input_snapshot_id,
        scheduled_roles=value.scheduled_roles,
        skipped_roles=value.skipped_roles,
        reserved_cost=value.reserved_cost,
        available_inputs=value.available_inputs,
        total_budget=value.total_budget,
    )


@dataclass(frozen=True)
class AggregatedProposal:
    direction: Literal["LONG", "SHORT", "FLAT"]
    score: Decimal
    accepted_roles: tuple[str, ...]
    rejected_roles: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]
    total_cost: Decimal
    live_authority_granted: bool = False

    def __post_init__(self) -> None:
        if type(self.live_authority_granted) is not bool or self.live_authority_granted:
            raise SpecialistDagError(
                "specialist aggregation cannot grant live financial authority"
            )
        if type(self.direction) is not str or self.direction not in {
            "LONG",
            "SHORT",
            "FLAT",
        }:
            raise SpecialistDagError(
                "aggregate direction must be exact LONG, SHORT or FLAT"
            )
        score = _decimal(self.score, "aggregate score")
        try:
            if exact_abs(score) > 1:
                raise SpecialistDagError(
                    "aggregate score magnitude cannot exceed one"
                )
        except ExactDecimalError as error:
            raise SpecialistDagError(
                "aggregate score exceeds the exact numeric resource envelope"
            ) from error
        expected_direction = "LONG" if score > 0 else "SHORT" if score < 0 else "FLAT"
        if self.direction != expected_direction:
            raise SpecialistDagError("aggregate direction must match score sign")
        if type(self.accepted_roles) is not tuple:
            raise SpecialistDagError("accepted_roles must be an exact tuple")
        if type(self.rejected_roles) is not tuple:
            raise SpecialistDagError("rejected_roles must be an exact tuple")
        if type(self.evidence_refs) is not tuple:
            raise SpecialistDagError("evidence_refs must be an exact tuple")
        accepted = tuple(_text(item, "accepted role") for item in self.accepted_roles)
        if len(accepted) != len(set(accepted)):
            raise SpecialistDagError("accepted_roles must not contain duplicates")
        rejected: list[tuple[str, str]] = []
        for item in self.rejected_roles:
            if type(item) is not tuple or len(item) != 2:
                raise SpecialistDagError(
                    "rejected_roles entries must be exact role/reason tuples"
                )
            rejected.append(
                (_text(item[0], "rejected role"), _text(item[1], "rejection reason"))
            )
        refs = tuple(_text(item, "evidence reference") for item in self.evidence_refs)
        if len(refs) != len(set(refs)):
            raise SpecialistDagError(
                "aggregate evidence references must not contain duplicates"
            )
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "accepted_roles", accepted)
        object.__setattr__(self, "rejected_roles", tuple(rejected))
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(
            self,
            "total_cost",
            _decimal(self.total_cost, "aggregate total_cost", non_negative=True),
        )


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
    raw_items = tuple(specs)
    items = tuple(_readmit_spec(item) for item in raw_items)
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
            next_reserved = _exact_add(
                reserved,
                spec.max_cost,
                "reserved specialist cost",
            )
            if next_reserved > budget:
                skipped.append((role_id, "budget_exceeded"))
                continue
            scheduled.append(role_id)
            reserved = next_reserved
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
    raw_spec_items = tuple(specs)
    spec_items = tuple(_readmit_spec(spec) for spec in raw_spec_items)
    by_id = {spec.role_id: spec for spec in spec_items}
    if not by_id:
        raise SpecialistDagError("at least one specialist specification is required")
    if len(by_id) != len(spec_items):
        raise SpecialistDagError("specialist role ids must be unique")
    plan = _readmit_plan(plan)

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
    blockers: set[str] = set()
    for item in blocking_critique_terms:
        blockers.add(_text(item, "blocking critique term").lower())

    for raw_run in runs:
        run = _readmit_run(raw_run)
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

    # Planning dependencies are semantic data dependencies, not only scheduling
    # hints. A downstream result cannot remain accepted when any dependency's
    # result was missing, late, over-budget, critique-blocked, or otherwise
    # rejected. scheduled_roles is canonical topological order from the planner,
    # so cascading removal is deterministic.
    accepted_by_role = {run.role_id: (spec, run) for spec, run in accepted}
    for role_id in scheduled:
        pair = accepted_by_role.get(role_id)
        if pair is None:
            continue
        spec, _run = pair
        if any(dependency not in accepted_by_role for dependency in spec.dependencies):
            del accepted_by_role[role_id]
            rejected.append((role_id, "dependency_result_unavailable"))

    accepted = [
        accepted_by_role[role_id]
        for role_id in scheduled
        if role_id in accepted_by_role
    ]

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

    # Use exact rational arithmetic for weighting/averaging. Decimal operators
    # obey the process-global mutable Decimal context and therefore cannot be
    # allowed to decide a durable/replayable specialist proposal.
    group_scores: list[Fraction] = []
    for group_runs in grouped.values():
        weight_sum = Fraction(0, 1)
        weighted_score = Fraction(0, 1)
        for run in group_runs:
            confidence = _fraction(run.confidence, "specialist confidence")
            score = _fraction(run.score, "specialist score")
            weight_sum = _bounded_fraction(
                weight_sum + confidence,
                "specialist group confidence",
            )
            weighted_score = _bounded_fraction(
                weighted_score
                + _bounded_fraction(
                    score * confidence,
                    "specialist weighted score",
                ),
                "specialist group weighted score",
            )
        if weight_sum == 0:
            group_scores.append(Fraction(0, 1))
            continue
        group_scores.append(
            _bounded_fraction(
                weighted_score / weight_sum,
                "specialist group score",
            )
        )

    aggregate_fraction = Fraction(0, 1)
    for group_score in group_scores:
        aggregate_fraction = _bounded_fraction(
            aggregate_fraction + group_score,
            "aggregate specialist score",
        )
    aggregate_fraction = _bounded_fraction(
        aggregate_fraction / len(group_scores),
        "aggregate specialist score",
    )
    aggregate = _score_decimal(aggregate_fraction)
    if aggregate > 0:
        direction: Literal["LONG", "SHORT", "FLAT"] = "LONG"
    elif aggregate < 0:
        direction = "SHORT"
    else:
        direction = "FLAT"

    refs = tuple(sorted({ref for _, run in accepted for ref in run.evidence_refs}))
    total_cost = Decimal("0")
    for _, run in accepted:
        total_cost = _exact_add(total_cost, run.cost, "specialist total cost")
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

    full = _decimal(full_utility, "full_utility")
    ablated = _decimal(ablated_utility, "ablated_utility")
    cost = _decimal(incremental_cost, "incremental_cost", non_negative=True)
    try:
        return exact_subtract(exact_subtract(full, ablated), cost)
    except ExactDecimalError as error:
        raise SpecialistDagError(
            "marginal value exceeds the exact numeric resource envelope"
        ) from error
