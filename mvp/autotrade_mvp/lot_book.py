"""Exact FIFO lot/basis projection for simulated accounting evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from .exact_decimal import (
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    exact_multiply,
    exact_subtract,
    exact_sum,
    parse_bounded_exact_decimal,
    terminating_decimal,
)


_EXACT_ERROR = "lot-book exact arithmetic exceeds resource envelope"
ExactBasis = Decimal | Fraction


def _decimal(value: Decimal | str | int | float) -> Decimal:
    if type(value) not in (Decimal, str, int):
        raise TypeError("Financial values must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError("Value must be a bounded exact decimal") from error


def _fraction(value: Decimal | Fraction | int) -> Fraction:
    try:
        if type(value) is Fraction:
            return bounded_fraction(value)
        if type(value) is Decimal:
            return bounded_fraction(as_fraction(value))
        if type(value) is int:
            return bounded_fraction(Fraction(value, 1))
        raise ExactDecimalError("unsupported exact lot-book scalar")
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _fraction_add(left: Fraction, right: Fraction) -> Fraction:
    try:
        return bounded_fraction(left + right)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _fraction_subtract(left: Fraction, right: Fraction) -> Fraction:
    try:
        return bounded_fraction(left - right)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _fraction_multiply(left: Fraction, right: Fraction) -> Fraction:
    try:
        return bounded_fraction(left * right)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _exact_decimal_multiply(left: Decimal, right: Decimal) -> Decimal:
    try:
        return exact_multiply(left, right)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _exact_decimal_subtract(left: Decimal, right: Decimal) -> Decimal:
    try:
        return exact_subtract(left, right)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _decimal_sum(values) -> Decimal:
    try:
        return exact_sum(values)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _public_exact(value: Fraction) -> ExactBasis:
    """Prefer Decimal for backwards-compatible terminating values, else Fraction."""
    value = _fraction(value)
    try:
        return terminating_decimal(value)
    except ExactDecimalError:
        return value


@dataclass(frozen=True)
class Lot:
    quantity: Decimal
    total_basis: Fraction

    @property
    def unit_cost(self) -> ExactBasis:
        ratio = _fraction_multiply(
            self.total_basis,
            _fraction(Fraction(1, 1) / as_fraction(self.quantity)),
        )
        return _public_exact(ratio)


@dataclass(frozen=True)
class BasisSnapshot:
    position: Decimal
    open_basis: ExactBasis
    realized_pnl: ExactBasis


class FifoLotBook:
    """Tracks one instrument using exact FIFO lots.

    Authoritative lot basis is retained as a bounded rational total, not a
    rounded per-unit Decimal. Terminating public values remain Decimals for
    compatibility; non-terminating exact values are exposed as Fractions.
    Fees are included in basis on buys and deducted from proceeds on sells.
    Short inventory is intentionally unsupported at this foundation stage.
    """

    def __init__(self) -> None:
        self._lots: list[Lot] = []
        self._realized = Fraction(0, 1)

    @property
    def lots(self) -> tuple[Lot, ...]:
        return tuple(self._lots)

    def _open_basis_fraction(self) -> Fraction:
        total = Fraction(0, 1)
        for lot in self._lots:
            total = _fraction_add(total, lot.total_basis)
        return total

    @staticmethod
    def _snapshot_for(
        lots: list[Lot],
        realized: Fraction,
    ) -> BasisSnapshot:
        position = _decimal_sum(lot.quantity for lot in lots)
        basis = Fraction(0, 1)
        for lot in lots:
            basis = _fraction_add(basis, lot.total_basis)
        return BasisSnapshot(
            position=position,
            open_basis=_public_exact(basis),
            realized_pnl=_public_exact(realized),
        )

    def snapshot(self) -> BasisSnapshot:
        return self._snapshot_for(self._lots, self._realized)

    def buy(
        self,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        *,
        fee: Decimal | str | int | float = 0,
    ) -> BasisSnapshot:
        qty = _decimal(quantity)
        px = _decimal(price)
        cost_fee = _decimal(fee)
        if qty <= 0 or px <= 0 or cost_fee < 0:
            raise ValueError("Buy quantity and price must be positive and fee non-negative")
        total_cost = _fraction_add(
            _fraction(_exact_decimal_multiply(qty, px)),
            _fraction(cost_fee),
        )
        candidate = Lot(qty, total_cost)
        candidate_lots = [*self._lots, candidate]
        next_snapshot = self._snapshot_for(candidate_lots, self._realized)
        self._lots = candidate_lots
        return next_snapshot

    def sell(
        self,
        quantity: Decimal | str | int | float,
        price: Decimal | str | int | float,
        *,
        fee: Decimal | str | int | float = 0,
    ) -> BasisSnapshot:
        qty = _decimal(quantity)
        px = _decimal(price)
        sell_fee = _decimal(fee)
        if qty <= 0 or px <= 0 or sell_fee < 0:
            raise ValueError("Sell quantity and price must be positive and fee non-negative")

        available = self.snapshot().position
        if qty > available:
            raise ValueError("Cannot sell more than the available long position")

        remaining = qty
        removed_basis = Fraction(0, 1)
        working_lots = list(self._lots)
        while remaining > 0:
            lot = working_lots[0]
            taken = min(remaining, lot.quantity)
            if taken == lot.quantity:
                taken_basis = lot.total_basis
                working_lots.pop(0)
            else:
                share = _fraction_multiply(
                    _fraction(taken),
                    _fraction(Fraction(1, 1) / as_fraction(lot.quantity)),
                )
                taken_basis = _fraction_multiply(lot.total_basis, share)
                leftover_quantity = _exact_decimal_subtract(lot.quantity, taken)
                leftover_basis = _fraction_subtract(lot.total_basis, taken_basis)
                working_lots[0] = Lot(leftover_quantity, leftover_basis)
            removed_basis = _fraction_add(removed_basis, taken_basis)
            remaining = _exact_decimal_subtract(remaining, taken)

        net_proceeds = _fraction_subtract(
            _fraction(_exact_decimal_multiply(qty, px)),
            _fraction(sell_fee),
        )
        new_realized = _fraction_add(
            self._realized,
            _fraction_subtract(net_proceeds, removed_basis),
        )

        next_snapshot = self._snapshot_for(working_lots, new_realized)
        self._lots = working_lots
        self._realized = new_realized
        return next_snapshot

    def mark_to_market(
        self,
        price: Decimal | str | int | float,
    ) -> ExactBasis:
        px = _decimal(price)
        if px <= 0:
            raise ValueError("Mark price must be positive")
        market_value = _fraction(_exact_decimal_multiply(self.snapshot().position, px))
        return _public_exact(
            _fraction_subtract(market_value, self._open_basis_fraction())
        )
