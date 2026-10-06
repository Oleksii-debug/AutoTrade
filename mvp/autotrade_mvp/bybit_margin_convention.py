"""Deterministic Bybit risk-limit convention over neutral source facts.

Bybit publishes maintenance/initial margin rates as percentage points.  For
isolated/cross perpetual and futures risk limits, maintenance margin is:

    position_value * MMR - maintenance_margin_deduction

This module converts the provider percentage-point presentation to the shared
fractional MarginTier convention using exact rational arithmetic.  It validates
the provider deduction recurrence before producing any tier set.  The result is
still neutral derived evidence; it is not provider wire-origin authority and
cannot bypass the PAPER/LIVE margin firebreak.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json

from .bybit_margin_market import (
    BYBIT_MARGIN_MARKET_PARSER_CONTRACT_DIGEST,
    BybitMarginRiskLimitPage,
    BybitMarginRiskTierFact,
)
from .exact_decimal import (
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    exact_add,
    exact_multiply,
    exact_subtract,
    terminating_decimal,
)
from .perpetual_margin import MarginTier, PerpetualMarginError


BYBIT_MARGIN_TIER_CONVENTION_IDENTITY = (
    "BYBIT_V5_RISK_LIMIT_PERCENT_POINTS_DEDUCT_V1"
)
_CONVENTION_MATERIAL = {
    "identity": BYBIT_MARGIN_TIER_CONVENTION_IDENTITY,
    "provider": "BYBIT",
    "categories": ["linear", "inverse"],
    "maintenance_rate_source_unit": "PERCENT_POINTS",
    "maintenance_rate_internal_unit": "FRACTION",
    "rate_scale": "100",
    "maintenance_formula": "NOTIONAL_TIMES_RATE_MINUS_DEDUCTION",
    "deduction_recurrence": (
        "PREVIOUS_RISK_LIMIT_TIMES_RATE_DELTA_PLUS_PREVIOUS_DEDUCTION"
    ),
    "parser_contract_digest": BYBIT_MARGIN_MARKET_PARSER_CONTRACT_DIGEST,
}
BYBIT_MARGIN_TIER_CONVENTION_DIGEST = "sha256:" + sha256(
    json.dumps(
        _CONVENTION_MATERIAL,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()


class BybitMarginConventionError(PerpetualMarginError):
    """Raised when provider tier facts cannot satisfy the canonical convention."""


def _rate_fraction(value: Decimal, *, name: str) -> Decimal:
    try:
        rational = bounded_fraction(as_fraction(value) / 100)
        result = terminating_decimal(rational)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise BybitMarginConventionError(
            f"{name} cannot be converted exactly from percentage points"
        ) from error
    if result <= 0 or result > 1:
        raise BybitMarginConventionError(
            f"{name} must convert to a fractional rate in (0, 1]"
        )
    return result


def _deduction(value: Decimal | None) -> Decimal:
    return Decimal("0") if value is None else value


@dataclass(frozen=True, slots=True)
class BybitMarginTierConvention:
    tiers: tuple[MarginTier, ...]
    source_risk_ids: tuple[int, ...]
    symbol: str
    category: str
    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    capability_snapshot_id: str
    instrument_version: str
    query_digest: str
    evidence_ref: str
    response_sha256: str
    observed_at: str
    provider_time_ms: int
    revision_id: str
    convention_identity: str = BYBIT_MARGIN_TIER_CONVENTION_IDENTITY
    convention_digest: str = BYBIT_MARGIN_TIER_CONVENTION_DIGEST


def _revision_material(
    page: BybitMarginRiskLimitPage,
    rows: tuple[BybitMarginRiskTierFact, ...],
    rates: tuple[Decimal, ...],
) -> bytes:
    material = {
        "schema": 1,
        "convention_digest": BYBIT_MARGIN_TIER_CONVENTION_DIGEST,
        "provider_id": page.provider_id,
        "account_id": page.account_id,
        "entity_id": page.entity_id,
        "environment": page.environment,
        "capability_snapshot_id": page.capability_snapshot_id,
        "instrument_version": page.instrument_version,
        "symbol": page.symbol,
        "category": page.category,
        "query_digest": page.query_digest,
        "evidence_ref": page.evidence_ref,
        "response_sha256": page.response_sha256,
        "observed_at": page.observed_at,
        "provider_time_ms": page.provider_time_ms,
        "tiers": [
            {
                "risk_id": row.risk_id,
                "risk_limit_value": str(row.risk_limit_value),
                "maintenance_rate": str(rate),
                "initial_margin_percent_points": str(row.initial_margin),
                "max_leverage": str(row.max_leverage),
                "maintenance_margin_deduction": str(
                    _deduction(row.maintenance_margin_deduction)
                ),
            }
            for row, rate in zip(rows, rates, strict=True)
        ],
    }
    return json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def derive_bybit_margin_tier_convention(
    page: BybitMarginRiskLimitPage,
) -> BybitMarginTierConvention:
    """Validate and convert one complete provider risk-limit table exactly."""

    if type(page) is not BybitMarginRiskLimitPage:
        raise TypeError("page must be exact BybitMarginRiskLimitPage")
    if page.provider_id != "BYBIT":
        raise BybitMarginConventionError("margin tier source provider must be BYBIT")
    if page.category not in {"linear", "inverse"}:
        raise BybitMarginConventionError("unsupported Bybit margin category")

    rows = tuple(sorted(page.tiers, key=lambda row: row.risk_id))
    if not rows:
        raise BybitMarginConventionError("risk-limit tier table must not be empty")
    expected_ids = tuple(range(1, len(rows) + 1))
    if tuple(row.risk_id for row in rows) != expected_ids:
        raise BybitMarginConventionError(
            "risk-limit ids must be contiguous starting at one"
        )
    if not rows[0].is_lowest_risk or any(
        row.is_lowest_risk for row in rows[1:]
    ):
        raise BybitMarginConventionError(
            "risk-limit tier one must be the unique lowest-risk tier"
        )

    rates = tuple(
        _rate_fraction(
            row.maintenance_margin,
            name=f"risk tier {row.risk_id} maintenance margin rate",
        )
        for row in rows
    )
    initial_rates = tuple(
        _rate_fraction(
            row.initial_margin,
            name=f"risk tier {row.risk_id} initial margin rate",
        )
        for row in rows
    )
    deductions = tuple(_deduction(row.maintenance_margin_deduction) for row in rows)

    if deductions[0] != 0:
        raise BybitMarginConventionError(
            "lowest-risk tier maintenance margin deduction must be zero"
        )

    previous_limit = Decimal("0")
    previous_rate = Decimal("0")
    previous_initial = Decimal("0")
    previous_leverage: Decimal | None = None
    previous_deduction = Decimal("0")
    tiers: list[MarginTier] = []

    for row, rate, initial_rate, deduction in zip(
        rows,
        rates,
        initial_rates,
        deductions,
        strict=True,
    ):
        if row.risk_limit_value <= previous_limit:
            raise BybitMarginConventionError(
                "risk-limit position bounds must increase with tier"
            )
        if rate < previous_rate:
            raise BybitMarginConventionError(
                "maintenance margin rate must not decrease with tier"
            )
        if initial_rate < previous_initial:
            raise BybitMarginConventionError(
                "initial margin rate must not decrease with tier"
            )
        if rate >= initial_rate:
            raise BybitMarginConventionError(
                "maintenance margin rate must stay below initial margin rate"
            )
        if previous_leverage is not None and row.max_leverage > previous_leverage:
            raise BybitMarginConventionError(
                "maximum leverage must not increase with tier"
            )

        if row.risk_id > 1:
            if row.maintenance_margin_deduction is None:
                raise BybitMarginConventionError(
                    "non-lowest risk tier requires maintenance margin deduction"
                )
            try:
                expected = exact_add(
                    previous_deduction,
                    exact_multiply(
                        previous_limit,
                        exact_subtract(rate, previous_rate),
                    ),
                )
            except ExactDecimalError as error:
                raise BybitMarginConventionError(
                    "risk-limit deduction recurrence exceeds exact numeric envelope"
                ) from error
            if deduction != expected:
                raise BybitMarginConventionError(
                    "maintenance margin deduction conflicts with Bybit tier recurrence"
                )

        tiers.append(
            MarginTier(
                notional_upper_bound=row.risk_limit_value,
                maintenance_rate=rate,
                maintenance_adjustment=deduction,
                adjustment_convention="DEDUCT",
            )
        )
        previous_limit = row.risk_limit_value
        previous_rate = rate
        previous_initial = initial_rate
        previous_leverage = row.max_leverage
        previous_deduction = deduction

    revision_bytes = _revision_material(page, rows, rates)
    return BybitMarginTierConvention(
        tiers=tuple(tiers),
        source_risk_ids=tuple(row.risk_id for row in rows),
        symbol=page.symbol,
        category=page.category,
        provider_id=page.provider_id,
        account_id=page.account_id,
        entity_id=page.entity_id,
        environment=page.environment,
        capability_snapshot_id=page.capability_snapshot_id,
        instrument_version=page.instrument_version,
        query_digest=page.query_digest,
        evidence_ref=page.evidence_ref,
        response_sha256=page.response_sha256,
        observed_at=page.observed_at,
        provider_time_ms=page.provider_time_ms,
        revision_id="bybit-margin-tier:sha256:" + sha256(revision_bytes).hexdigest(),
    )
