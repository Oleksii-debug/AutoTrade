"""Exact valuation normalization for WP-32 portfolio proposals.

This module is deliberately proposal-only.  It reuses the canonical FX
valuation semantics and the existing linear contract notional formula, and it
fails closed for payoff families that require a richer canonical payoff
boundary.  It never grants trading authority or sends provider commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from .fx_valuation import FxQuote, FxValuationError, value_amount
from .perpetuals import PerpetualError, linear_notional


class AllocationValuationError(ValueError):
    """Valuation evidence is incomplete, inconsistent, stale or unsupported."""


_COST_COMPONENTS = ("execution", "financing", "funding", "borrow", "fx")
_SUPPORTED_LINEAR_ASSET_CLASSES = {
    "CASH_EQUITY",
    "FUND",
    "FX",
    "CRYPTO_SPOT",
    "FUTURE",
    "PERPETUAL",
}


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise AllocationValuationError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise AllocationValuationError(f"{name} must be a finite decimal")
    return result


def _positive(value, *, name: str, allow_zero: bool = False) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0 or (result == 0 and not allow_zero):
        raise AllocationValuationError(
            f"{name} must be {'non-negative' if allow_zero else 'positive'}"
        )
    return result


def _text(value, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AllocationValuationError(f"{name} is required")
    return value.strip()


def _currency(value, *, name: str) -> str:
    return _text(value, name=name).upper()


def _instant(value, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AllocationValuationError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AllocationValuationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _sha256(value, *, name: str) -> str:
    text = _text(value, name=name)
    if (
        len(text) != 64
        or text.lower() != text
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise AllocationValuationError(f"{name} must be lowercase sha256 hex")
    return text


def _mapping(value, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AllocationValuationError(f"{name} must be a mapping")
    return value


def _optional_decimal_equal(actual, expected, *, name: str) -> None:
    if expected is None:
        if actual is not None:
            raise AllocationValuationError(f"{name} must be null")
        return
    if actual is None:
        raise AllocationValuationError(f"{name} is required")
    if _decimal(actual, name=name) != expected:
        raise AllocationValuationError(f"{name} mismatch")


@dataclass(frozen=True)
class AllocationValuation:
    """One candidate normalized into the portfolio base currency."""

    symbol: str
    asset_class: str
    payoff: str
    quantity_unit: str
    contract_multiplier: Decimal
    quote_currency: str
    settlement_currency: str
    portfolio_base_currency: str
    unit_base_notional: Decimal
    fx_rate: Decimal
    fx_source_id: str
    fx_evidence_sha256: str | None
    payoff_identity: str
    cost_rate_components: Mapping[str, Decimal]
    cost_evidence_refs: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cost_rate_components",
            MappingProxyType(dict(self.cost_rate_components)),
        )
        object.__setattr__(
            self,
            "cost_evidence_refs",
            MappingProxyType(dict(self.cost_evidence_refs)),
        )


def normalize_allocation_valuation(
    *,
    symbol: str,
    market_payload: Mapping[str, object],
    valuation_payload: Mapping[str, object],
    source_price,
    expected_cost_rate,
    expected_capital_requirement_rate,
    expected_min_notional_base,
    expected_fee_floor_base,
    expected_max_executable_notional_base,
    decision_time: str,
    portfolio_base_currency: str,
) -> AllocationValuation:
    """Validate and normalize one exact linear candidate into base currency.

    Inverse futures/perpetuals and options are deliberately rejected here until
    their canonical nonlinear payoff evidence is supplied to WP-32.  They must
    never fall through to a linear quantity*price approximation.
    """

    symbol_text = _text(symbol, name="allocation symbol")
    market = _mapping(market_payload, name=f"{symbol_text} market payload")
    valuation = _mapping(
        valuation_payload,
        name=f"{symbol_text} valuation payload",
    )
    point = _instant(decision_time, name="allocation decision_time")
    base_currency = _currency(
        portfolio_base_currency,
        name="portfolio base currency",
    )

    for key in (
        "instrument_version",
        "capability_snapshot_id",
        "asset_class",
        "payoff",
        "quantity_unit",
        "contract_multiplier",
        "quote_currency",
        "settlement_currency",
    ):
        if key not in market:
            raise AllocationValuationError(
                f"{symbol_text} market evidence is missing {key}"
            )
        if key not in valuation:
            raise AllocationValuationError(
                f"{symbol_text} valuation evidence is missing {key}"
            )
        market_value = market[key]
        valuation_value = valuation[key]
        if key == "contract_multiplier":
            if _positive(
                market_value,
                name=f"{symbol_text} market contract_multiplier",
            ) != _positive(
                valuation_value,
                name=f"{symbol_text} valuation contract_multiplier",
            ):
                raise AllocationValuationError(
                    f"{symbol_text} contract_multiplier evidence mismatch"
                )
        else:
            if _text(
                market_value,
                name=f"{symbol_text} market {key}",
            ) != _text(
                valuation_value,
                name=f"{symbol_text} valuation {key}",
            ):
                raise AllocationValuationError(
                    f"{symbol_text} {key} evidence mismatch"
                )

    if _text(valuation.get("symbol"), name=f"{symbol_text} valuation symbol") != symbol_text:
        raise AllocationValuationError(f"{symbol_text} valuation symbol mismatch")
    asset_class = _text(
        valuation["asset_class"],
        name=f"{symbol_text} valuation asset_class",
    ).upper()
    payoff = _text(
        valuation["payoff"],
        name=f"{symbol_text} valuation payoff",
    ).upper()
    quantity_unit = _text(
        valuation["quantity_unit"],
        name=f"{symbol_text} valuation quantity_unit",
    )
    multiplier = _positive(
        valuation["contract_multiplier"],
        name=f"{symbol_text} valuation contract_multiplier",
    )
    quote_currency = _currency(
        valuation["quote_currency"],
        name=f"{symbol_text} valuation quote_currency",
    )
    settlement_currency = _currency(
        valuation["settlement_currency"],
        name=f"{symbol_text} valuation settlement_currency",
    )
    if asset_class not in _SUPPORTED_LINEAR_ASSET_CLASSES:
        raise AllocationValuationError(
            f"{symbol_text} asset class {asset_class} requires a canonical "
            "nonlinear payoff boundary before allocation"
        )
    if payoff != "LINEAR":
        raise AllocationValuationError(
            f"{symbol_text} payoff {payoff} requires its canonical payoff "
            "boundary and cannot use linear quantity*price allocation"
        )
    if settlement_currency != quote_currency:
        raise AllocationValuationError(
            f"{symbol_text} linear settlement currency differs from quote currency; "
            "an explicit settlement conversion boundary is required"
        )

    source_price_value = _positive(
        source_price,
        name=f"{symbol_text} source price",
    )
    if _positive(
        valuation.get("source_price"),
        name=f"{symbol_text} valuation source_price",
    ) != source_price_value:
        raise AllocationValuationError(
            f"{symbol_text} valuation source_price mismatch"
        )
    if _currency(
        valuation.get("portfolio_base_currency"),
        name=f"{symbol_text} valuation portfolio_base_currency",
    ) != base_currency:
        raise AllocationValuationError(
            f"{symbol_text} portfolio base currency mismatch"
        )

    try:
        source_unit_notional = linear_notional(
            signed_contracts=Decimal("1"),
            multiplier=multiplier,
            price=source_price_value,
        )
    except PerpetualError as error:
        raise AllocationValuationError(
            f"{symbol_text} canonical linear notional calculation failed"
        ) from error

    fx_quote_payload = valuation.get("fx_quote")
    fx_source_id = _text(
        valuation.get("fx_source_id"),
        name=f"{symbol_text} valuation fx_source_id",
    )
    expected_fx_rate = _positive(
        valuation.get("fx_rate"),
        name=f"{symbol_text} valuation fx_rate",
    )
    fx_evidence_sha256: str | None
    if quote_currency == base_currency:
        if fx_quote_payload not in (None, {}):
            raise AllocationValuationError(
                f"{symbol_text} identity FX conversion must not carry a quote"
            )
        if expected_fx_rate != Decimal("1") or fx_source_id != "IDENTITY":
            raise AllocationValuationError(
                f"{symbol_text} identity FX conversion must use rate 1 and IDENTITY source"
            )
        fx_evidence_sha256 = None
        converted = source_unit_notional
        rate_used = Decimal("1")
    else:
        quote_payload = _mapping(
            fx_quote_payload,
            name=f"{symbol_text} valuation fx_quote",
        )
        max_age_seconds = quote_payload.get("max_age_seconds")
        if (
            not isinstance(max_age_seconds, int)
            or isinstance(max_age_seconds, bool)
            or max_age_seconds <= 0
        ):
            raise AllocationValuationError(
                f"{symbol_text} fx max_age_seconds must be a positive integer"
            )
        haircut = _positive(
            quote_payload.get("haircut", "0"),
            name=f"{symbol_text} fx haircut",
            allow_zero=True,
        )
        if haircut != 0:
            raise AllocationValuationError(
                f"{symbol_text} exposure conversion cannot use a haircut that understates notional"
            )
        fx_evidence_sha256 = _sha256(
            quote_payload.get("evidence_sha256"),
            name=f"{symbol_text} fx evidence_sha256",
        )
        try:
            quote = FxQuote.create(
                base_currency=_currency(
                    quote_payload.get("base_currency"),
                    name=f"{symbol_text} fx base_currency",
                ),
                quote_currency=_currency(
                    quote_payload.get("quote_currency"),
                    name=f"{symbol_text} fx quote_currency",
                ),
                bid=_positive(
                    quote_payload.get("bid"),
                    name=f"{symbol_text} fx bid",
                ),
                ask=_positive(
                    quote_payload.get("ask"),
                    name=f"{symbol_text} fx ask",
                ),
                available_at=_instant(
                    quote_payload.get("available_at"),
                    name=f"{symbol_text} fx available_at",
                ),
                source_id=_text(
                    quote_payload.get("source_id"),
                    name=f"{symbol_text} fx source_id",
                ),
                evidence_sha256=fx_evidence_sha256,
            )
            fx_value = value_amount(
                source_unit_notional,
                source_currency=quote_currency,
                reporting_currency=base_currency,
                quote=quote,
                as_of=point,
                max_age=timedelta(seconds=max_age_seconds),
                haircut=Decimal("0"),
            )
        except FxValuationError as error:
            raise AllocationValuationError(
                f"{symbol_text} canonical FX valuation failed"
            ) from error
        if not fx_value.allocatable or fx_value.converted_amount is None:
            raise AllocationValuationError(
                f"{symbol_text} FX valuation is not allocatable: {fx_value.status}"
            )
        if fx_value.rate_used is None or fx_value.source_id is None:
            raise AllocationValuationError(
                f"{symbol_text} FX valuation lacks exact conversion identity"
            )
        rate_used = fx_value.rate_used
        converted = fx_value.converted_amount
        if expected_fx_rate != rate_used:
            raise AllocationValuationError(
                f"{symbol_text} valuation fx_rate does not match canonical FX conversion"
            )
        if fx_source_id != fx_value.source_id:
            raise AllocationValuationError(
                f"{symbol_text} valuation fx_source_id mismatch"
            )
        if _sha256(
            valuation.get("fx_evidence_sha256"),
            name=f"{symbol_text} valuation fx_evidence_sha256",
        ) != fx_evidence_sha256:
            raise AllocationValuationError(
                f"{symbol_text} valuation FX evidence digest mismatch"
            )

    unit_base_notional = abs(converted)
    if _positive(
        valuation.get("unit_base_notional"),
        name=f"{symbol_text} valuation unit_base_notional",
    ) != unit_base_notional:
        raise AllocationValuationError(
            f"{symbol_text} valuation unit_base_notional mismatch"
        )

    expected_capital_rate = _positive(
        expected_capital_requirement_rate,
        name=f"{symbol_text} expected capital_requirement_rate",
    )
    if _positive(
        valuation.get("capital_requirement_rate"),
        name=f"{symbol_text} valuation capital_requirement_rate",
    ) != expected_capital_rate:
        raise AllocationValuationError(
            f"{symbol_text} valuation capital_requirement_rate mismatch"
        )
    expected_min = _positive(
        expected_min_notional_base,
        name=f"{symbol_text} expected min_notional_base",
        allow_zero=True,
    )
    if _positive(
        valuation.get("min_notional_base"),
        name=f"{symbol_text} valuation min_notional_base",
        allow_zero=True,
    ) != expected_min:
        raise AllocationValuationError(
            f"{symbol_text} valuation min_notional_base mismatch"
        )
    expected_floor = _positive(
        expected_fee_floor_base,
        name=f"{symbol_text} expected fee_floor_base",
        allow_zero=True,
    )
    if _positive(
        valuation.get("fee_floor_base"),
        name=f"{symbol_text} valuation fee_floor_base",
        allow_zero=True,
    ) != expected_floor:
        raise AllocationValuationError(
            f"{symbol_text} valuation fee_floor_base mismatch"
        )
    _optional_decimal_equal(
        valuation.get("max_executable_notional_base"),
        (
            None
            if expected_max_executable_notional_base is None
            else _positive(
                expected_max_executable_notional_base,
                name=f"{symbol_text} expected max_executable_notional_base",
                allow_zero=True,
            )
        ),
        name=f"{symbol_text} valuation max_executable_notional_base",
    )

    components_raw = _mapping(
        valuation.get("cost_rate_components"),
        name=f"{symbol_text} valuation cost_rate_components",
    )
    refs_raw = _mapping(
        valuation.get("cost_evidence_refs"),
        name=f"{symbol_text} valuation cost_evidence_refs",
    )
    if set(components_raw) != set(_COST_COMPONENTS):
        raise AllocationValuationError(
            f"{symbol_text} cost_rate_components must explicitly cover "
            + ", ".join(_COST_COMPONENTS)
        )
    if set(refs_raw) != set(_COST_COMPONENTS):
        raise AllocationValuationError(
            f"{symbol_text} cost_evidence_refs must explicitly cover "
            + ", ".join(_COST_COMPONENTS)
        )
    components = {
        key: _positive(
            components_raw[key],
            name=f"{symbol_text} {key} cost rate",
            allow_zero=True,
        )
        for key in _COST_COMPONENTS
    }
    refs = {
        key: _text(
            refs_raw[key],
            name=f"{symbol_text} {key} cost evidence ref",
        )
        for key in _COST_COMPONENTS
    }
    total_cost_rate = sum(components.values(), Decimal("0"))
    if total_cost_rate != _positive(
        expected_cost_rate,
        name=f"{symbol_text} expected cost_rate",
        allow_zero=True,
    ):
        raise AllocationValuationError(
            f"{symbol_text} explicit cost components do not match candidate cost_rate"
        )

    payoff_identity = _text(
        valuation.get("payoff_identity"),
        name=f"{symbol_text} valuation payoff_identity",
    )
    return AllocationValuation(
        symbol=symbol_text,
        asset_class=asset_class,
        payoff=payoff,
        quantity_unit=quantity_unit,
        contract_multiplier=multiplier,
        quote_currency=quote_currency,
        settlement_currency=settlement_currency,
        portfolio_base_currency=base_currency,
        unit_base_notional=unit_base_notional,
        fx_rate=rate_used,
        fx_source_id=fx_source_id,
        fx_evidence_sha256=fx_evidence_sha256,
        payoff_identity=payoff_identity,
        cost_rate_components=components,
        cost_evidence_refs=refs,
    )
