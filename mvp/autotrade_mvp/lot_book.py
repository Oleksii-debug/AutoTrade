"""Exact FIFO lot/basis projection for simulated accounting evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from .exact_decimal import (
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    exact_add,
    exact_multiply,
    exact_subtract,
    exact_sum,
    parse_bounded_exact_decimal,
    terminating_decimal,
)


_EXACT_ERROR = "lot-book exact arithmetic exceeds resource envelope"


def _decimal(value: Decimal | str | int | float) -> Decimal:
    if type(value) not in (Decimal, str, int):
        raise TypeError("Financial values must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError("Value must be a bounded exact decimal") from error


def _translate(operation, *args):
    try:
        return operation(*args)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _add(left: Decimal, right: Decimal) -> Decimal:
    return _translate(exact_add, left, right)


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    return _translate(exact_subtract, left, right)


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    return _translate(exact_multiply, left, right)


def _sum(values) -> Decimal:
    return _translate(exact_sum, values)


ExactAmount = Decimal | Fraction


def _fraction(value: Decimal) -> Fraction:
    try:
        return bounded_fraction(as_fraction(value))
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


def _fraction_divide(left: Fraction, right: Fraction) -> Fraction:
    if right == 0:
        raise ValueError("lot-book exact division by zero")
    try:
        return bounded_fraction(left / right)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error


def _fraction_sum(values) -> Fraction:
    total = Fraction(0, 1)
    for value in values:
        total = _fraction_add(total, value)
    return total


def _project_fraction(value: Fraction) -> ExactAmount:
    """Return Decimal when exact/terminating, otherwise retain exact Fraction."""

    try:
        bounded = bounded_fraction(value)
    except ExactDecimalError as error:
        raise ValueError(_EXACT_ERROR) from error
    try:
        return terminating_decimal(bounded)
    except ExactDecimalError:
        return bounded


@dataclass(frozen=True)
class Lot:
    quantity: Decimal
    total_basis: Fraction

    @property
    def unit_cost(self) -> ExactAmount:
        return _project_fraction(
            _fraction_divide(self.total_basis, _fraction(self.quantity))
        )

    @property
    def basis(self) -> ExactAmount:
        return _project_fraction(self.total_basis)


@dataclass(frozen=True)
class BasisSnapshot:
    position: Decimal
    open_basis: ExactAmount
    realized_pnl: ExactAmount


class FifoLotBook:
    """Tracks one instrument using exact FIFO quantity and rational lot basis.

    Fees are included in basis on buys and deducted from proceeds on sells.
    A non-terminating per-unit basis stays rational rather than being rounded.
    Short inventory is intentionally unsupported at this foundation stage.
    """

    def __init__(self) -> None:
        self._lots: list[Lot] = []
        self._realized = Fraction(0, 1)

    @property
    def lots(self) -> tuple[Lot, ...]:
        return tuple(self._lots)

    def snapshot(self) -> BasisSnapshot:
        position = _sum(lot.quantity for lot in self._lots)
        basis = _fraction_sum(lot.total_basis for lot in self._lots)
        return BasisSnapshot(
            position=position,
            open_basis=_project_fraction(basis),
            realized_pnl=_project_fraction(self._realized),
        )

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
        total_cost = _add(_multiply(qty, px), cost_fee)
        total_basis = _fraction(total_cost)
        self._lots.append(Lot(qty, total_basis))
        return self.snapshot()

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
            taken_ratio = _fraction_divide(
                _fraction(taken),
                _fraction(lot.quantity),
            )
            taken_basis = _fraction_multiply(lot.total_basis, taken_ratio)
            removed_basis = _fraction_add(removed_basis, taken_basis)
            leftover = _subtract(lot.quantity, taken)
            if leftover == 0:
                working_lots.pop(0)
            else:
                working_lots[0] = Lot(
                    leftover,
                    _fraction_subtract(lot.total_basis, taken_basis),
                )
            remaining = _subtract(remaining, taken)

        net_proceeds = _fraction(
            _subtract(_multiply(qty, px), sell_fee)
        )
        new_realized = _fraction_add(
            self._realized,
            _fraction_subtract(net_proceeds, removed_basis),
        )

        # Commit the derived state only after every exact calculation succeeds.
        self._lots = working_lots
        self._realized = new_realized
        return self.snapshot()

    def mark_to_market(
        self,
        price: Decimal | str | int | float,
    ) -> ExactAmount:
        px = _decimal(price)
        if px <= 0:
            raise ValueError("Mark price must be positive")
        state = self.snapshot()
        market_value = _fraction(_multiply(state.position, px))
        open_basis = _fraction_sum(lot.total_basis for lot in self._lots)
        return _project_fraction(
            _fraction_subtract(market_value, open_basis)
        )
