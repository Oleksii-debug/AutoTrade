"""Exact futures lifecycle primitives.

Provider-neutral financial math only. This module does not authorize orders,
choose leverage or connect to a venue.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Literal

from .accounting import JournalTransaction, posting, validate_transaction


class FuturesError(ValueError):
    pass


def _decimal(value: Decimal | str | int, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FuturesError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise FuturesError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise FuturesError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise FuturesError(f"{name} must be positive")
    return result


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuturesError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FuturesError(f"{name} must be timezone-aware")
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
class FuturesContract:
    instrument: str
    payoff: Literal["LINEAR", "INVERSE"]
    multiplier: Decimal
    quote_currency: str
    settlement_currency: str
    last_trade_at: datetime
    delivery_cutoff: datetime
    expiry: datetime
    settlement_method: Literal["CASH", "PHYSICAL"]
    price_base_currency: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument"))
        if self.payoff not in {"LINEAR", "INVERSE"}:
            raise FuturesError("payoff must be LINEAR or INVERSE")
        object.__setattr__(self, "multiplier", _decimal(self.multiplier, "multiplier", positive=True))
        object.__setattr__(self, "quote_currency", _text(self.quote_currency, "quote_currency"))
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, "settlement_currency"),
        )
        for name in ("last_trade_at", "delivery_cutoff", "expiry"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if not self.last_trade_at <= self.expiry:
            raise FuturesError("last_trade_at cannot be after expiry")
        if not self.delivery_cutoff <= self.expiry:
            raise FuturesError("delivery_cutoff cannot be after expiry")
        if self.settlement_method not in {"CASH", "PHYSICAL"}:
            raise FuturesError("settlement_method must be CASH or PHYSICAL")
        if self.price_base_currency is not None:
            object.__setattr__(
                self,
                "price_base_currency",
                _text(self.price_base_currency, "price_base_currency"),
            )
        if self.payoff == "INVERSE":
            if self.price_base_currency is None:
                raise FuturesError(
                    "inverse futures require explicit price_base_currency qualification"
                )
            if self.settlement_currency != self.price_base_currency:
                raise FuturesError(
                    "inverse settlement_currency must match the base currency produced by face/price"
                )


@dataclass(frozen=True)
class InverseVariationMarginState:
    """Exact inverse-futures state between explicit settlement boundaries."""

    contract: FuturesContract
    signed_contracts: Decimal
    last_settlement_price: Decimal
    cumulative_variation_margin: Fraction = Fraction(0, 1)

    def __post_init__(self) -> None:
        if self.contract.payoff != "INVERSE":
            raise FuturesError("inverse variation-margin state requires INVERSE futures")
        contracts = _decimal(self.signed_contracts, "signed_contracts")
        if contracts == 0:
            raise FuturesError("signed_contracts must be non-zero")
        object.__setattr__(self, "signed_contracts", contracts)
        object.__setattr__(
            self,
            "last_settlement_price",
            _decimal(self.last_settlement_price, "last_settlement_price", positive=True),
        )
        if not isinstance(self.cumulative_variation_margin, Fraction):
            raise FuturesError("cumulative inverse variation margin must be an exact Fraction")


@dataclass(frozen=True)
class VariationMarginState:
    contract: FuturesContract
    signed_contracts: Decimal
    last_settlement_price: Decimal
    cumulative_variation_margin: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        contracts = _decimal(self.signed_contracts, "signed_contracts")
        if contracts == 0:
            raise FuturesError("signed_contracts must be non-zero")
        object.__setattr__(self, "signed_contracts", contracts)
        object.__setattr__(
            self,
            "last_settlement_price",
            _decimal(self.last_settlement_price, "last_settlement_price", positive=True),
        )
        object.__setattr__(
            self,
            "cumulative_variation_margin",
            _decimal(self.cumulative_variation_margin, "cumulative_variation_margin"),
        )
        if self.contract.payoff != "LINEAR":
            raise FuturesError("decimal variation-margin state currently supports LINEAR futures only")


def linear_futures_pnl(
    *,
    signed_contracts: Decimal | str | int,
    multiplier: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Decimal:
    contracts = _decimal(signed_contracts, "signed_contracts")
    contract_multiplier = _decimal(multiplier, "multiplier", positive=True)
    entry = _decimal(entry_price, "entry_price", positive=True)
    exit_value = _decimal(exit_price, "exit_price", positive=True)
    return contracts * contract_multiplier * (exit_value - entry)


def inverse_futures_pnl_exact(
    *,
    signed_contracts: Decimal | str | int,
    contract_quote_value: Decimal | str | int,
    entry_price: Decimal | str | int,
    exit_price: Decimal | str | int,
) -> Fraction:
    """Return exact price-base-currency P&L as a rational number.

    For a contract whose multiplier is a quote-currency face value:
    contracts * face * (1 / entry - 1 / exit).
    """

    contracts = _decimal(signed_contracts, "signed_contracts")
    face = _decimal(contract_quote_value, "contract_quote_value", positive=True)
    entry = _decimal(entry_price, "entry_price", positive=True)
    exit_value = _decimal(exit_price, "exit_price", positive=True)
    return (
        _fraction(contracts)
        * _fraction(face)
        * (Fraction(1, 1) / _fraction(entry) - Fraction(1, 1) / _fraction(exit_value))
    )


def settle_fraction(
    value: Fraction,
    *,
    quantum: Decimal | str,
    rounding: Literal["HALF_EVEN", "DOWN"] = "HALF_EVEN",
) -> Decimal:
    """Round an exact rational to an exact multiple of the settlement quantum."""

    if not isinstance(value, Fraction):
        raise FuturesError("value must be an exact Fraction")
    step = _decimal(quantum, "quantum", positive=True)
    if rounding not in {"HALF_EVEN", "DOWN"}:
        raise FuturesError("unsupported rounding policy")

    units = value / _fraction(step)
    sign = -1 if units < 0 else 1
    numerator = abs(units.numerator)
    denominator = units.denominator
    whole, remainder = divmod(numerator, denominator)

    if rounding == "HALF_EVEN":
        doubled = remainder * 2
        if doubled > denominator or (doubled == denominator and whole % 2 == 1):
            whole += 1

    signed_units = whole * sign
    return step * Decimal(signed_units)


def apply_variation_margin(
    state: VariationMarginState,
    settlement_price: Decimal | str | int,
) -> tuple[VariationMarginState, Decimal]:
    price = _decimal(settlement_price, "settlement_price", positive=True)
    amount = linear_futures_pnl(
        signed_contracts=state.signed_contracts,
        multiplier=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=price,
    )
    return (
        replace(
            state,
            last_settlement_price=price,
            cumulative_variation_margin=state.cumulative_variation_margin + amount,
        ),
        amount,
    )


def apply_inverse_variation_margin(
    state: InverseVariationMarginState,
    settlement_price: Decimal | str | int,
) -> tuple[InverseVariationMarginState, Fraction]:
    """Apply one inverse settlement step without premature decimal rounding."""

    price = _decimal(settlement_price, "settlement_price", positive=True)
    amount = inverse_futures_pnl_exact(
        signed_contracts=state.signed_contracts,
        contract_quote_value=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=price,
    )
    return (
        replace(
            state,
            last_settlement_price=price,
            cumulative_variation_margin=state.cumulative_variation_margin + amount,
        ),
        amount,
    )


def settle_and_book_inverse_variation_margin(
    *,
    transaction_id: str,
    cause_event_id: str,
    contract: FuturesContract,
    exact_amount: Fraction,
    settlement_quantum: Decimal | str,
    rounding: Literal["HALF_EVEN", "DOWN"] = "HALF_EVEN",
) -> tuple[Decimal, JournalTransaction | None]:
    """Round only at the explicit settlement boundary and book exact currency truth.

    A sub-quantum amount that rounds to zero creates no artificial zero posting.
    The caller retains the exact rational amount as evidence; the returned Decimal
    is the provider-facing cash settlement amount.
    """

    if not isinstance(contract, FuturesContract) or contract.payoff != "INVERSE":
        raise FuturesError("inverse settlement booking requires an INVERSE futures contract")
    settled = settle_fraction(
        exact_amount,
        quantum=settlement_quantum,
        rounding=rounding,
    )
    if settled == 0:
        return settled, None
    return (
        settled,
        book_variation_margin(
            transaction_id=transaction_id,
            cause_event_id=cause_event_id,
            settlement_currency=contract.settlement_currency,
            amount=settled,
        ),
    )


def unrealized_inverse_after_variation(
    state: InverseVariationMarginState,
    mark_price: Decimal | str | int,
) -> Fraction:
    """Return exact inverse mark P&L from the last settled price."""

    return inverse_futures_pnl_exact(
        signed_contracts=state.signed_contracts,
        contract_quote_value=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=mark_price,
    )


def unrealized_after_variation(
    state: VariationMarginState,
    mark_price: Decimal | str | int,
) -> Decimal:
    """Mark only from the last settled price, avoiding double counting."""

    return linear_futures_pnl(
        signed_contracts=state.signed_contracts,
        multiplier=state.contract.multiplier,
        entry_price=state.last_settlement_price,
        exit_price=mark_price,
    )


def book_variation_margin(
    *,
    transaction_id: str,
    cause_event_id: str,
    settlement_currency: str,
    amount: Decimal | str | int,
) -> JournalTransaction:
    value = _decimal(amount, "amount")
    if value == 0:
        raise FuturesError("variation margin posting must be non-zero")
    currency = _text(settlement_currency, "settlement_currency")
    transaction = JournalTransaction(
        transaction_id=_text(transaction_id, "transaction_id"),
        cause_event_id=_text(cause_event_id, "cause_event_id"),
        postings=(
            posting(f"CASH:{currency}", currency, value),
            posting(f"FUTURES_VARIATION_PNL:{currency}", currency, -value),
        ),
    )
    validate_transaction(transaction)
    return transaction


def lifecycle_gate(
    contract: FuturesContract,
    at: datetime,
    *,
    physical_delivery_authorized: bool = False,
) -> str:
    """Return a conservative lifecycle state for holding/trading the contract."""

    point = _utc(at, "at")
    if point >= contract.expiry:
        return "EXPIRED"
    if point >= contract.last_trade_at:
        return "TRADING_ENDED"
    if (
        contract.settlement_method == "PHYSICAL"
        and not physical_delivery_authorized
        and point >= contract.delivery_cutoff
    ):
        return "DELIVERY_BLOCKED"
    return "OPEN"


def require_open_for_new_exposure(
    contract: FuturesContract,
    at: datetime,
    *,
    physical_delivery_authorized: bool = False,
) -> None:
    state = lifecycle_gate(
        contract,
        at,
        physical_delivery_authorized=physical_delivery_authorized,
    )
    if state != "OPEN":
        raise FuturesError(f"new futures exposure is blocked: {state}")
