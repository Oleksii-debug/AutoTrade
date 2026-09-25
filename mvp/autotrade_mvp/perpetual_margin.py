"""Conservative perpetual margin and liquidation-headroom oracle.

This module is deliberately provider-neutral and non-authoritative. It does not
place orders, choose leverage, or post funding. It evaluates evidenced mark,
index, collateral conversion and provider margin tiers, then applies explicit
stress assumptions. Stale or incomplete evidence blocks new risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Sequence


class PerpetualMarginError(ValueError):
    pass


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerpetualMarginError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise PerpetualMarginError(f"{name} must be a finite decimal")
    return result


def _non_negative(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0:
        raise PerpetualMarginError(f"{name} must be non-negative")
    return result


def _positive(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= 0:
        raise PerpetualMarginError(f"{name} must be positive")
    return result


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerpetualMarginError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise PerpetualMarginError(f"{name} must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise PerpetualMarginError(f"{name} must be an ISO-8601 instant") from error
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class MarginTier:
    notional_upper_bound: Decimal
    maintenance_rate: Decimal
    maintenance_adjustment: Decimal = Decimal("0")
    adjustment_convention: Literal["ADD", "DEDUCT"] = "ADD"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "notional_upper_bound",
            _positive(self.notional_upper_bound, name="notional_upper_bound"),
        )
        rate = _non_negative(self.maintenance_rate, name="maintenance_rate")
        if rate > 1:
            raise PerpetualMarginError("maintenance_rate cannot exceed 1")
        object.__setattr__(self, "maintenance_rate", rate)
        object.__setattr__(
            self,
            "maintenance_adjustment",
            _non_negative(
                self.maintenance_adjustment,
                name="maintenance_adjustment",
            ),
        )
        if self.adjustment_convention not in {"ADD", "DEDUCT"}:
            raise PerpetualMarginError(
                "adjustment_convention must be ADD or DEDUCT"
            )

    def maintenance_requirement(self, notional: Decimal) -> Decimal:
        base = notional * self.maintenance_rate
        if self.adjustment_convention == "ADD":
            requirement = base + self.maintenance_adjustment
        else:
            requirement = base - self.maintenance_adjustment
        if requirement < 0:
            raise PerpetualMarginError(
                "margin tier formula produced negative maintenance"
            )
        return requirement


@dataclass(frozen=True, slots=True)
class PerpetualMarginEvidence:
    instrument_version: str
    mark_price: Decimal
    index_price: Decimal
    collateral_fx_to_settlement: Decimal
    mark_observed_at: str
    index_observed_at: str
    collateral_fx_observed_at: str
    margin_tiers_observed_at: str
    margin_tiers: tuple[MarginTier, ...]
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(self, "mark_price", _positive(self.mark_price, name="mark_price"))
        object.__setattr__(self, "index_price", _positive(self.index_price, name="index_price"))
        object.__setattr__(
            self,
            "collateral_fx_to_settlement",
            _positive(
                self.collateral_fx_to_settlement,
                name="collateral_fx_to_settlement",
            ),
        )
        for name in (
            "mark_observed_at",
            "index_observed_at",
            "collateral_fx_observed_at",
            "margin_tiers_observed_at",
        ):
            _instant(getattr(self, name), name=name)
        tiers = tuple(self.margin_tiers)
        if not tiers:
            raise PerpetualMarginError("margin_tiers cannot be empty")
        prior = Decimal("0")
        for tier in tiers:
            if not isinstance(tier, MarginTier):
                raise TypeError("margin_tiers must contain MarginTier")
            if tier.notional_upper_bound <= prior:
                raise PerpetualMarginError(
                    "margin tier upper bounds must be strictly increasing"
                )
            prior = tier.notional_upper_bound
        object.__setattr__(self, "margin_tiers", tiers)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, name="evidence_ref"))


@dataclass(frozen=True, slots=True)
class PerpetualStress:
    price_loss_fraction: Decimal
    collateral_fx_loss_fraction: Decimal
    exit_cost_fraction: Decimal
    additional_funding_loss: Decimal = Decimal("0")
    unavailable_exit_extra_loss: Decimal = Decimal("0")
    notional_increase_fraction: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in (
            "price_loss_fraction",
            "collateral_fx_loss_fraction",
            "exit_cost_fraction",
        ):
            value = _non_negative(getattr(self, name), name=name)
            if value > 1:
                raise PerpetualMarginError(f"{name} cannot exceed 1")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "additional_funding_loss",
            _non_negative(
                self.additional_funding_loss,
                name="additional_funding_loss",
            ),
        )
        object.__setattr__(
            self,
            "unavailable_exit_extra_loss",
            _non_negative(
                self.unavailable_exit_extra_loss,
                name="unavailable_exit_extra_loss",
            ),
        )
        notional_growth = _non_negative(
            self.notional_increase_fraction,
            name="notional_increase_fraction",
        )
        object.__setattr__(self, "notional_increase_fraction", notional_growth)


@dataclass(frozen=True, slots=True)
class PerpetualMarginResult:
    verdict: Literal["ALLOW_NEW_RISK", "BLOCK_NEW_RISK", "LIQUIDATION_STRESS"]
    notional: Decimal
    maintenance_requirement: Decimal
    stressed_maintenance_requirement: Decimal
    stressed_notional: Decimal
    current_equity_settlement: Decimal
    stressed_equity_settlement: Decimal
    liquidation_headroom: Decimal
    mark_index_divergence_bps: Decimal
    selected_tier_upper_bound: Decimal
    reasons: tuple[str, ...]


def _select_tier(notional: Decimal, tiers: Sequence[MarginTier]) -> MarginTier:
    for tier in tiers:
        if notional <= tier.notional_upper_bound:
            return tier
    raise PerpetualMarginError(
        "position notional exceeds evidenced margin tier coverage"
    )


def evaluate_perpetual_margin(
    *,
    instrument_version: str,
    signed_notional_settlement,
    collateral_amount,
    unrealized_pnl_settlement,
    evidence: PerpetualMarginEvidence,
    stress: PerpetualStress,
    evaluated_at: str,
    maximum_evidence_age_seconds: int,
    maximum_mark_index_divergence_bps,
    collateral_haircut_fraction=0,
) -> PerpetualMarginResult:
    """Evaluate current/stressed margin with explicit freshness and depeg stress.

    signed_notional_settlement must already be calculated using the
    instrument's qualified linear/inverse payoff convention. This oracle never
    silently converts an inverse contract as though it were linear.
    """

    if not isinstance(evidence, PerpetualMarginEvidence):
        raise TypeError("evidence must be PerpetualMarginEvidence")
    if not isinstance(stress, PerpetualStress):
        raise TypeError("stress must be PerpetualStress")
    instrument = _text(instrument_version, name="instrument_version")
    if instrument != evidence.instrument_version:
        raise PerpetualMarginError("instrument_version must match margin evidence")
    if (
        isinstance(maximum_evidence_age_seconds, bool)
        or not isinstance(maximum_evidence_age_seconds, int)
        or maximum_evidence_age_seconds < 0
    ):
        raise PerpetualMarginError(
            "maximum_evidence_age_seconds must be a non-negative integer"
        )

    signed_notional = _decimal(
        signed_notional_settlement,
        name="signed_notional_settlement",
    )
    notional = abs(signed_notional)
    collateral = _non_negative(collateral_amount, name="collateral_amount")
    unrealized = _decimal(
        unrealized_pnl_settlement,
        name="unrealized_pnl_settlement",
    )
    haircut = _non_negative(
        collateral_haircut_fraction,
        name="collateral_haircut_fraction",
    )
    if haircut > 1:
        raise PerpetualMarginError("collateral_haircut_fraction cannot exceed 1")
    divergence_limit = _non_negative(
        maximum_mark_index_divergence_bps,
        name="maximum_mark_index_divergence_bps",
    )

    tier = _select_tier(notional, evidence.margin_tiers)
    maintenance = tier.maintenance_requirement(notional)
    stressed_notional = notional * (
        Decimal("1") + stress.notional_increase_fraction
    )
    stressed_tier = _select_tier(stressed_notional, evidence.margin_tiers)
    stressed_maintenance = stressed_tier.maintenance_requirement(stressed_notional)

    now = _instant(evaluated_at, name="evaluated_at")
    max_age = timedelta(seconds=maximum_evidence_age_seconds)
    reasons: list[str] = []
    for field in (
        "mark_observed_at",
        "index_observed_at",
        "collateral_fx_observed_at",
        "margin_tiers_observed_at",
    ):
        observed = _instant(getattr(evidence, field), name=field)
        if observed > now:
            reasons.append(f"{field}:FUTURE_EVIDENCE")
        elif now - observed > max_age:
            reasons.append(f"{field}:STALE")

    divergence = (
        abs(evidence.mark_price - evidence.index_price)
        / evidence.index_price
        * Decimal("10000")
    )
    if divergence > divergence_limit:
        reasons.append("MARK_INDEX_DIVERGENCE")

    current_collateral_value = (
        collateral
        * evidence.collateral_fx_to_settlement
        * (Decimal("1") - haircut)
    )
    current_equity = current_collateral_value + unrealized

    stressed_fx = evidence.collateral_fx_to_settlement * (
        Decimal("1") - stress.collateral_fx_loss_fraction
    )
    stressed_collateral_value = (
        collateral * stressed_fx * (Decimal("1") - haircut)
    )
    price_loss = notional * stress.price_loss_fraction
    exit_cost = notional * stress.exit_cost_fraction
    stressed_equity = (
        stressed_collateral_value
        + unrealized
        - price_loss
        - exit_cost
        - stress.additional_funding_loss
        - stress.unavailable_exit_extra_loss
    )
    headroom = stressed_equity - stressed_maintenance

    if current_equity < maintenance:
        reasons.append("CURRENT_MAINTENANCE_BREACH")
    if headroom < 0:
        reasons.append("STRESSED_LIQUIDATION_HEADROOM_NEGATIVE")

    if (
        "CURRENT_MAINTENANCE_BREACH" in reasons
        or "STRESSED_LIQUIDATION_HEADROOM_NEGATIVE" in reasons
    ):
        verdict = "LIQUIDATION_STRESS"
    elif reasons:
        verdict = "BLOCK_NEW_RISK"
    else:
        verdict = "ALLOW_NEW_RISK"

    return PerpetualMarginResult(
        verdict=verdict,
        notional=notional,
        maintenance_requirement=maintenance,
        stressed_maintenance_requirement=stressed_maintenance,
        stressed_notional=stressed_notional,
        current_equity_settlement=current_equity,
        stressed_equity_settlement=stressed_equity,
        liquidation_headroom=headroom,
        mark_index_divergence_bps=divergence,
        selected_tier_upper_bound=tier.notional_upper_bound,
        reasons=tuple(reasons),
    )
