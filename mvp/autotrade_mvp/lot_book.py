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


def _exact_unit_cost(total_cost: Decimal, quantity: Decimal) -> Decimal:
    """Project exact lot cost per unit only when its Decimal form terminates.

    Lot.unit_cost is part of the existing Decimal public surface. A rational
    unit basis such as 301/3 cannot be represented exactly there, so the
    foundation fails closed instead of silently accepting ambient-context
    rounding as financial truth.
    """

    try:
        ratio = bounded_fraction(as_fraction(total_cost) / as_fraction(quantity))
        return terminating_decimal(ratio)
    except ExactDecimalError as error:
        raise ValueError(
            "Lot unit cost is non-terminating or exceeds exact resource envelope"
        ) from error


@dataclass(frozen=True)
class Lot:
    quantity: Decimal
    unit_cost: Decimal


@dataclass(frozen=True)
class BasisSnapshot:
    position: Decimal
    open_basis: Decimal
    realized_pnl: Decimal


class FifoLotBook:
    """Tracks one instrument using exact Decimal FIFO lots.

    Fees are included in basis on buys and deducted from proceeds on sells.
    Short inventory is intentionally unsupported at this foundation stage.
    """

    def __init__(self) -> None:
        self._lots: list[Lot] = []
        self._realized = Decimal("0")

    @property
    def lots(self) -> tuple[Lot, ...]:
        return tuple(self._lots)

    def snapshot(self) -> BasisSnapshot:
        position = _sum(lot.quantity for lot in self._lots)
        basis = _sum(_multiply(lot.quantity, lot.unit_cost) for lot in self._lots)
        return BasisSnapshot(
            position=position,
            open_basis=basis,
            realized_pnl=self._realized,
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
        unit_cost = _exact_unit_cost(total_cost, qty)
        self._lots.append(Lot(qty, unit_cost))
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
        removed_basis = Decimal("0")
        working_lots = list(self._lots)
        while remaining > 0:
            lot = working_lots[0]
            taken = min(remaining, lot.quantity)
            removed_basis = _add(
                removed_basis,
                _multiply(taken, lot.unit_cost),
            )
            leftover = _subtract(lot.quantity, taken)
            if leftover == 0:
                working_lots.pop(0)
            else:
                working_lots[0] = Lot(leftover, lot.unit_cost)
            remaining = _subtract(remaining, taken)

        net_proceeds = _subtract(_multiply(qty, px), sell_fee)
        new_realized = _add(
            self._realized,
            _subtract(net_proceeds, removed_basis),
        )

        # Commit the derived state only after every exact calculation succeeds.
        self._lots = working_lots
        self._realized = new_realized
        return self.snapshot()

    def mark_to_market(
        self,
        price: Decimal | str | int | float,
    ) -> Decimal:
        px = _decimal(price)
        if px <= 0:
            raise ValueError("Mark price must be positive")
        state = self.snapshot()
        return _subtract(
            _multiply(state.position, px),
            state.open_basis,
        )
