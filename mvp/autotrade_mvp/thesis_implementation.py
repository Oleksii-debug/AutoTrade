"""Provider-neutral market-thesis to execution-instrument screening.

This module deliberately stops *before* order/risk/send authority.  A market
thesis is not an order and an implementation candidate is not permission to
trade.  The selector only answers whether the supplied candidate evidence is
feasible under an explicit screening policy and, when possible without an
arbitrary utility score, whether one feasible implementation Pareto-dominates
all others.

Ambiguity is therefore first-class: when multiple non-dominated expressions
remain, the result is ``AMBIGUOUS`` and downstream portfolio policy must make a
separate authoritative decision.  If nothing is feasible the result is
``NO_TRADE``.  Neither outcome can bypass canonical allocation, risk,
reservation, provider qualification, dispatch, reconciliation, or recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from .exact_decimal import ExactDecimalError, exact_sum, parse_bounded_exact_decimal


class ThesisImplementationError(ValueError):
    """Raised when thesis/implementation screening input is not canonical."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    normalized = value.strip()
    if not normalized:
        raise ThesisImplementationError(f"{name} is required")
    return normalized


def _bool(value: object, *, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be exact bool")
    return value


def _decimal(value: object, *, name: str, non_negative: bool = False) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        raise TypeError(f"{name} must use exact Decimal, string or integer input")
    try:
        result = parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ThesisImplementationError(
            f"{name} must be a bounded finite decimal"
        ) from error
    if non_negative and result < 0:
        raise ThesisImplementationError(f"{name} must be non-negative")
    return result


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be exact datetime")
    if value.tzinfo is None or type(value.tzinfo) is not timezone:
        raise ThesisImplementationError(
            f"{name} must use a built-in fixed-offset timezone"
        )
    return value.astimezone(timezone.utc)


def _asset_classes(values: object) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError("permitted_asset_classes must be an exact tuple")
    normalized: list[str] = []
    for value in values:
        item = _text(value, name="permitted_asset_class").upper()
        if item in normalized:
            raise ThesisImplementationError(
                "permitted_asset_classes must not contain duplicates"
            )
        normalized.append(item)
    return tuple(normalized)


@dataclass(frozen=True)
class MarketThesis:
    """A provider-neutral desired market exposure, not a trading command."""

    thesis_id: str
    subject: str
    direction: str
    as_of: datetime
    horizon_end: datetime
    required_notional: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "thesis_id", _text(self.thesis_id, name="thesis_id"))
        object.__setattr__(self, "subject", _text(self.subject, name="subject"))
        direction = _text(self.direction, name="direction").upper()
        if direction not in {"LONG", "SHORT"}:
            raise ThesisImplementationError("direction must be LONG or SHORT")
        object.__setattr__(self, "direction", direction)
        as_of = _utc(self.as_of, name="as_of")
        horizon_end = _utc(self.horizon_end, name="horizon_end")
        if horizon_end <= as_of:
            raise ThesisImplementationError("horizon_end must be after as_of")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "horizon_end", horizon_end)
        required = _decimal(
            self.required_notional,
            name="required_notional",
            non_negative=True,
        )
        if required <= 0:
            raise ThesisImplementationError("required_notional must be positive")
        object.__setattr__(self, "required_notional", required)


@dataclass(frozen=True)
class ImplementationPolicy:
    """Explicit screening limits; this is not the canonical risk policy."""

    max_total_cost_rate: Decimal
    max_leverage_ratio: Decimal
    max_liquidation_risk: Decimal
    min_liquidity_capacity: Decimal = Decimal("0")
    permitted_asset_classes: tuple[str, ...] = ()
    require_qualified_route: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "max_total_cost_rate",
            "max_leverage_ratio",
            "max_liquidation_risk",
            "min_liquidity_capacity",
        ):
            value = _decimal(
                getattr(self, field_name),
                name=field_name,
                non_negative=True,
            )
            object.__setattr__(self, field_name, value)
        if self.max_liquidation_risk > 1:
            raise ThesisImplementationError(
                "max_liquidation_risk must be between zero and one"
            )
        object.__setattr__(
            self,
            "permitted_asset_classes",
            _asset_classes(self.permitted_asset_classes),
        )
        object.__setattr__(
            self,
            "require_qualified_route",
            _bool(self.require_qualified_route, name="require_qualified_route"),
        )


