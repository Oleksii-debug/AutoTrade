"""Deterministic cost/risk-aware portfolio allocation foundation.

This module produces proposed targets only. It never sends orders and never
treats simulated or expected returns as evidence of profitability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from types import MappingProxyType
from typing import Mapping, Sequence


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


def _fraction(value: Decimal) -> Fraction:
    sign, digits, exponent = value.as_tuple()
    integer = 0
    for digit in digits:
        integer = integer * 10 + digit
    if sign:
        integer = -integer
    if exponent >= 0:
        return Fraction(integer * (10**exponent), 1)
    return Fraction(integer, 10 ** (-exponent))


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
    lot_notional = _fraction(price) * _fraction(lot_size)
    exact_lots = _fraction(abs(notional)) / lot_notional
    lots = exact_lots.numerator // exact_lots.denominator
    quantity = Decimal(lots) * lot_size
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
    """Select a deterministic feasible candidate prefix by expected net utility.

    Candidates are ranked by directional expected return less an explicit risk
    penalty. Every tested prefix is then passed through the same hard allocation
    constraints. Estimated execution cost is subtracted exactly once from the
    objective. The search is intentionally transparent and bounded; it is not a
    claim of globally optimal portfolio construction. If the search budget is
    exceeded, evidence is incomplete, or no positive-utility feasible target is
    found, the result is a no-increase cash fallback.
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
            objective_version="deterministic-net-utility-v1",
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
                objective_version="deterministic-net-utility-v1",
                reason=evidence_problem,
            )
        normalized_evidence = tuple(stress_evidence)

    if len(candidates) > max_candidate_sets:
        fallback = _cash_fallback(
            allocation_candidates,
            reason="objective search budget exceeded before evaluation",
        )
        return ObjectiveAllocationResult(
            allocation=fallback,
            selected_symbols=(),
            expected_net_utility=Decimal("0"),
            objective_version="deterministic-net-utility-v1",
            reason="objective search budget exceeded before evaluation",
        )

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
            objective_version="deterministic-net-utility-v1",
            reason="no candidate has positive expected return after risk penalty",
        )

    objective_by_symbol = {
        item.candidate.symbol: item
        for item in ranked
    }
    best_result: AllocationResult | None = None
    best_symbols: tuple[str, ...] = ()
    best_utility = Decimal("0")

    for prefix_size in range(1, len(ranked) + 1):
        prefix = ranked[:prefix_size]
        prefix_symbols = tuple(item.candidate.symbol for item in prefix)
        projected_stress = {
            scenario_name: {
                symbol: scenario[symbol]
                for symbol in prefix_symbols
            }
            for scenario_name, scenario in normalized_stress.items()
        }
        projected_evidence = tuple(
            StressScenarioEvidence.create(
                name=item.name,
                shocks={symbol: item.shocks[symbol] for symbol in prefix_symbols},
                observed_at=item.observed_at,
                valid_until=item.valid_until,
                source_ref=item.source_ref,
            )
            for item in normalized_evidence
        )
        result = allocate_targets(
            [item.candidate for item in prefix],
            policy,
            stress_scenarios=projected_stress,
            stress_evidence=projected_evidence,
            decision_time=decision_time,
        )
        if result.status != "ALLOCATED":
            continue
        utility = _expected_net_utility(result, objective_by_symbol)
        if utility > best_utility:
            best_result = result
            best_symbols = prefix_symbols
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
            objective_version="deterministic-net-utility-v1",
            reason=(
                "no positive-utility feasible allocation survived hard "
                "constraints and estimated costs"
            ),
        )

    return ObjectiveAllocationResult(
        allocation=best_result,
        selected_symbols=best_symbols,
        expected_net_utility=best_utility,
        objective_version="deterministic-net-utility-v1",
        reason=(
            "selected the highest positive expected-net-utility deterministic "
            "candidate prefix that passed all hard allocation constraints"
        ),
    )
