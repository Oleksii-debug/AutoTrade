"""Exact FIFO lot/basis projection for simulated accounting evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _decimal(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("Financial values must use Decimal, string or integer input")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Value must be a valid decimal") from error
    if not amount.is_finite():
        raise ValueError("Value must be finite")
    return amount


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
        position = sum((lot.quantity for lot in self._lots), Decimal("0"))
        basis = sum((lot.quantity * lot.unit_cost for lot in self._lots), Decimal("0"))
        return BasisSnapshot(position=position, open_basis=basis, realized_pnl=self._realized)

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
        unit_cost = (qty * px + cost_fee) / qty
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
        while remaining > 0:
            lot = self._lots[0]
            taken = min(remaining, lot.quantity)
            removed_basis += taken * lot.unit_cost
            leftover = lot.quantity - taken
            if leftover == 0:
                self._lots.pop(0)
            else:
                self._lots[0] = Lot(leftover, lot.unit_cost)
            remaining -= taken

        net_proceeds = qty * px - sell_fee
        self._realized += net_proceeds - removed_basis
        return self.snapshot()

    def mark_to_market(
        self,
        price: Decimal | str | int | float,
    ) -> Decimal:
        px = _decimal(price)
        if px <= 0:
            raise ValueError("Mark price must be positive")
        state = self.snapshot()
        return state.position * px - state.open_basis