@dataclass(frozen=True)
class ImplementationCandidate:
    """Detached evidence for one possible expression of a market thesis.

    ``route_qualified`` and the legal/technical flags are screening evidence,
    not authorization.  The actual provider/risk/send authorities must be
    re-resolved at their canonical boundaries before any irreversible action.
    """

    candidate_id: str
    thesis_id: str
    instrument_version: str
    provider_id: str
    asset_class: str
    exposure_direction: str
    route_id: str
    legal: bool
    technically_available: bool
    route_qualified: bool
    fee_rate: Decimal
    spread_rate: Decimal
    holding_cost_rate: Decimal
    leverage_ratio: Decimal
    liquidation_risk: Decimal
    liquidity_capacity: Decimal
    tradable_until: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_id",
            "thesis_id",
            "instrument_version",
            "provider_id",
            "asset_class",
            "route_id",
        ):
            value = _text(getattr(self, field_name), name=field_name)
            if field_name in {"provider_id", "asset_class"}:
                value = value.upper()
            object.__setattr__(self, field_name, value)
        direction = _text(
            self.exposure_direction,
            name="exposure_direction",
        ).upper()
        if direction not in {"LONG", "SHORT"}:
            raise ThesisImplementationError(
                "exposure_direction must be LONG or SHORT"
            )
        object.__setattr__(self, "exposure_direction", direction)
        for field_name in ("legal", "technically_available", "route_qualified"):
            object.__setattr__(
                self,
                field_name,
                _bool(getattr(self, field_name), name=field_name),
            )
        for field_name in (
            "fee_rate",
            "spread_rate",
            "holding_cost_rate",
            "leverage_ratio",
            "liquidation_risk",
            "liquidity_capacity",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(
                    getattr(self, field_name),
                    name=field_name,
                    non_negative=True,
                ),
            )
        if self.liquidation_risk > 1:
            raise ThesisImplementationError(
                "liquidation_risk must be between zero and one"
            )
        if self.tradable_until is not None:
            object.__setattr__(
                self,
                "tradable_until",
                _utc(self.tradable_until, name="tradable_until"),
            )

    @property
    def total_cost_rate(self) -> Decimal:
        return exact_sum((self.fee_rate, self.spread_rate, self.holding_cost_rate))


@dataclass(frozen=True)
class ImplementationDecision:
    """Deterministic screening result with a first-class no-trade outcome."""

    thesis_id: str
    status: str
    selected_candidate_id: str | None
    feasible_candidate_ids: tuple[str, ...]
    pareto_frontier_ids: tuple[str, ...]
    rejected_reasons: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "thesis_id", _text(self.thesis_id, name="thesis_id"))
        status = _text(self.status, name="status").upper()
        if status not in {"SELECTED", "AMBIGUOUS", "NO_TRADE"}:
            raise ThesisImplementationError("unsupported decision status")
        object.__setattr__(self, "status", status)
        if self.selected_candidate_id is not None:
            object.__setattr__(
                self,
                "selected_candidate_id",
                _text(self.selected_candidate_id, name="selected_candidate_id"),
            )
        feasible = tuple(
            _text(item, name="feasible_candidate_id")
            for item in self.feasible_candidate_ids
        )
        frontier = tuple(
            _text(item, name="pareto_frontier_id")
            for item in self.pareto_frontier_ids
        )
        object.__setattr__(self, "feasible_candidate_ids", feasible)
        object.__setattr__(self, "pareto_frontier_ids", frontier)
        if type(self.rejected_reasons) is not dict:
            raise TypeError("rejected_reasons must be an exact dict")
        detached: dict[str, tuple[str, ...]] = {}
        for candidate_id, reasons in self.rejected_reasons.items():
            cid = _text(candidate_id, name="rejected_candidate_id")
            if type(reasons) is not tuple:
                raise TypeError("rejection reasons must be exact tuples")
            detached[cid] = tuple(_text(reason, name="rejection_reason") for reason in reasons)
        object.__setattr__(self, "rejected_reasons", MappingProxyType(detached))


def _detach_thesis(value: object) -> MarketThesis:
    if type(value) is not MarketThesis:
        raise TypeError("thesis must be exact MarketThesis")
    return MarketThesis(
        thesis_id=value.thesis_id,
        subject=value.subject,
        direction=value.direction,
        as_of=value.as_of,
        horizon_end=value.horizon_end,
        required_notional=value.required_notional,
    )


def _detach_policy(value: object) -> ImplementationPolicy:
    if type(value) is not ImplementationPolicy:
        raise TypeError("policy must be exact ImplementationPolicy")
    return ImplementationPolicy(
        max_total_cost_rate=value.max_total_cost_rate,
        max_leverage_ratio=value.max_leverage_ratio,
        max_liquidation_risk=value.max_liquidation_risk,
        min_liquidity_capacity=value.min_liquidity_capacity,
        permitted_asset_classes=value.permitted_asset_classes,
        require_qualified_route=value.require_qualified_route,
    )


def _detach_candidate(value: object) -> ImplementationCandidate:
    if type(value) is not ImplementationCandidate:
        raise TypeError("candidates must be exact ImplementationCandidate")
    return ImplementationCandidate(
        candidate_id=value.candidate_id,
        thesis_id=value.thesis_id,
        instrument_version=value.instrument_version,
        provider_id=value.provider_id,
        asset_class=value.asset_class,
        exposure_direction=value.exposure_direction,
        route_id=value.route_id,
        legal=value.legal,
        technically_available=value.technically_available,
        route_qualified=value.route_qualified,
        fee_rate=value.fee_rate,
        spread_rate=value.spread_rate,
        holding_cost_rate=value.holding_cost_rate,
        leverage_ratio=value.leverage_ratio,
        liquidation_risk=value.liquidation_risk,
        liquidity_capacity=value.liquidity_capacity,
        tradable_until=value.tradable_until,
    )


