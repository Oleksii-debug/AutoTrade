"""Deterministic cost/risk-aware portfolio allocation foundation.

This module produces proposed targets only. It never sends orders and never
treats simulated or expected returns as evidence of profitability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping, Sequence

from .allocation_valuation import (
    AllocationValuationError,
    normalize_allocation_valuation,
)


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value, *, name: str, allow_zero: bool = False) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class StressScenarioEvidence:
    """Immutable decision-time stress evidence with explicit validity."""

    name: str
    shocks: Mapping[str, Decimal]
    observed_at: str
    valid_until: str
    source_ref: str

    def __post_init__(self) -> None:
        name = _text(self.name, name="stress evidence name")
        source = _text(self.source_ref, name="stress evidence source_ref")
        observed = _instant(self.observed_at, name="stress evidence observed_at")
        valid_until = _instant(self.valid_until, name="stress evidence valid_until")
        if valid_until < observed:
            raise ValueError("stress evidence valid_until must not precede observed_at")
        if not isinstance(self.shocks, Mapping) or not self.shocks:
            raise ValueError("stress evidence shocks must be a non-empty mapping")
        normalized: dict[str, Decimal] = {}
        for symbol, shock in self.shocks.items():
            key = _text(symbol, name="stress evidence symbol")
            if key in normalized:
                raise ValueError("stress evidence symbols must be unique")
            normalized[key] = _decimal(shock, name=f"stress evidence shock {key}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "source_ref", source)
        object.__setattr__(
            self,
            "observed_at",
            observed.isoformat().replace("+00:00", "Z"),
        )
        object.__setattr__(
            self,
            "valid_until",
            valid_until.isoformat().replace("+00:00", "Z"),
        )
        object.__setattr__(self, "shocks", MappingProxyType(normalized))

    @classmethod
    def create(
        cls,
        *,
        name: str,
        shocks: Mapping[str, object],
        observed_at: str,
        valid_until: str,
        source_ref: str,
    ) -> "StressScenarioEvidence":
        return cls(
            name=name,
            shocks=shocks,
            observed_at=observed_at,
            valid_until=valid_until,
            source_ref=source_ref,
        )

    def valid_at(self, decision_time: str) -> bool:
        point = _instant(decision_time, name="decision_time")
        observed = _instant(self.observed_at, name="stress evidence observed_at")
        valid_until = _instant(self.valid_until, name="stress evidence valid_until")
        return observed <= point <= valid_until


@dataclass(frozen=True)
class AllocationCandidate:
    symbol: str
    desired_notional: Decimal
    price: Decimal
    lot_size: Decimal
    cost_rate: Decimal = Decimal("0")
    capital_requirement_rate: Decimal = Decimal("1")
    min_notional: Decimal = Decimal("0")
    fee_floor: Decimal = Decimal("0")
    max_executable_notional: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        object.__setattr__(
            self,
            "desired_notional",
            _decimal(self.desired_notional, name="desired_notional"),
        )
        object.__setattr__(self, "price", _positive(self.price, name="price"))
        object.__setattr__(
            self, "lot_size", _positive(self.lot_size, name="lot_size")
        )
        object.__setattr__(
            self,
            "cost_rate",
            _positive(self.cost_rate, name="cost_rate", allow_zero=True),
        )
        object.__setattr__(
            self,
            "capital_requirement_rate",
            _positive(
                self.capital_requirement_rate,
                name="capital_requirement_rate",
            ),
        )
        object.__setattr__(
            self,
            "min_notional",
            _positive(self.min_notional, name="min_notional", allow_zero=True),
        )
        object.__setattr__(
            self,
            "fee_floor",
            _positive(self.fee_floor, name="fee_floor", allow_zero=True),
        )
        if self.max_executable_notional is not None:
            object.__setattr__(
                self,
                "max_executable_notional",
                _positive(
                    self.max_executable_notional,
                    name="max_executable_notional",
                    allow_zero=True,
                ),
            )

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        desired_notional,
        price,
        lot_size,
        cost_rate=0,
        capital_requirement_rate=1,
        min_notional=0,
        fee_floor=0,
        max_executable_notional=None,
    ) -> "AllocationCandidate":
        return cls(
            symbol=_text(symbol, name="symbol"),
            desired_notional=_decimal(desired_notional, name="desired_notional"),
            price=_positive(price, name="price"),
            lot_size=_positive(lot_size, name="lot_size"),
            cost_rate=_positive(cost_rate, name="cost_rate", allow_zero=True),
            capital_requirement_rate=_positive(
                capital_requirement_rate,
                name="capital_requirement_rate",
            ),
            min_notional=_positive(min_notional, name="min_notional", allow_zero=True),
            fee_floor=_positive(fee_floor, name="fee_floor", allow_zero=True),
            max_executable_notional=(
                None
                if max_executable_notional is None
                else _positive(
                    max_executable_notional,
                    name="max_executable_notional",
                    allow_zero=True,
                )
            ),
        )


@dataclass(frozen=True)
class ObjectiveCandidate:
    """Allocation candidate plus explicit decision-time objective evidence.

    expected_return_rate is directional: a positive value means the proposed
    long/short direction has positive expected gross return per unit notional.
    risk_penalty_rate is a decision preference, not a cash expense.
    """

    candidate: AllocationCandidate
    expected_return_rate: Decimal
    risk_penalty_rate: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, AllocationCandidate):
            raise TypeError("candidate must be an AllocationCandidate")
        object.__setattr__(
            self,
            "expected_return_rate",
            _decimal(self.expected_return_rate, name="expected_return_rate"),
        )
        object.__setattr__(
            self,
            "risk_penalty_rate",
            _positive(
                self.risk_penalty_rate,
                name="risk_penalty_rate",
                allow_zero=True,
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        desired_notional,
        price,
        lot_size,
        expected_return_rate,
        risk_penalty_rate=0,
        cost_rate=0,
        capital_requirement_rate=1,
        min_notional=0,
        fee_floor=0,
        max_executable_notional=None,
    ) -> "ObjectiveCandidate":
        return cls(
            candidate=AllocationCandidate.create(
                symbol=symbol,
                desired_notional=desired_notional,
                price=price,
                lot_size=lot_size,
                cost_rate=cost_rate,
                capital_requirement_rate=capital_requirement_rate,
                min_notional=min_notional,
                fee_floor=fee_floor,
                max_executable_notional=max_executable_notional,
            ),
            expected_return_rate=_decimal(
                expected_return_rate,
                name="expected_return_rate",
            ),
            risk_penalty_rate=_positive(
                risk_penalty_rate,
                name="risk_penalty_rate",
                allow_zero=True,
            ),
        )

    @property
    def objective_rate(self) -> Decimal:
        return self.expected_return_rate - self.risk_penalty_rate


@dataclass(frozen=True)
class AllocationPolicy:
    cash_available: Decimal
    max_gross_notional: Decimal
    max_net_notional: Decimal
    max_symbol_notional: Decimal
    max_total_cost: Decimal
    max_stress_loss: Decimal
    minimum_cash_reserve: Decimal = Decimal("0")
    max_iterations: int = 64
    min_scale_tolerance: Decimal = Decimal("0.000001")
    require_adverse_stress_evidence: bool = True
    require_fresh_stress_evidence: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "cash_available",
            "max_gross_notional",
            "max_net_notional",
            "max_symbol_notional",
            "max_total_cost",
            "max_stress_loss",
            "minimum_cash_reserve",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive(
                    getattr(self, field_name),
                    name=field_name,
                    allow_zero=True,
                ),
            )
        if (
            not isinstance(self.max_iterations, int)
            or isinstance(self.max_iterations, bool)
            or self.max_iterations < 1
        ):
            raise ValueError("max_iterations must be a positive integer")
        if not isinstance(self.require_adverse_stress_evidence, bool):
            raise TypeError("require_adverse_stress_evidence must be a boolean")
        if not isinstance(self.require_fresh_stress_evidence, bool):
            raise TypeError("require_fresh_stress_evidence must be a boolean")
        object.__setattr__(
            self,
            "min_scale_tolerance",
            _positive(
                self.min_scale_tolerance,
                name="min_scale_tolerance",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        cash_available,
        max_gross_notional,
        max_net_notional,
        max_symbol_notional,
        max_total_cost,
        max_stress_loss,
        minimum_cash_reserve=0,
        max_iterations: int = 64,
        min_scale_tolerance="0.000001",
        require_adverse_stress_evidence: bool = True,
        require_fresh_stress_evidence: bool = True,
    ) -> "AllocationPolicy":
        if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")
        if not isinstance(require_adverse_stress_evidence, bool):
            raise TypeError("require_adverse_stress_evidence must be a boolean")
        if not isinstance(require_fresh_stress_evidence, bool):
            raise TypeError("require_fresh_stress_evidence must be a boolean")
        return cls(
            cash_available=_positive(cash_available, name="cash_available", allow_zero=True),
            max_gross_notional=_positive(max_gross_notional, name="max_gross_notional", allow_zero=True),
            max_net_notional=_positive(max_net_notional, name="max_net_notional", allow_zero=True),
            max_symbol_notional=_positive(max_symbol_notional, name="max_symbol_notional", allow_zero=True),
            max_total_cost=_positive(max_total_cost, name="max_total_cost", allow_zero=True),
            max_stress_loss=_positive(max_stress_loss, name="max_stress_loss", allow_zero=True),
            minimum_cash_reserve=_positive(
                minimum_cash_reserve,
                name="minimum_cash_reserve",
                allow_zero=True,
            ),
            max_iterations=max_iterations,
            min_scale_tolerance=_positive(min_scale_tolerance, name="min_scale_tolerance"),
            require_adverse_stress_evidence=require_adverse_stress_evidence,
            require_fresh_stress_evidence=require_fresh_stress_evidence,
        )


@dataclass(frozen=True)
class AllocationTarget:
    symbol: str
    quantity: Decimal
    notional: Decimal
    estimated_cost: Decimal


@dataclass(frozen=True)
class AllocationResult:
    status: str
    scale: Decimal
    targets: tuple[AllocationTarget, ...]
    gross_notional: Decimal
    net_notional: Decimal
    estimated_cost: Decimal
    worst_stress_loss: Decimal
    cash_required: Decimal
    reason: str


@dataclass(frozen=True)
class ObjectiveAllocationResult:
    allocation: AllocationResult
    selected_symbols: tuple[str, ...]
    expected_net_utility: Decimal
    objective_version: str
    reason: str


def _round_quantity(notional: Decimal, price: Decimal, lot_size: Decimal) -> Decimal:
    if notional == 0:
        return Decimal("0")
    absolute_quantity = abs(notional) / price
    lots = (absolute_quantity / lot_size).to_integral_value(rounding=ROUND_DOWN)
    quantity = lots * lot_size
    return quantity if notional > 0 else -quantity


def _normalize_stress_scenarios(
    candidates: Sequence[AllocationCandidate],
    stress_scenarios: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, dict[str, Decimal]]:
    symbols = [candidate.symbol for candidate in candidates]
    if len(symbols) != len(set(symbols)):
        raise ValueError("candidate symbols must be unique")

    normalized = {
        _text(name, name="scenario name"): {
            _text(symbol, name="stress symbol"): _decimal(
                shock,
                name=f"stress shock {symbol}",
            )
            for symbol, shock in scenario.items()
        }
        for name, scenario in (stress_scenarios or {}).items()
    }
    symbol_set = set(symbols)
    unknown = sorted(
        {
            symbol
            for scenario in normalized.values()
            for symbol in scenario
            if symbol not in symbol_set
        }
    )
    if unknown:
        raise ValueError(
            f"stress scenarios reference unknown symbols: {', '.join(unknown)}"
        )

    for scenario_name, scenario in normalized.items():
        missing = sorted(symbol_set - set(scenario))
        if missing:
            raise ValueError(
                f"stress scenario {scenario_name} is missing explicit shocks for: "
                f"{', '.join(missing)}"
            )
    return normalized


def _normalize_stress_evidence(
    candidates: Sequence[AllocationCandidate],
    evidence: Sequence[StressScenarioEvidence],
    *,
    decision_time: str | None,
) -> tuple[dict[str, dict[str, Decimal]], str | None]:
    if decision_time is None:
        return {}, "fresh stress evidence requires an explicit decision_time"
    point = _instant(decision_time, name="decision_time")
    materialized = tuple(evidence)
    if not materialized:
        return {}, "fresh stress evidence is required before increasing exposure"
    if any(not isinstance(item, StressScenarioEvidence) for item in materialized):
        raise TypeError("stress_evidence must contain StressScenarioEvidence values")
    names = [item.name for item in materialized]
    if len(names) != len(set(names)):
        raise ValueError("stress evidence names must be unique")
    for item in materialized:
        observed = _instant(item.observed_at, name="stress evidence observed_at")
        valid_until = _instant(item.valid_until, name="stress evidence valid_until")
        if observed > point:
            return {}, f"stress evidence {item.name} was not observable at decision_time"
        if point > valid_until:
            return {}, f"stress evidence {item.name} expired before decision_time"
    normalized = _normalize_stress_scenarios(
        candidates,
        {item.name: dict(item.shocks) for item in materialized},
    )
    return normalized, None


def _evaluate(
    candidates: Sequence[AllocationCandidate],
    policy: AllocationPolicy,
    stress_scenarios: Mapping[str, Mapping[str, Decimal]],
    scale: Decimal,
) -> AllocationResult:
    targets: list[AllocationTarget] = []
    notionals: dict[str, Decimal] = {}
    total_cost = Decimal("0")
    capital_required = Decimal("0")

    for candidate in candidates:
        scaled = candidate.desired_notional * scale
        quantity = _round_quantity(scaled, candidate.price, candidate.lot_size)
        notional = quantity * candidate.price
        if (
            candidate.max_executable_notional is not None
            and abs(notional) > candidate.max_executable_notional
        ):
            capped = (
                candidate.max_executable_notional
                if notional > 0
                else -candidate.max_executable_notional
            )
            quantity = _round_quantity(capped, candidate.price, candidate.lot_size)
            notional = quantity * candidate.price
        if quantity != 0 and abs(notional) < candidate.min_notional:
            quantity = Decimal("0")
            notional = Decimal("0")
        proportional_cost = abs(notional) * candidate.cost_rate
        cost = (
            max(proportional_cost, candidate.fee_floor)
            if notional != 0
            else Decimal("0")
        )
        notionals[candidate.symbol] = notional
        total_cost += cost
        capital_required += abs(notional) * candidate.capital_requirement_rate
        targets.append(
            AllocationTarget(
                symbol=candidate.symbol,
                quantity=quantity,
                notional=notional,
                estimated_cost=cost,
            )
        )

    gross = sum((abs(value) for value in notionals.values()), Decimal("0"))
    net = abs(sum(notionals.values(), Decimal("0")))
    cash_required = capital_required + total_cost

    worst_stress_loss = Decimal("0")
    for scenario in stress_scenarios.values():
        pnl = sum(
            (
                notional * scenario[symbol]
                for symbol, notional in notionals.items()
            ),
            Decimal("0"),
        )
        worst_stress_loss = max(worst_stress_loss, -pnl)

    symbol_ok = all(
        abs(value) <= policy.max_symbol_notional
        for value in notionals.values()
    )
    feasible = (
        symbol_ok
        and gross <= policy.max_gross_notional
        and net <= policy.max_net_notional
        and total_cost <= policy.max_total_cost
        and worst_stress_loss <= policy.max_stress_loss
        and cash_required + policy.minimum_cash_reserve <= policy.cash_available
    )

    return AllocationResult(
        status="ALLOCATED" if feasible else "INFEASIBLE",
        scale=scale,
        targets=tuple(targets),
        gross_notional=gross,
        net_notional=net,
        estimated_cost=total_cost,
        worst_stress_loss=worst_stress_loss,
        cash_required=cash_required,
        reason=(
            "all hard constraints satisfied"
            if feasible
            else "one or more hard constraints failed"
        ),
    )


def _cash_fallback(
    candidates: Sequence[AllocationCandidate],
    *,
    reason: str,
) -> AllocationResult:
    return AllocationResult(
        status="NO_INCREASE_FALLBACK",
        scale=Decimal("0"),
        targets=tuple(
            AllocationTarget(
                symbol=candidate.symbol,
                quantity=Decimal("0"),
                notional=Decimal("0"),
                estimated_cost=Decimal("0"),
            )
            for candidate in candidates
        ),
        gross_notional=Decimal("0"),
        net_notional=Decimal("0"),
        estimated_cost=Decimal("0"),
        worst_stress_loss=Decimal("0"),
        cash_required=Decimal("0"),
        reason=reason,
    )


def allocate_targets(
    candidates: Sequence[AllocationCandidate],
    policy: AllocationPolicy,
    *,
    stress_scenarios: Mapping[str, Mapping[str, object]] | None = None,
    stress_evidence: Sequence[StressScenarioEvidence] = (),
    decision_time: str | None = None,
) -> AllocationResult:
    """Return the largest uniformly scaled feasible target set.

    When no positive lot can be proven feasible inside the bounded search,
    returns an explicit all-cash no-increase fallback.
    """

    if not candidates:
        return _cash_fallback(candidates, reason="no allocation candidates")

    normalized_stress = _normalize_stress_scenarios(candidates, stress_scenarios)
    if policy.require_adverse_stress_evidence and policy.require_fresh_stress_evidence:
        normalized_stress, evidence_problem = _normalize_stress_evidence(
            candidates,
            stress_evidence,
            decision_time=decision_time,
        )
        if evidence_problem is not None:
            return _cash_fallback(candidates, reason=evidence_problem)

    if policy.minimum_cash_reserve > policy.cash_available:
        return _cash_fallback(
            candidates,
            reason="minimum cash reserve exceeds available cash",
        )

    if policy.require_adverse_stress_evidence:
        requested_symbols = {
            candidate.symbol: candidate.desired_notional
            for candidate in candidates
            if candidate.desired_notional != 0
        }
        if requested_symbols and not normalized_stress:
            return _cash_fallback(
                candidates,
                reason=(
                    "adverse stress evidence is required before increasing exposure; "
                    "no stress scenarios were supplied"
                ),
            )
        uncovered = []
        for symbol, desired_notional in requested_symbols.items():
            has_adverse_shock = any(
                (
                    scenario[symbol] < 0
                    if desired_notional > 0
                    else scenario[symbol] > 0
                )
                for scenario in normalized_stress.values()
            )
            if not has_adverse_shock:
                uncovered.append(symbol)
        if uncovered:
            return _cash_fallback(
                candidates,
                reason=(
                    "adverse stress evidence is missing for requested exposure: "
                    + ", ".join(sorted(uncovered))
                ),
            )

    requested = _evaluate(candidates, policy, normalized_stress, Decimal("1"))
    if requested.status == "ALLOCATED":
        if requested.gross_notional > 0:
            return requested
        return _cash_fallback(
            candidates,
            reason=(
                "requested allocation contains no executable positive lot after "
                "lot-size and minimum-notional constraints; remain in cash"
            ),
        )

    low = Decimal("0")
    high = Decimal("1")
    best = _evaluate(candidates, policy, normalized_stress, low)

    for _ in range(policy.max_iterations):
        if high - low <= policy.min_scale_tolerance:
            break
        mid = (low + high) / Decimal("2")
        result = _evaluate(candidates, policy, normalized_stress, mid)
        if result.status == "ALLOCATED":
            best = result
            low = mid
        else:
            high = mid

    if best.gross_notional == 0:
        return _cash_fallback(
            candidates,
            reason=(
                "bounded search found no positive-lot feasible allocation; "
                "remain in cash"
            ),
        )

    return AllocationResult(
        status="ALLOCATED",
        scale=best.scale,
        targets=best.targets,
        gross_notional=best.gross_notional,
        net_notional=best.net_notional,
        estimated_cost=best.estimated_cost,
        worst_stress_loss=best.worst_stress_loss,
        cash_required=best.cash_required,
        reason=(
            "requested allocation was infeasible; uniformly reduced to the "
            "largest verified feasible target found"
        ),
    )


def _expected_net_utility(
    result: AllocationResult,
    objective_by_symbol: Mapping[str, ObjectiveCandidate],
) -> Decimal:
    gross_objective = sum(
        (
            abs(target.notional)
            * objective_by_symbol[target.symbol].objective_rate
            for target in result.targets
        ),
        Decimal("0"),
    )
    return gross_objective - result.estimated_cost


def allocate_objective_targets(
    candidates: Sequence[ObjectiveCandidate],
    policy: AllocationPolicy,
    *,
    stress_scenarios: Mapping[str, Mapping[str, object]] | None = None,
    stress_evidence: Sequence[StressScenarioEvidence] = (),
    decision_time: str | None = None,
    max_candidate_sets: int = 64,
) -> ObjectiveAllocationResult:
    """Select the best deterministic feasible candidate subset by net utility.

    Candidates are ranked only to make enumeration and tie-breaking stable.
    Every non-empty subset of positive objective-rate candidates is evaluated
    through the same hard allocation constraints. Estimated execution cost is
    subtracted exactly once from the objective. The search is exhaustive only
    inside max_candidate_sets; if the complete subset space does not fit that
    budget, the allocator fails closed to a no-increase cash fallback rather
    than silently truncating instrument selection.
    """

    if not isinstance(max_candidate_sets, int) or isinstance(max_candidate_sets, bool):
        raise ValueError("max_candidate_sets must be a positive integer")
    if max_candidate_sets < 1:
        raise ValueError("max_candidate_sets must be a positive integer")

    allocation_candidates: list[AllocationCandidate] = []
    for item in candidates:
        if not isinstance(item, ObjectiveCandidate):
            raise TypeError("all candidates must be ObjectiveCandidate values")
        allocation_candidates.append(item.candidate)

    if not candidates:
        fallback = _cash_fallback((), reason="no objective candidates")
        return ObjectiveAllocationResult(
            allocation=fallback,
            selected_symbols=(),
            expected_net_utility=Decimal("0"),
            objective_version="deterministic-net-utility-v2",
            reason="no objective candidates",
        )

    normalized_stress = _normalize_stress_scenarios(
        allocation_candidates,
        stress_scenarios,
    )
    normalized_evidence: tuple[StressScenarioEvidence, ...] = ()
    if policy.require_adverse_stress_evidence and policy.require_fresh_stress_evidence:
        normalized_stress, evidence_problem = _normalize_stress_evidence(
            allocation_candidates,
            stress_evidence,
            decision_time=decision_time,
        )
        if evidence_problem is not None:
            fallback = _cash_fallback(allocation_candidates, reason=evidence_problem)
            return ObjectiveAllocationResult(
                allocation=fallback,
                selected_symbols=(),
                expected_net_utility=Decimal("0"),
                objective_version="deterministic-net-utility-v2",
                reason=evidence_problem,
            )
        normalized_evidence = tuple(stress_evidence)

    ranked = sorted(
        (
            item
            for item in candidates
            if item.objective_rate > 0
        ),
        key=lambda item: (-item.objective_rate, item.candidate.symbol),
    )

    if not ranked:
        fallback = _cash_fallback(
            allocation_candidates,
            reason="no candidate has positive expected return after risk penalty",
        )
        return ObjectiveAllocationResult(
            allocation=fallback,
            selected_symbols=(),
            expected_net_utility=Decimal("0"),
            objective_version="deterministic-net-utility-v2",
            reason="no candidate has positive expected return after risk penalty",
        )

    candidate_set_count = (1 << len(ranked)) - 1
    if candidate_set_count > max_candidate_sets:
        fallback = _cash_fallback(
            allocation_candidates,
            reason=(
                "objective search budget exceeded before complete subset "
                "evaluation"
            ),
        )
        return ObjectiveAllocationResult(
            allocation=fallback,
            selected_symbols=(),
            expected_net_utility=Decimal("0"),
            objective_version="deterministic-net-utility-v2",
            reason=(
                "objective search budget exceeded before complete subset "
                "evaluation"
            ),
        )

    objective_by_symbol = {
        item.candidate.symbol: item
        for item in ranked
    }
    best_result: AllocationResult | None = None
    best_symbols: tuple[str, ...] = ()
    best_utility = Decimal("0")

    for subset_mask in range(1, candidate_set_count + 1):
        subset = [
            item
            for index, item in enumerate(ranked)
            if subset_mask & (1 << index)
        ]
        subset_symbols = tuple(item.candidate.symbol for item in subset)
        projected_stress = {
            scenario_name: {
                symbol: scenario[symbol]
                for symbol in subset_symbols
            }
            for scenario_name, scenario in normalized_stress.items()
        }
        projected_evidence = tuple(
            StressScenarioEvidence.create(
                name=item.name,
                shocks={symbol: item.shocks[symbol] for symbol in subset_symbols},
                observed_at=item.observed_at,
                valid_until=item.valid_until,
                source_ref=item.source_ref,
            )
            for item in normalized_evidence
        )
        result = allocate_targets(
            [item.candidate for item in subset],
            policy,
            stress_scenarios=projected_stress,
            stress_evidence=projected_evidence,
            decision_time=decision_time,
        )
        if result.status != "ALLOCATED":
            continue
        utility = _expected_net_utility(result, objective_by_symbol)
        if utility <= 0:
            continue
        active_symbols = tuple(
            sorted(
                target.symbol
                for target in result.targets
                if target.notional != 0
            )
        )
        if not active_symbols:
            continue
        better = best_result is None or utility > best_utility
        if (
            not better
            and best_result is not None
            and utility == best_utility
        ):
            candidate_key = (
                result.estimated_cost,
                result.gross_notional,
                active_symbols,
            )
            current_key = (
                best_result.estimated_cost,
                best_result.gross_notional,
                best_symbols,
            )
            better = candidate_key < current_key
        if better:
            best_result = result
            best_symbols = active_symbols
            best_utility = utility

    if best_result is None:
        fallback = _cash_fallback(
            allocation_candidates,
            reason=(
                "no positive-utility feasible allocation survived hard "
                "constraints and estimated costs"
            ),
        )
        return ObjectiveAllocationResult(
            allocation=fallback,
            selected_symbols=(),
            expected_net_utility=Decimal("0"),
            objective_version="deterministic-net-utility-v2",
            reason=(
                "no positive-utility feasible allocation survived hard "
                "constraints and estimated costs"
            ),
        )

    return ObjectiveAllocationResult(
        allocation=best_result,
        selected_symbols=best_symbols,
        expected_net_utility=best_utility,
        objective_version="deterministic-net-utility-v2",
        reason=(
            "selected the highest positive expected-net-utility deterministic "
            "candidate subset that passed all hard allocation constraints"
        ),
    )

# Evidence-bound allocation convergence for WP-20/WP-32/WP-47.

_ALLOWED_EVIDENCE_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_ALLOWED_ALLOCATION_EVIDENCE_KINDS = frozenset(
    {
        "OBJECTIVE",
        "MARKET_CONSTRAINT",
        "CAPITAL_STATE",
        "STRESS_SCENARIO",
        "VALUATION",
    }
)


def _canonical_evidence_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        raise TypeError("allocation evidence cannot contain binary floating-point values")
    if isinstance(value, Mapping):
        normalized = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, name="allocation evidence payload key")
            if key in normalized:
                raise ValueError("allocation evidence payload keys must be unique")
            normalized[key] = _canonical_evidence_value(raw_value)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_evidence_value(item) for item in value]
    raise TypeError(f"unsupported allocation evidence value type: {type(value).__name__}")


def _freeze_evidence_value(value):
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_evidence_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_evidence_value(item) for item in value)
    return value


def _canonical_evidence_json(value) -> str:
    return json.dumps(
        _canonical_evidence_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _allocation_evidence_digest(
    *,
    evidence_id: str,
    kind: str,
    environment: str,
    schema_version: str,
    observed_at: str,
    valid_until: str,
    payload: Mapping[str, object],
) -> str:
    body = {
        "evidence_id": evidence_id,
        "kind": kind,
        "environment": environment,
        "schema_version": schema_version,
        "observed_at": observed_at,
        "valid_until": valid_until,
        "payload": payload,
    }
    return sha256(_canonical_evidence_json(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImmutableAllocationEvidence:
    """Content-bound evidence consumed by the allocation proposal boundary.

    The digest is over both identity metadata and the canonical payload.  A
    downstream authority must still resolve the evidence_id from its trusted
    store and compare the digest; this type does not mint financial authority.
    """

    evidence_id: str
    kind: str
    environment: str
    schema_version: str
    observed_at: str
    valid_until: str
    payload: Mapping[str, object]
    digest: str

    def __post_init__(self) -> None:
        evidence_id = _text(self.evidence_id, name="allocation evidence_id")
        kind = _text(self.kind, name="allocation evidence kind").upper()
        environment = _text(
            self.environment,
            name="allocation evidence environment",
        ).upper()
        schema_version = _text(
            self.schema_version,
            name="allocation evidence schema_version",
        )
        if kind not in _ALLOWED_ALLOCATION_EVIDENCE_KINDS:
            raise ValueError(f"unsupported allocation evidence kind: {kind}")
        if environment not in _ALLOWED_EVIDENCE_ENVIRONMENTS:
            raise ValueError(f"unsupported allocation evidence environment: {environment}")
        observed = _instant(self.observed_at, name="allocation evidence observed_at")
        valid_until = _instant(self.valid_until, name="allocation evidence valid_until")
        if valid_until < observed:
            raise ValueError("allocation evidence valid_until must not precede observed_at")
        if not isinstance(self.payload, Mapping) or not self.payload:
            raise ValueError("allocation evidence payload must be a non-empty mapping")
        normalized_payload = _canonical_evidence_value(self.payload)
        if not isinstance(normalized_payload, dict):
            raise TypeError("allocation evidence payload must normalize to an object")
        digest = _text(self.digest, name="allocation evidence digest")
        if (
            len(digest) != 64
            or digest.lower() != digest
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("allocation evidence digest must be lowercase sha256 hex")
        normalized_observed = observed.isoformat().replace("+00:00", "Z")
        normalized_valid_until = valid_until.isoformat().replace("+00:00", "Z")
        expected = _allocation_evidence_digest(
            evidence_id=evidence_id,
            kind=kind,
            environment=environment,
            schema_version=schema_version,
            observed_at=normalized_observed,
            valid_until=normalized_valid_until,
            payload=normalized_payload,
        )
        if digest != expected:
            raise ValueError("allocation evidence digest does not match canonical content")
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "observed_at", normalized_observed)
        object.__setattr__(self, "valid_until", normalized_valid_until)
        object.__setattr__(self, "payload", _freeze_evidence_value(normalized_payload))
        object.__setattr__(self, "digest", digest)

    @classmethod
    def create(
        cls,
        *,
        evidence_id: str,
        kind: str,
        environment: str,
        schema_version: str,
        observed_at: str,
        valid_until: str,
        payload: Mapping[str, object],
    ) -> "ImmutableAllocationEvidence":
        normalized_id = _text(evidence_id, name="allocation evidence_id")
        normalized_kind = _text(kind, name="allocation evidence kind").upper()
        normalized_environment = _text(
            environment,
            name="allocation evidence environment",
        ).upper()
        normalized_schema = _text(
            schema_version,
            name="allocation evidence schema_version",
        )
        observed = _instant(observed_at, name="allocation evidence observed_at")
        valid = _instant(valid_until, name="allocation evidence valid_until")
        if valid < observed:
            raise ValueError("allocation evidence valid_until must not precede observed_at")
        normalized_payload = _canonical_evidence_value(payload)
        if not isinstance(normalized_payload, dict) or not normalized_payload:
            raise ValueError("allocation evidence payload must be a non-empty mapping")
        normalized_observed = observed.isoformat().replace("+00:00", "Z")
        normalized_valid = valid.isoformat().replace("+00:00", "Z")
        digest = _allocation_evidence_digest(
            evidence_id=normalized_id,
            kind=normalized_kind,
            environment=normalized_environment,
            schema_version=normalized_schema,
            observed_at=normalized_observed,
            valid_until=normalized_valid,
            payload=normalized_payload,
        )
        return cls(
            evidence_id=normalized_id,
            kind=normalized_kind,
            environment=normalized_environment,
            schema_version=normalized_schema,
            observed_at=normalized_observed,
            valid_until=normalized_valid,
            payload=normalized_payload,
            digest=digest,
        )

    def valid_at(self, instant: str) -> bool:
        point = _instant(instant, name="allocation evidence decision time")
        return (
            _instant(self.observed_at, name="allocation evidence observed_at")
            <= point
            <= _instant(self.valid_until, name="allocation evidence valid_until")
        )


@dataclass(frozen=True)
class EvidenceBoundObjectiveAllocationResult:
    objective: ObjectiveAllocationResult
    decision_digest: str
    evidence_refs: tuple[tuple[str, str], ...]
    environment: str
    policy_version: str
    decision_time: str
    provider_id: str
    account_id: str
    instrument_versions: tuple[tuple[str, str], ...]
    capability_snapshot_ids: tuple[tuple[str, str], ...]
    account_snapshot_id: str
    reconciliation_run_id: str
    account_state_version: int
    reservation_state_version: int
    reservation_state_digest: str
    base_currency: str


def _payload_text(evidence: ImmutableAllocationEvidence, key: str) -> str:
    value = evidence.payload.get(key)
    return _text(value, name=f"{evidence.kind} payload {key}")


def _payload_decimal(evidence: ImmutableAllocationEvidence, key: str) -> Decimal:
    if key not in evidence.payload:
        raise ValueError(f"{evidence.kind} payload is missing {key}")
    return _decimal(evidence.payload[key], name=f"{evidence.kind} payload {key}")


def _payload_nonnegative_int(
    evidence: ImmutableAllocationEvidence,
    key: str,
) -> int:
    value = evidence.payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{evidence.kind} payload {key} must be a non-negative integer")
    return value


def _resolve_allocation_evidence(
    evidence: ImmutableAllocationEvidence,
    resolved_evidence: Mapping[str, ImmutableAllocationEvidence],
    *,
    expected_kind: str,
    expected_environment: str,
    at: str,
) -> ImmutableAllocationEvidence:
    if not isinstance(evidence, ImmutableAllocationEvidence):
        raise TypeError("allocation evidence values must be ImmutableAllocationEvidence")
    if evidence.kind != expected_kind:
        raise ValueError(
            f"allocation evidence {evidence.evidence_id} has kind {evidence.kind}, "
            f"expected {expected_kind}"
        )
    if evidence.environment != expected_environment:
        raise ValueError(
            f"allocation evidence {evidence.evidence_id} environment mismatch"
        )
    if not evidence.valid_at(at):
        raise ValueError(
            f"allocation evidence {evidence.evidence_id} is stale or not yet observable"
        )
    resolved = resolved_evidence.get(evidence.evidence_id)
    if not isinstance(resolved, ImmutableAllocationEvidence):
        raise ValueError(
            f"allocation evidence {evidence.evidence_id} cannot be resolved authoritatively"
        )
    if resolved.digest != evidence.digest:
        raise ValueError(
            f"allocation evidence {evidence.evidence_id} digest does not match authoritative content"
        )
    if resolved.kind != expected_kind or resolved.environment != expected_environment:
        raise ValueError(
            f"allocation evidence {evidence.evidence_id} authoritative scope mismatch"
        )
    if not resolved.valid_at(at):
        raise ValueError(
            f"allocation evidence {evidence.evidence_id} authoritative record is stale"
        )
    return resolved


def _candidate_evidence_matches(
    item: ObjectiveCandidate,
    objective: ImmutableAllocationEvidence,
    market: ImmutableAllocationEvidence,
    *,
    decision_time: str,
) -> tuple[str, str, str, str]:
    symbol = item.candidate.symbol
    if _payload_text(objective, "symbol") != symbol:
        raise ValueError(f"objective evidence symbol mismatch for {symbol}")
    if _payload_decimal(objective, "desired_notional") != item.candidate.desired_notional:
        raise ValueError(f"objective evidence desired_notional mismatch for {symbol}")
    if _payload_decimal(objective, "expected_return_rate") != item.expected_return_rate:
        raise ValueError(f"objective evidence expected_return_rate mismatch for {symbol}")
    if _payload_decimal(objective, "risk_penalty_rate") != item.risk_penalty_rate:
        raise ValueError(f"objective evidence risk_penalty_rate mismatch for {symbol}")
    for required_identity in (
        "candidate_id",
        "proposal_id",
        "strategy_version",
        "protocol_digest",
        "input_snapshot_digest",
        "information_cutoff",
    ):
        _payload_text(objective, required_identity)
    cutoff = _instant(
        _payload_text(objective, "information_cutoff"),
        name="objective information_cutoff",
    )
    if cutoff > _instant(decision_time, name="decision_time"):
        raise ValueError(f"objective evidence information_cutoff is in the future for {symbol}")

    candidate = item.candidate
    if _payload_text(market, "symbol") != symbol:
        raise ValueError(f"market evidence symbol mismatch for {symbol}")
    for required_identity in (
        "instrument_version",
        "provider_id",
        "account_id",
        "capability_snapshot_id",
        "source_as_of",
    ):
        _payload_text(market, required_identity)
    market_as_of = _instant(
        _payload_text(market, "source_as_of"),
        name="market evidence source_as_of",
    )
    if market_as_of > _instant(decision_time, name="decision_time"):
        raise ValueError(f"market evidence source_as_of is in the future for {symbol}")
    for key, actual in (
        ("price", candidate.price),
        ("lot_size", candidate.lot_size),
        ("cost_rate", candidate.cost_rate),
        ("capital_requirement_rate", candidate.capital_requirement_rate),
        ("min_notional", candidate.min_notional),
        ("fee_floor", candidate.fee_floor),
    ):
        if _payload_decimal(market, key) != actual:
            raise ValueError(f"market evidence {key} mismatch for {symbol}")
    expected_max = market.payload.get("max_executable_notional")
    if candidate.max_executable_notional is None:
        if expected_max is not None:
            raise ValueError(
                f"market evidence max_executable_notional mismatch for {symbol}"
            )
    else:
        if expected_max is None or _decimal(
            expected_max,
            name=f"MARKET_CONSTRAINT payload max_executable_notional {symbol}",
        ) != candidate.max_executable_notional:
            raise ValueError(
                f"market evidence max_executable_notional mismatch for {symbol}"
            )
    return (
        _payload_text(market, "provider_id"),
        _payload_text(market, "account_id"),
        _payload_text(market, "instrument_version"),
        _payload_text(market, "capability_snapshot_id"),
    )


def _allocation_decision_digest(
    result: ObjectiveAllocationResult,
    *,
    evidence_refs: Sequence[tuple[str, str]],
    environment: str,
    policy_version: str,
    decision_time: str,
    provider_id: str,
    account_id: str,
    instrument_versions: Sequence[tuple[str, str]],
    capability_snapshot_ids: Sequence[tuple[str, str]],
    account_snapshot_id: str,
    reconciliation_run_id: str,
    account_state_version: int,
    reservation_state_version: int,
    reservation_state_digest: str,
    base_currency: str,
) -> str:
    payload = {
        "environment": environment,
        "policy_version": policy_version,
        "decision_time": decision_time,
        "provider_id": provider_id,
        "account_id": account_id,
        "instrument_versions": [
            {"symbol": symbol, "instrument_version": version}
            for symbol, version in sorted(instrument_versions)
        ],
        "capability_snapshot_ids": [
            {"symbol": symbol, "capability_snapshot_id": snapshot_id}
            for symbol, snapshot_id in sorted(capability_snapshot_ids)
        ],
        "account_snapshot_id": account_snapshot_id,
        "reconciliation_run_id": reconciliation_run_id,
        "account_state_version": account_state_version,
        "reservation_state_version": reservation_state_version,
        "reservation_state_digest": reservation_state_digest,
        "base_currency": base_currency,
        "objective_version": result.objective_version,
        "selected_symbols": list(result.selected_symbols),
        "expected_net_utility": str(result.expected_net_utility),
        "allocation": {
            "status": result.allocation.status,
            "scale": str(result.allocation.scale),
            "gross_notional": str(result.allocation.gross_notional),
            "net_notional": str(result.allocation.net_notional),
            "estimated_cost": str(result.allocation.estimated_cost),
            "worst_stress_loss": str(result.allocation.worst_stress_loss),
            "cash_required": str(result.allocation.cash_required),
            "targets": [
                {
                    "symbol": target.symbol,
                    "quantity": str(target.quantity),
                    "notional": str(target.notional),
                    "estimated_cost": str(target.estimated_cost),
                }
                for target in result.allocation.targets
            ],
        },
        "evidence_refs": [
            {"evidence_id": evidence_id, "digest": digest}
            for evidence_id, digest in evidence_refs
        ],
    }
    return sha256(_canonical_evidence_json(payload).encode("utf-8")).hexdigest()


def allocate_evidence_bound_objective_targets(
    candidates: Sequence[ObjectiveCandidate],
    policy: AllocationPolicy,
    *,
    objective_evidence: Mapping[str, ImmutableAllocationEvidence],
    market_evidence: Mapping[str, ImmutableAllocationEvidence],
    valuation_evidence: Mapping[str, ImmutableAllocationEvidence],
    capital_evidence: ImmutableAllocationEvidence,
    stress_source_evidence: Sequence[ImmutableAllocationEvidence],
    resolved_evidence: Mapping[str, ImmutableAllocationEvidence],
    environment: str,
    decision_time: str,
    policy_version: str,
    max_candidate_sets: int = 64,
) -> EvidenceBoundObjectiveAllocationResult:
    """Validate authoritative inputs, then reuse the existing WP-32 allocator.

    This wrapper remains proposal-only.  It binds all decision-relevant inputs
    to immutable evidence and emits a deterministic digest that a later
    financial authority can revalidate against current account/reservation
    versions before admission.
    """

    normalized_environment = _text(environment, name="allocation environment").upper()
    if normalized_environment not in _ALLOWED_EVIDENCE_ENVIRONMENTS:
        raise ValueError(f"unsupported allocation environment: {normalized_environment}")
    normalized_policy_version = _text(policy_version, name="allocation policy_version")
    normalized_decision_time = _instant(
        decision_time,
        name="allocation decision_time",
    ).isoformat().replace("+00:00", "Z")
    if not isinstance(resolved_evidence, Mapping):
        raise TypeError("resolved_evidence must be a mapping")

    materialized = tuple(candidates)
    symbols = tuple(item.candidate.symbol for item in materialized)
    if len(symbols) != len(set(symbols)):
        raise ValueError("objective candidate symbols must be unique")
    if set(objective_evidence) != set(symbols):
        raise ValueError("objective evidence must exactly cover candidate symbols")
    if set(market_evidence) != set(symbols):
        raise ValueError("market evidence must exactly cover candidate symbols")
    if set(valuation_evidence) != set(symbols):
        raise ValueError("valuation evidence must exactly cover candidate symbols")

    resolved_objective = {}
    resolved_market = {}
    provider_ids = set()
    account_ids = set()
    instrument_versions = {}
    capability_snapshot_ids = {}
    for item in materialized:
        symbol = item.candidate.symbol
        objective = _resolve_allocation_evidence(
            objective_evidence[symbol],
            resolved_evidence,
            expected_kind="OBJECTIVE",
            expected_environment=normalized_environment,
            at=normalized_decision_time,
        )
        market = _resolve_allocation_evidence(
            market_evidence[symbol],
            resolved_evidence,
            expected_kind="MARKET_CONSTRAINT",
            expected_environment=normalized_environment,
            at=normalized_decision_time,
        )
        (
            provider_id,
            account_id,
            instrument_version,
            capability_snapshot_id,
        ) = _candidate_evidence_matches(
            item,
            objective,
            market,
            decision_time=normalized_decision_time,
        )
        resolved_objective[symbol] = objective
        resolved_market[symbol] = market
        provider_ids.add(provider_id)
        account_ids.add(account_id)
        instrument_versions[symbol] = instrument_version
        capability_snapshot_ids[symbol] = capability_snapshot_id
    if len(provider_ids) != 1:
        raise ValueError("market evidence candidates must share one provider_id")
    if len(account_ids) != 1:
        raise ValueError("market evidence candidates must share one account_id")
    provider_id = next(iter(provider_ids))
    account_id = next(iter(account_ids))

    resolved_capital = _resolve_allocation_evidence(
        capital_evidence,
        resolved_evidence,
        expected_kind="CAPITAL_STATE",
        expected_environment=normalized_environment,
        at=normalized_decision_time,
    )
    if _payload_text(resolved_capital, "provider_id") != provider_id:
        raise ValueError("capital evidence provider_id does not match market evidence")
    if _payload_text(resolved_capital, "account_id") != account_id:
        raise ValueError("capital evidence account_id does not match market evidence")
    if _payload_decimal(resolved_capital, "cash_available") != policy.cash_available:
        raise ValueError("policy cash_available does not match authoritative capital evidence")
    account_snapshot_id = _payload_text(
        resolved_capital,
        "account_snapshot_id",
    )
    reconciliation_run_id = _payload_text(
        resolved_capital,
        "reconciliation_run_id",
    )
    reservation_state_digest = _payload_text(
        resolved_capital,
        "reservation_state_digest",
    )
    if (
        len(reservation_state_digest) != 64
        or reservation_state_digest.lower() != reservation_state_digest
        or any(character not in "0123456789abcdef" for character in reservation_state_digest)
    ):
        raise ValueError("capital evidence reservation_state_digest must be lowercase sha256 hex")
    account_state_version = _payload_nonnegative_int(
        resolved_capital,
        "account_state_version",
    )
    reservation_state_version = _payload_nonnegative_int(
        resolved_capital,
        "reservation_state_version",
    )
    base_currency = _payload_text(resolved_capital, "base_currency").upper()

    resolved_valuation = {}
    normalized_candidates = []
    for item in materialized:
        symbol = item.candidate.symbol
        valuation = _resolve_allocation_evidence(
            valuation_evidence[symbol],
            resolved_evidence,
            expected_kind="VALUATION",
            expected_environment=normalized_environment,
            at=normalized_decision_time,
        )
        quote_currency = _payload_text(
            resolved_market[symbol],
            "quote_currency",
        ).upper()
        valuation_fx_rate = _payload_decimal(valuation, "fx_rate")
        # Monetary units are authority, including the identity-FX case.  Requiring
        # declarations only for cross-currency candidates lets a same-currency
        # caller omit or mislabel desired/minimum/fee/cap units while those raw
        # numbers are silently treated as portfolio-base amounts.
        constraint_currency = _payload_text(
            resolved_market[symbol],
            "monetary_constraint_currency",
        ).upper()
        desired_currency = _payload_text(
            resolved_objective[symbol],
            "desired_notional_currency",
        ).upper()
        valuation_constraint_currency = _payload_text(
            valuation,
            "source_monetary_currency",
        ).upper()
        valuation_desired_currency = _payload_text(
            valuation,
            "desired_notional_currency",
        ).upper()
        if constraint_currency != quote_currency:
            raise ValueError(
                f"market monetary constraints for {symbol} must declare quote-currency units"
            )
        if desired_currency != quote_currency:
            raise ValueError(
                f"objective desired_notional for {symbol} must declare quote-currency units"
            )
        if valuation_constraint_currency != constraint_currency:
            raise ValueError(
                f"valuation source monetary currency mismatch for {symbol}"
            )
        if valuation_desired_currency != desired_currency:
            raise ValueError(
                f"valuation desired_notional currency mismatch for {symbol}"
            )
        if quote_currency == base_currency:
            monetary_rate = Decimal("1")
        else:
            monetary_rate = valuation_fx_rate

        desired_notional_base = item.candidate.desired_notional * monetary_rate
        min_notional_base = item.candidate.min_notional * monetary_rate
        fee_floor_base = item.candidate.fee_floor * monetary_rate
        max_executable_notional_base = (
            None
            if item.candidate.max_executable_notional is None
            else item.candidate.max_executable_notional * monetary_rate
        )
        if quote_currency != base_currency:
            if _payload_decimal(
                valuation,
                "desired_notional_base",
            ) != desired_notional_base:
                raise ValueError(
                    f"valuation desired_notional_base mismatch for {symbol}"
                )
        elif "desired_notional_base" in valuation.payload:
            if _payload_decimal(
                valuation,
                "desired_notional_base",
            ) != desired_notional_base:
                raise ValueError(
                    f"valuation desired_notional_base mismatch for {symbol}"
                )

        try:
            normalized = normalize_allocation_valuation(
                symbol=symbol,
                market_payload=resolved_market[symbol].payload,
                valuation_payload=valuation.payload,
                source_price=item.candidate.price,
                expected_cost_rate=item.candidate.cost_rate,
                expected_capital_requirement_rate=item.candidate.capital_requirement_rate,
                expected_min_notional_base=min_notional_base,
                expected_fee_floor_base=fee_floor_base,
                expected_max_executable_notional_base=max_executable_notional_base,
                decision_time=normalized_decision_time,
                portfolio_base_currency=base_currency,
            )
        except AllocationValuationError as error:
            raise ValueError(
                f"allocation valuation evidence is unusable for {symbol}: {error}"
            ) from error
        resolved_valuation[symbol] = valuation
        normalized_candidates.append(
            ObjectiveCandidate(
                candidate=AllocationCandidate(
                    symbol=symbol,
                    desired_notional=desired_notional_base,
                    price=normalized.unit_base_notional,
                    lot_size=item.candidate.lot_size,
                    cost_rate=item.candidate.cost_rate,
                    capital_requirement_rate=item.candidate.capital_requirement_rate,
                    min_notional=min_notional_base,
                    fee_floor=fee_floor_base,
                    max_executable_notional=max_executable_notional_base,
                ),
                expected_return_rate=item.expected_return_rate,
                risk_penalty_rate=item.risk_penalty_rate,
            )
        )

    stress_items = tuple(stress_source_evidence)
    if policy.require_adverse_stress_evidence and not stress_items:
        raise ValueError("evidence-bound allocation requires stress scenario evidence")
    strict_stress = []
    resolved_stress = []
    for item in stress_items:
        resolved = _resolve_allocation_evidence(
            item,
            resolved_evidence,
            expected_kind="STRESS_SCENARIO",
            expected_environment=normalized_environment,
            at=normalized_decision_time,
        )
        name = _payload_text(resolved, "name")
        shocks_raw = resolved.payload.get("shocks")
        versions_raw = resolved.payload.get("instrument_versions")
        if not isinstance(shocks_raw, Mapping) or not isinstance(versions_raw, Mapping):
            raise ValueError("stress evidence requires shocks and instrument_versions mappings")
        if set(shocks_raw) != set(symbols) or set(versions_raw) != set(symbols):
            raise ValueError("stress evidence must exactly cover candidate symbols")
        shocks = {
            symbol: _decimal(
                shocks_raw[symbol],
                name=f"stress evidence {name} shock {symbol}",
            )
            for symbol in symbols
        }
        for symbol in symbols:
            if _text(
                versions_raw[symbol],
                name=f"stress evidence {name} instrument version {symbol}",
            ) != instrument_versions[symbol]:
                raise ValueError(
                    f"stress evidence instrument_version mismatch for {symbol}"
                )
        strict_stress.append(
            StressScenarioEvidence.create(
                name=name,
                shocks=shocks,
                observed_at=resolved.observed_at,
                valid_until=resolved.valid_until,
                source_ref=f"{resolved.evidence_id}:{resolved.digest}",
            )
        )
        resolved_stress.append(resolved)

    objective_result = allocate_objective_targets(
        tuple(normalized_candidates),
        policy,
        stress_evidence=tuple(strict_stress),
        decision_time=normalized_decision_time,
        max_candidate_sets=max_candidate_sets,
    )
    all_evidence = [
        *(resolved_objective[symbol] for symbol in sorted(resolved_objective)),
        *(resolved_market[symbol] for symbol in sorted(resolved_market)),
        *(resolved_valuation[symbol] for symbol in sorted(resolved_valuation)),
        resolved_capital,
        *sorted(resolved_stress, key=lambda evidence: evidence.evidence_id),
    ]
    evidence_refs = tuple(
        (evidence.evidence_id, evidence.digest)
        for evidence in all_evidence
    )
    bound_instrument_versions = tuple(sorted(instrument_versions.items()))
    bound_capability_snapshot_ids = tuple(sorted(capability_snapshot_ids.items()))
    decision_digest = _allocation_decision_digest(
        objective_result,
        evidence_refs=evidence_refs,
        environment=normalized_environment,
        policy_version=normalized_policy_version,
        decision_time=normalized_decision_time,
        provider_id=provider_id,
        account_id=account_id,
        instrument_versions=bound_instrument_versions,
        capability_snapshot_ids=bound_capability_snapshot_ids,
        account_snapshot_id=account_snapshot_id,
        reconciliation_run_id=reconciliation_run_id,
        account_state_version=account_state_version,
        reservation_state_version=reservation_state_version,
        reservation_state_digest=reservation_state_digest,
        base_currency=base_currency,
    )
    return EvidenceBoundObjectiveAllocationResult(
        objective=objective_result,
        decision_digest=decision_digest,
        evidence_refs=evidence_refs,
        environment=normalized_environment,
        policy_version=normalized_policy_version,
        decision_time=normalized_decision_time,
        provider_id=provider_id,
        account_id=account_id,
        instrument_versions=bound_instrument_versions,
        capability_snapshot_ids=bound_capability_snapshot_ids,
        account_snapshot_id=account_snapshot_id,
        reconciliation_run_id=reconciliation_run_id,
        account_state_version=account_state_version,
        reservation_state_version=reservation_state_version,
        reservation_state_digest=reservation_state_digest,
        base_currency=base_currency,
    )


def _normalize_current_scope_mapping(
    value: Mapping[str, str],
    *,
    name: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, str] = {}
    for raw_symbol, raw_identity in value.items():
        symbol = _text(raw_symbol, name=f"{name} symbol")
        if symbol in normalized:
            raise ValueError(f"{name} symbols must be unique")
        normalized[symbol] = _text(raw_identity, name=f"{name} identity")
    return tuple(sorted(normalized.items()))


def revalidate_evidence_bound_allocation(
    result: EvidenceBoundObjectiveAllocationResult,
    *,
    resolved_evidence: Mapping[str, ImmutableAllocationEvidence],
    environment: str,
    as_of: str,
    current_policy_version: str,
    current_provider_id: str,
    current_instrument_versions: Mapping[str, str],
    current_capability_snapshot_ids: Mapping[str, str],
    current_account_id: str,
    current_account_snapshot_id: str,
    current_reconciliation_run_id: str,
    current_account_state_version: int,
    current_reservation_state_version: int,
    current_reservation_state_digest: str,
) -> bool:
    """Fail closed if evidence or reconciled capital state changed after proposal."""

    if not isinstance(result, EvidenceBoundObjectiveAllocationResult):
        raise TypeError("result must be EvidenceBoundObjectiveAllocationResult")
    normalized_environment = _text(environment, name="allocation environment").upper()
    if normalized_environment != result.environment:
        raise ValueError("allocation result environment does not match authority environment")
    point_instant = _instant(as_of, name="allocation revalidation time")
    decision_instant = _instant(
        result.decision_time,
        name="allocation proposal decision_time",
    )
    if point_instant < decision_instant:
        raise ValueError("allocation revalidation time precedes proposal decision_time")
    point = point_instant.isoformat().replace("+00:00", "Z")
    if _text(current_policy_version, name="current_policy_version") != result.policy_version:
        raise ValueError("policy version changed after allocation proposal")
    if _text(current_provider_id, name="current_provider_id") != result.provider_id:
        raise ValueError("provider identity changed after allocation proposal")
    if _normalize_current_scope_mapping(
        current_instrument_versions,
        name="current_instrument_versions",
    ) != result.instrument_versions:
        raise ValueError("instrument version scope changed after allocation proposal")
    if _normalize_current_scope_mapping(
        current_capability_snapshot_ids,
        name="current_capability_snapshot_ids",
    ) != result.capability_snapshot_ids:
        raise ValueError("capability snapshot scope changed after allocation proposal")
    if _text(current_account_id, name="current_account_id") != result.account_id:
        raise ValueError("account identity does not match allocation proposal")
    if _text(
        current_account_snapshot_id,
        name="current_account_snapshot_id",
    ) != result.account_snapshot_id:
        raise ValueError("account snapshot identity advanced after allocation proposal")
    if _text(
        current_reconciliation_run_id,
        name="current_reconciliation_run_id",
    ) != result.reconciliation_run_id:
        raise ValueError("reconciliation identity advanced after allocation proposal")
    if _text(
        current_reservation_state_digest,
        name="current_reservation_state_digest",
    ) != result.reservation_state_digest:
        raise ValueError("reservation state digest changed after allocation proposal")
    for name, actual, expected in (
        (
            "account state version",
            current_account_state_version,
            result.account_state_version,
        ),
        (
            "reservation state version",
            current_reservation_state_version,
            result.reservation_state_version,
        ),
    ):
        if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
            raise ValueError(f"current {name} must be a non-negative integer")
        if actual != expected:
            raise ValueError(f"{name} advanced after allocation proposal")

    for evidence_id, digest in result.evidence_refs:
        evidence = resolved_evidence.get(evidence_id)
        if not isinstance(evidence, ImmutableAllocationEvidence):
            raise ValueError(f"allocation evidence {evidence_id} no longer resolves")
        if evidence.digest != digest:
            raise ValueError(f"allocation evidence {evidence_id} changed after proposal")
        if evidence.environment != result.environment:
            raise ValueError(f"allocation evidence {evidence_id} environment changed")
        if not evidence.valid_at(point):
            raise ValueError(f"allocation evidence {evidence_id} is stale at admission")

    expected_digest = _allocation_decision_digest(
        result.objective,
        evidence_refs=result.evidence_refs,
        environment=result.environment,
        policy_version=result.policy_version,
        decision_time=result.decision_time,
        provider_id=result.provider_id,
        account_id=result.account_id,
        instrument_versions=result.instrument_versions,
        capability_snapshot_ids=result.capability_snapshot_ids,
        account_snapshot_id=result.account_snapshot_id,
        reconciliation_run_id=result.reconciliation_run_id,
        account_state_version=result.account_state_version,
        reservation_state_version=result.reservation_state_version,
        reservation_state_digest=result.reservation_state_digest,
        base_currency=result.base_currency,
    )
    if expected_digest != result.decision_digest:
        raise ValueError("allocation decision digest does not match result content")
    return True

