"""Exact perpetual-contract lifecycle primitives.

The module is provider-neutral and deliberately does not place orders. It models
funding, mark/index evidence, collateral conversion, and fail-closed margin
admission with explicit venue conventions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import json
from typing import Literal


class PerpetualError(ValueError):
    pass


def _decimal(value: Decimal | str | int, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise PerpetualError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerpetualError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise PerpetualError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise PerpetualError(f"{name} must be positive")
    return result


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerpetualError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PerpetualError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


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


@dataclass(frozen=True)
class PerpetualContract:
    instrument_id: str
    settlement_currency: str
    collateral_currency: str
    multiplier: Decimal
    payoff: Literal["LINEAR", "INVERSE"] = "LINEAR"
    face_currency: str | None = None
    price_quote_currency: str | None = None
    price_base_currency: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _text(self.instrument_id, "instrument_id"))
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, "settlement_currency"),
        )
        object.__setattr__(
            self,
            "collateral_currency",
            _text(self.collateral_currency, "collateral_currency"),
        )
        object.__setattr__(self, "multiplier", _decimal(self.multiplier, "multiplier", positive=True))
        if self.payoff not in {"LINEAR", "INVERSE"}:
            raise PerpetualError("payoff must be LINEAR or INVERSE")
        if self.face_currency is not None:
            object.__setattr__(
                self,
                "face_currency",
                _text(self.face_currency, "face_currency"),
            )
        if self.price_quote_currency is not None:
            object.__setattr__(
                self,
                "price_quote_currency",
                _text(self.price_quote_currency, "price_quote_currency"),
            )
        if self.price_base_currency is not None:
            object.__setattr__(
                self,
                "price_base_currency",
                _text(self.price_base_currency, "price_base_currency"),
            )


def _require_inverse_units(contract: PerpetualContract) -> None:
    if contract.payoff != "INVERSE":
        raise PerpetualError("inverse economics require an INVERSE contract")
    if contract.face_currency is None:
        raise PerpetualError("inverse contract requires explicit face_currency qualification")
    if contract.price_quote_currency is None:
        raise PerpetualError(
            "inverse contract requires explicit price_quote_currency qualification"
        )
    if contract.face_currency != contract.price_quote_currency:
        raise PerpetualError(
            "inverse face_currency must match the currency of quoted prices"
        )
    if contract.price_base_currency is None:
        raise PerpetualError(
            "inverse contract requires explicit price_base_currency qualification"
        )
    if contract.settlement_currency != contract.price_base_currency:
        raise PerpetualError(
            "inverse settlement_currency must match the base currency produced by face/price"
        )


@dataclass(frozen=True)
class FundingConvention:
    positive_rate_effect: Literal["LONG_PAYS", "LONG_RECEIVES"]
    price_basis: Literal["MARK", "INDEX"]

    def __post_init__(self) -> None:
        if self.positive_rate_effect not in {"LONG_PAYS", "LONG_RECEIVES"}:
            raise PerpetualError("positive_rate_effect must be explicit")
        if self.price_basis not in {"MARK", "INDEX"}:
            raise PerpetualError("price_basis must be MARK or INDEX")


@dataclass(frozen=True)
class MarketSnapshot:
    mark_price: Decimal
    index_price: Decimal
    observed_at: datetime
    max_age: timedelta
    max_mark_index_deviation: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "mark_price", _decimal(self.mark_price, "mark_price", positive=True))
        object.__setattr__(self, "index_price", _decimal(self.index_price, "index_price", positive=True))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise PerpetualError("max_age must be positive")
        deviation = _decimal(
            self.max_mark_index_deviation,
            "max_mark_index_deviation",
            positive=True,
        )
        object.__setattr__(self, "max_mark_index_deviation", deviation)

    def require_valid(self, at: datetime) -> None:
        point = _utc(at, "at")
        if point < self.observed_at:
            raise PerpetualError("market snapshot cannot come from the future")
        if point - self.observed_at > self.max_age:
            raise PerpetualError("market snapshot is stale")
        relative = abs(self.mark_price - self.index_price) / self.index_price
        if relative > self.max_mark_index_deviation:
            raise PerpetualError("mark/index deviation exceeds configured bound")


@dataclass(frozen=True)
class CollateralQuote:
    from_currency: str
    to_currency: str
    rate: Decimal
    observed_at: datetime
    max_age: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_currency", _text(self.from_currency, "from_currency"))
        object.__setattr__(self, "to_currency", _text(self.to_currency, "to_currency"))
        if self.from_currency == self.to_currency:
            raise PerpetualError("collateral conversion currencies must differ")
        object.__setattr__(self, "rate", _decimal(self.rate, "rate", positive=True))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise PerpetualError("max_age must be positive")

    def convert(self, amount: Decimal | str | int, at: datetime) -> Decimal:
        point = _utc(at, "at")
        if point < self.observed_at:
            raise PerpetualError("collateral quote cannot come from the future")
        if point - self.observed_at > self.max_age:
            raise PerpetualError("collateral quote is stale")
        return _decimal(amount, "amount") * self.rate


@dataclass(frozen=True)
class MarginSnapshot:
    equity: Decimal
    maintenance_requirement: Decimal
    observed_at: datetime
    max_age: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "equity", _decimal(self.equity, "equity"))
        object.__setattr__(
            self,
            "maintenance_requirement",
            _decimal(self.maintenance_requirement, "maintenance_requirement", positive=True),
        )
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise PerpetualError("max_age must be positive")

    def require_fresh(self, at: datetime) -> None:
        point = _utc(at, "at")
        if point < self.observed_at:
            raise PerpetualError("margin snapshot cannot come from the future")
        if point - self.observed_at > self.max_age:
            raise PerpetualError("margin snapshot is stale")


@dataclass(frozen=True)
class LiquidationSnapshot:
    """Provider-qualified liquidation boundary for one current margin tier."""

    side: Literal["LONG", "SHORT"]
    liquidation_price: Decimal
    tier_id: str
    evidence_ref: str
    observed_at: datetime
    max_age: timedelta

    def __post_init__(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise PerpetualError("side must be LONG or SHORT")
        object.__setattr__(
            self,
            "liquidation_price",
            _decimal(self.liquidation_price, "liquidation_price", positive=True),
        )
        object.__setattr__(self, "tier_id", _text(self.tier_id, "tier_id"))
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise PerpetualError("max_age must be positive")

    def require_fresh(self, at: datetime) -> None:
        point = _utc(at, "at")
        if point < self.observed_at:
            raise PerpetualError("liquidation snapshot cannot come from the future")
        if point - self.observed_at > self.max_age:
            raise PerpetualError("liquidation snapshot is stale")

    def headroom_fraction(self, mark_price: Decimal | str | int) -> Decimal:
        mark = _decimal(mark_price, "mark_price", positive=True)
        if self.side == "LONG":
            if self.liquidation_price >= mark:
                raise PerpetualError("long liquidation boundary must be below current mark")
            return (mark - self.liquidation_price) / mark
        if self.liquidation_price <= mark:
            raise PerpetualError("short liquidation boundary must be above current mark")
        return (self.liquidation_price - mark) / mark


def require_liquidation_headroom(
    *,
    liquidation: LiquidationSnapshot,
    market: MarketSnapshot,
    minimum_headroom_fraction: Decimal | str | int,
    at: datetime,
) -> Decimal:
    """Require fresh provider-tier evidence and a bounded liquidation buffer."""

    if not isinstance(liquidation, LiquidationSnapshot):
        raise TypeError("liquidation must be LiquidationSnapshot")
    if not isinstance(market, MarketSnapshot):
        raise TypeError("market must be MarketSnapshot")
    liquidation.require_fresh(at)
    market.require_valid(at)
    minimum = _decimal(minimum_headroom_fraction, "minimum_headroom_fraction")
    if minimum < 0:
        raise PerpetualError("minimum_headroom_fraction cannot be negative")
    headroom = liquidation.headroom_fraction(market.mark_price)
    if headroom < minimum:
        raise PerpetualError("liquidation headroom is below configured minimum")
    return headroom


def linear_notional(
    *,
    signed_contracts: Decimal | str | int,
    multiplier: Decimal | str | int,
    price: Decimal | str | int,
) -> Decimal:
    contracts = _decimal(signed_contracts, "signed_contracts")
    contract_multiplier = _decimal(multiplier, "multiplier", positive=True)
    mark = _decimal(price, "price", positive=True)
    return contracts * contract_multiplier * mark


def inverse_perpetual_pnl_exact(
    *,
    contract: PerpetualContract,
    signed_contracts: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Fraction:
    """Return exact inverse-contract P&L in settlement units.

    For inverse contracts the multiplier is an amount of face_currency per
    contract. Explicit face_currency evidence is required so a linear
    multiplier cannot silently be interpreted as inverse face value.
    """

    _require_inverse_units(contract)
    contracts = _decimal(signed_contracts, "signed_contracts")
    face = _decimal(contract.multiplier, "multiplier", positive=True)
    entry = _decimal(entry_price, "entry_price", positive=True)
    exit_value = _decimal(exit_price, "exit_price", positive=True)
    return (
        _fraction(contracts)
        * _fraction(face)
        * (Fraction(1, 1) / _fraction(entry) - Fraction(1, 1) / _fraction(exit_value))
    )


def inverse_funding_cashflow_exact(
    *,
    contract: PerpetualContract,
    signed_contracts: Decimal | str | int,
    funding_rate: Decimal | str | int,
    snapshot: MarketSnapshot,
    convention: FundingConvention,
    at: datetime,
) -> tuple[str, Fraction]:
    """Return exact inverse funding cashflow in settlement currency."""

    _require_inverse_units(contract)
    snapshot.require_valid(at)
    contracts = _decimal(signed_contracts, "signed_contracts")
    rate = _decimal(funding_rate, "funding_rate")
    basis = snapshot.mark_price if convention.price_basis == "MARK" else snapshot.index_price
    position_value = (
        _fraction(contracts)
        * _fraction(contract.multiplier)
        / _fraction(basis)
    )
    raw = position_value * _fraction(rate)
    cashflow = -raw if convention.positive_rate_effect == "LONG_PAYS" else raw
    return contract.settlement_currency, cashflow


def inverse_stressed_loss_exact(
    *,
    contract: PerpetualContract,
    signed_contracts: Decimal | str | int,
    mark_price: Decimal | str | int,
    adverse_move_fraction: Decimal | str | int,
) -> Fraction:
    """Return exact positive loss for an adverse inverse-contract price move."""

    _require_inverse_units(contract)
    contracts = _decimal(signed_contracts, "signed_contracts")
    if contracts == 0:
        return Fraction(0, 1)
    mark = _decimal(mark_price, "mark_price", positive=True)
    move = _decimal(adverse_move_fraction, "adverse_move_fraction", positive=True)
    if move >= 1:
        raise PerpetualError("adverse_move_fraction must be below one")
    exit_price = mark * (Decimal("1") - move if contracts > 0 else Decimal("1") + move)
    pnl = inverse_perpetual_pnl_exact(
        contract=contract,
        signed_contracts=contracts,
        entry_price=mark,
        exit_price=exit_price,
    )
    return -pnl if pnl < 0 else Fraction(0, 1)


def funding_cashflow(
    *,
    contract: PerpetualContract,
    signed_contracts: Decimal | str | int,
    funding_rate: Decimal | str | int,
    snapshot: MarketSnapshot,
    convention: FundingConvention,
    at: datetime,
) -> tuple[str, Decimal]:
    """Return account cashflow in settlement currency; positive means receipt."""

    if contract.payoff != "LINEAR":
        raise PerpetualError("inverse funding requires provider-specific unit qualification")
    snapshot.require_valid(at)
    rate = _decimal(funding_rate, "funding_rate")
    basis = snapshot.mark_price if convention.price_basis == "MARK" else snapshot.index_price
    notional = linear_notional(
        signed_contracts=signed_contracts,
        multiplier=contract.multiplier,
        price=basis,
    )
    raw = notional * rate
    cashflow = -raw if convention.positive_rate_effect == "LONG_PAYS" else raw
    return contract.settlement_currency, cashflow


def stressed_loss(
    *,
    contract: PerpetualContract,
    signed_contracts: Decimal | str | int,
    mark_price: Decimal | str | int,
    adverse_move_fraction: Decimal | str | int,
) -> Decimal:
    if contract.payoff != "LINEAR":
        raise PerpetualError("inverse stress requires provider-specific unit qualification")
    position = _decimal(signed_contracts, "signed_contracts")
    mark = _decimal(mark_price, "mark_price", positive=True)
    move = _decimal(adverse_move_fraction, "adverse_move_fraction", positive=True)
    if move >= 1:
        raise PerpetualError("adverse_move_fraction must be below one")
    return abs(position) * contract.multiplier * mark * move


def require_new_risk_capacity(
    *,
    margin: MarginSnapshot,
    market: MarketSnapshot,
    stressed_position_loss: Decimal | str | int,
    reserve_buffer: Decimal | str | int,
    at: datetime,
) -> None:
    """Fail closed if fresh equity cannot cover maintenance plus stress and buffer."""

    margin.require_fresh(at)
    market.require_valid(at)
    loss = _decimal(stressed_position_loss, "stressed_position_loss")
    if loss < 0:
        raise PerpetualError("stressed_position_loss cannot be negative")
    buffer = _decimal(reserve_buffer, "reserve_buffer")
    if buffer < 0:
        raise PerpetualError("reserve_buffer cannot be negative")
    required = margin.maintenance_requirement + loss + buffer
    if margin.equity <= required:
        raise PerpetualError("insufficient fresh margin for new risk")


class FundingLedger:
    """Idempotent funding-event accumulator with conflict detection."""

    def __init__(self) -> None:
        self._events: dict[str, tuple[str, str, Decimal]] = {}
        self._balances: dict[str, Decimal] = {}

    @staticmethod
    def _fingerprint(instrument_id: str, currency: str, amount: Decimal) -> str:
        payload = json.dumps(
            {
                "instrument_id": instrument_id,
                "currency": currency,
                "amount": format(amount, "f"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    def apply(
        self,
        *,
        event_id: str,
        instrument_id: str,
        currency: str,
        amount: Decimal | str | int,
    ) -> Decimal:
        identifier = _text(event_id, "event_id")
        instrument = _text(instrument_id, "instrument_id")
        unit = _text(currency, "currency")
        value = _decimal(amount, "amount")
        fingerprint = self._fingerprint(instrument, unit, value)

        existing = self._events.get(identifier)
        if existing is not None:
            old_fingerprint, old_currency, old_amount = existing
            if old_fingerprint != fingerprint:
                raise PerpetualError("funding event id was reused with different content")
            return old_amount

        self._events[identifier] = (fingerprint, unit, value)
        self._balances[unit] = self._balances.get(unit, Decimal("0")) + value
        return value

    def balance(self, currency: str) -> Decimal:
        return self._balances.get(_text(currency, "currency"), Decimal("0"))