def _rejection_reasons(
    thesis: MarketThesis,
    policy: ImplementationPolicy,
    candidate: ImplementationCandidate,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate.thesis_id != thesis.thesis_id:
        reasons.append("THESIS_ID_MISMATCH")
    if candidate.exposure_direction != thesis.direction:
        reasons.append("EXPOSURE_DIRECTION_MISMATCH")
    if not candidate.legal:
        reasons.append("LEGAL_RESTRICTION")
    if not candidate.technically_available:
        reasons.append("TECHNICALLY_UNAVAILABLE")
    if policy.require_qualified_route and not candidate.route_qualified:
        reasons.append("ROUTE_NOT_QUALIFIED")
    if (
        policy.permitted_asset_classes
        and candidate.asset_class not in policy.permitted_asset_classes
    ):
        reasons.append("ASSET_CLASS_NOT_PERMITTED")
    if (
        candidate.tradable_until is not None
        and candidate.tradable_until < thesis.horizon_end
    ):
        reasons.append("HORIZON_NOT_COVERED")
    required_capacity = max(
        thesis.required_notional,
        policy.min_liquidity_capacity,
    )
    if candidate.liquidity_capacity < required_capacity:
        reasons.append("INSUFFICIENT_LIQUIDITY_CAPACITY")
    if candidate.total_cost_rate > policy.max_total_cost_rate:
        reasons.append("TOTAL_COST_LIMIT")
    if candidate.leverage_ratio > policy.max_leverage_ratio:
        reasons.append("LEVERAGE_LIMIT")
    if candidate.liquidation_risk > policy.max_liquidation_risk:
        reasons.append("LIQUIDATION_RISK_LIMIT")
    return tuple(reasons)


def _dominates(
    candidate: ImplementationCandidate,
    other: ImplementationCandidate,
) -> bool:
    """Return true only for strict Pareto dominance, never a weighted score."""

    candidate_metrics = (
        candidate.total_cost_rate,
        candidate.leverage_ratio,
        candidate.liquidation_risk,
    )
    other_metrics = (
        other.total_cost_rate,
        other.leverage_ratio,
        other.liquidation_risk,
    )
    no_worse = all(left <= right for left, right in zip(candidate_metrics, other_metrics))
    no_worse = no_worse and candidate.liquidity_capacity >= other.liquidity_capacity
    strictly_better = any(
        left < right for left, right in zip(candidate_metrics, other_metrics)
    ) or candidate.liquidity_capacity > other.liquidity_capacity
    return no_worse and strictly_better


def select_implementation(
    *,
    thesis: MarketThesis,
    policy: ImplementationPolicy,
    candidates: Sequence[ImplementationCandidate],
) -> ImplementationDecision:
    """Screen implementations and select only an unambiguous Pareto winner.

    The returned candidate id is a proposal identifier.  It never grants
    capital, risk, provider, reservation, dispatch, PAPER/LIVE, or release
    authority.
    """

    detached_thesis = _detach_thesis(thesis)
    detached_policy = _detach_policy(policy)
    if type(candidates) not in {tuple, list}:
        raise TypeError("candidates must be an exact list or tuple")
    snapshot = tuple(_detach_candidate(item) for item in candidates)
    seen: set[str] = set()
    for candidate in snapshot:
        if candidate.candidate_id in seen:
            raise ThesisImplementationError("candidate_id must be unique")
        seen.add(candidate.candidate_id)

    rejected: dict[str, tuple[str, ...]] = {}
    feasible: list[ImplementationCandidate] = []
    for candidate in snapshot:
        reasons = _rejection_reasons(detached_thesis, detached_policy, candidate)
        if reasons:
            rejected[candidate.candidate_id] = reasons
        else:
            feasible.append(candidate)

    feasible.sort(key=lambda item: item.candidate_id)
    if not feasible:
        return ImplementationDecision(
            thesis_id=detached_thesis.thesis_id,
            status="NO_TRADE",
            selected_candidate_id=None,
            feasible_candidate_ids=(),
            pareto_frontier_ids=(),
            rejected_reasons=rejected,
        )

    frontier = [
        candidate
        for candidate in feasible
        if not any(
            _dominates(other, candidate)
            for other in feasible
            if other.candidate_id != candidate.candidate_id
        )
    ]
    frontier.sort(key=lambda item: item.candidate_id)
    feasible_ids = tuple(item.candidate_id for item in feasible)
    frontier_ids = tuple(item.candidate_id for item in frontier)
    if len(frontier) == 1:
        return ImplementationDecision(
            thesis_id=detached_thesis.thesis_id,
            status="SELECTED",
            selected_candidate_id=frontier[0].candidate_id,
            feasible_candidate_ids=feasible_ids,
            pareto_frontier_ids=frontier_ids,
            rejected_reasons=rejected,
        )
    return ImplementationDecision(
        thesis_id=detached_thesis.thesis_id,
        status="AMBIGUOUS",
        selected_candidate_id=None,
        feasible_candidate_ids=feasible_ids,
        pareto_frontier_ids=frontier_ids,
        rejected_reasons=rejected,
    )
