"""Exact perpetual-contract lifecycle primitives.

The module is provider-neutral and deliberately does not place orders. It models
funding, mark/index evidence, collateral conversion, and fail-closed margin
admission with explicit venue conventions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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


@dataclass(frozen=True)
class PerpetualContract:
    instrument_id: str
    settlement_currency: str
    collateral_currency: str
    multiplier: Decimal
    payoff: Literal["LINEAR", "INVERSE"] = "LINEAR"

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
        self._events: dict[str, tuple[str, str, str, Decimal]] = {}
        self._periods: dict[tuple[str, str], tuple[str, str, Decimal]] = {}
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
        funding_period_id: str,
        instrument_id: str,
        currency: str,
        amount: Decimal | str | int,
    ) -> Decimal:
        identifier = _text(event_id, "event_id")
        period = _text(funding_period_id, "funding_period_id")
        instrument = _text(instrument_id, "instrument_id")
        unit = _text(currency, "currency")
        value = _decimal(amount, "amount")
        fingerprint = self._fingerprint(instrument, unit, value)

        existing = self._events.get(identifier)
        if existing is not None:
            old_fingerprint, old_period, old_currency, old_amount = existing
            if old_fingerprint != fingerprint or old_period != period:
                raise PerpetualError("funding event id was reused with different content")
            return old_amount

        period_key = (instrument, period)
        existing_period = self._periods.get(period_key)
        if existing_period is not None:
            old_fingerprint, old_currency, old_amount = existing_period
            if old_fingerprint != fingerprint:
                raise PerpetualError(
                    "funding period was reused with different economic content"
                )
            self._events[identifier] = (fingerprint, period, unit, value)
            return old_amount

        self._events[identifier] = (fingerprint, period, unit, value)
        self._periods[period_key] = (fingerprint, unit, value)
        self._balances[unit] = self._balances.get(unit, Decimal("0")) + value
        return value

    def balance(self, currency: str) -> Decimal:
        return self._balances.get(_text(currency, "currency"), Decimal("0"))
