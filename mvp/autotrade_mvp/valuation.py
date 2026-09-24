"""Exact lot, settlement and conservative valuation projections.

Trading P&L is intentionally distinct from tax reporting. Fees are separately expensed
by the economic journal and therefore are not silently capitalized into lot basis here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping


class ValuationUncertain(ValueError):
    """Raised when a mark or FX conversion is missing/stale/invalid."""


def _dec(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value, *, name: str) -> Decimal:
    result = _dec(value, name=name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _instant(value: str, *, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be ISO-8601") from error
    if result.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class Lot:
    lot_id: str
    quantity: Decimal
    unit_basis: Decimal
    opened_at: str

    def __post_init__(self) -> None:
        if not self.lot_id.strip():
            raise ValueError("lot_id is required")
        if _dec(self.quantity, name="quantity") == 0:
            raise ValueError("lot quantity cannot be zero")
        if _positive(self.unit_basis, name="unit_basis") <= 0:
            raise AssertionError("unreachable")
        _instant(self.opened_at, name="opened_at")


@dataclass(frozen=True)
class FillProjection:
    fill_id: str
    side: str
    quantity: Decimal
    price: Decimal
    realized_gross_pnl: Decimal
    position_after: Decimal
    opened_lot_id: str | None


class InventoryBook:
    """FIFO economic basis projection supporting long and short inventory."""

    def __init__(self) -> None:
        self._lots: list[Lot] = []
        self._fills: dict[str, tuple] = {}
        self.realized_gross_pnl = Decimal("0")

    @property
    def lots(self) -> tuple[Lot, ...]:
        return tuple(self._lots)

    @property
    def position_quantity(self) -> Decimal:
        return sum((lot.quantity for lot in self._lots), Decimal("0"))

    @property
    def gross_open_basis(self) -> Decimal:
        return sum((abs(lot.quantity) * lot.unit_basis for lot in self._lots), Decimal("0"))

    def apply_fill(self, *, fill_id: str, side: str, quantity, price, trade_time: str) -> FillProjection:
        if not isinstance(fill_id, str) or not fill_id.strip():
            raise ValueError("fill_id is required")
        normalized_side = str(side).upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        qty = _positive(quantity, name="quantity")
        px = _positive(price, name="price")
        when = _instant(trade_time, name="trade_time")
        identity = (normalized_side, qty, px, when.isoformat())
        previous = self._fills.get(fill_id)
        if previous is not None:
            if previous[:4] != identity:
                raise ValueError("fill identity conflict")
            return previous[4]

        incoming_sign = Decimal("1") if normalized_side == "BUY" else Decimal("-1")
        remaining = qty
        realized = Decimal("0")
        rebuilt: list[Lot] = []

        for lot in self._lots:
            if remaining == 0 or (lot.quantity > 0) == (incoming_sign > 0):
                rebuilt.append(lot)
                continue
            close_qty = min(abs(lot.quantity), remaining)
            if lot.quantity > 0 and incoming_sign < 0:
                realized += (px - lot.unit_basis) * close_qty
                new_qty = lot.quantity - close_qty
            else:
                realized += (lot.unit_basis - px) * close_qty
                new_qty = lot.quantity + close_qty
            remaining -= close_qty
            if new_qty != 0:
                rebuilt.append(replace(lot, quantity=new_qty))

        opened_lot_id = None
        if remaining > 0:
            opened_lot_id = f"{fill_id}:open"
            rebuilt.append(
                Lot(
                    lot_id=opened_lot_id,
                    quantity=incoming_sign * remaining,
                    unit_basis=px,
                    opened_at=trade_time,
                )
            )

        # Matching guarantees residual lots cannot contain both long and short signs.
        signs = {lot.quantity > 0 for lot in rebuilt}
        if len(signs) > 1:
            raise AssertionError("mixed residual lot signs violate net FIFO projection")
        self._lots = rebuilt
        self.realized_gross_pnl += realized
        result = FillProjection(
            fill_id=fill_id,
            side=normalized_side,
            quantity=qty,
            price=px,
            realized_gross_pnl=realized,
            position_after=self.position_quantity,
            opened_lot_id=opened_lot_id,
        )
        self._fills[fill_id] = identity + (result,)
        return result

    def apply_split(self, factor, *, effective_at: str) -> None:
        ratio = _positive(factor, name="split factor")
        _instant(effective_at, name="effective_at")
        self._lots = [
            replace(
                lot,
                quantity=lot.quantity * ratio,
                unit_basis=lot.unit_basis / ratio,
            )
            for lot in self._lots
        ]

    def unrealized_gross_pnl(self, mark) -> Decimal:
        px = _positive(mark, name="mark")
        total = Decimal("0")
        for lot in self._lots:
            if lot.quantity > 0:
                total += (px - lot.unit_basis) * lot.quantity
            else:
                total += (lot.unit_basis - px) * abs(lot.quantity)
        return total


@dataclass(frozen=True)
class SettlementBalances:
    settled: Mapping[str, Decimal]
    unsettled: Mapping[str, Decimal]


class SettlementBook:
    def __init__(self) -> None:
        self._settled: dict[str, Decimal] = {}
        self._unsettled: dict[str, Decimal] = {}

    def post(self, currency: str, amount, *, bucket: str) -> None:
        if not isinstance(currency, str) or not currency.strip():
            raise ValueError("currency is required")
        value = _dec(amount, name="amount")
        normalized = bucket.upper()
        target = self._settled if normalized == "SETTLED" else self._unsettled if normalized == "UNSETTLED" else None
        if target is None:
            raise ValueError("bucket must be SETTLED or UNSETTLED")
        target[currency] = target.get(currency, Decimal("0")) + value

    def settle(self, currency: str, amount) -> None:
        value = _positive(amount, name="amount")
        available = self._unsettled.get(currency, Decimal("0"))
        if available < value:
            raise ValueError("cannot settle more positive cash than available unsettled cash")
        self._unsettled[currency] = available - value
        self._settled[currency] = self._settled.get(currency, Decimal("0")) + value

    def snapshot(self) -> SettlementBalances:
        return SettlementBalances(
            settled=MappingProxyType(dict(self._settled)),
            unsettled=MappingProxyType(dict(self._unsettled)),
        )


@dataclass(frozen=True)
class TradableMark:
    instrument_id: str
    bid: Decimal | str | int
    ask: Decimal | str | int
    as_of: str
    source_id: str

    def validate(self, *, now: str, max_age_seconds: int) -> tuple[Decimal, Decimal]:
        bid = _positive(self.bid, name="bid")
        ask = _positive(self.ask, name="ask")
        if ask < bid:
            raise ValuationUncertain("crossed mark")
        if not self.source_id.strip():
            raise ValuationUncertain("mark source is required")
        age = (_instant(now, name="now") - _instant(self.as_of, name="as_of")).total_seconds()
        if age < 0:
            raise ValuationUncertain("mark is from the future")
        if age > max_age_seconds:
            raise ValuationUncertain("mark is stale")
        return bid, ask


@dataclass(frozen=True)
class FxQuote:
    base_currency: str
    quote_currency: str
    bid: Decimal | str | int
    ask: Decimal | str | int
    as_of: str
    source_id: str

    def convert(self, amount, *, now: str, max_age_seconds: int) -> Decimal:
        value = _dec(amount, name="amount")
        bid = _positive(self.bid, name="fx bid")
        ask = _positive(self.ask, name="fx ask")
        if ask < bid:
            raise ValuationUncertain("crossed FX quote")
        if not self.base_currency.strip() or not self.quote_currency.strip() or not self.source_id.strip():
            raise ValuationUncertain("FX identity/source is required")
        age = (_instant(now, name="now") - _instant(self.as_of, name="fx.as_of")).total_seconds()
        if age < 0:
            raise ValuationUncertain("FX quote is from the future")
        if age > max_age_seconds:
            raise ValuationUncertain("FX quote is stale")
        # Positive base balance would be sold at bid; negative base liability must
        # be bought back at ask. This avoids optimistic mid-price valuation.
        return value * (bid if value >= 0 else ask)


@dataclass(frozen=True)
class PositionValuation:
    instrument_id: str
    quantity: Decimal
    liquidation_mark: Decimal
    market_value: Decimal
    gross_unrealized_pnl: Decimal
    stressed_liquidation_value: Decimal


def value_position(
    instrument_id: str,
    inventory: InventoryBook,
    mark: TradableMark,
    *,
    now: str,
    max_age_seconds: int,
    stress_haircut,
) -> PositionValuation:
    if mark.instrument_id != instrument_id:
        raise ValuationUncertain("mark instrument mismatch")
    bid, ask = mark.validate(now=now, max_age_seconds=max_age_seconds)
    qty = inventory.position_quantity
    liquidation_mark = bid if qty >= 0 else ask
    market_value = qty * liquidation_mark
    unrealized = inventory.unrealized_gross_pnl(liquidation_mark)
    haircut = _dec(stress_haircut, name="stress_haircut")
    if haircut < 0 or haircut >= 1:
        raise ValueError("stress_haircut must be in [0,1)")
    if qty >= 0:
        stressed = market_value * (Decimal("1") - haircut)
    else:
        # Short liabilities become more expensive under adverse stress.
        stressed = market_value * (Decimal("1") + haircut)
    return PositionValuation(
        instrument_id=instrument_id,
        quantity=qty,
        liquidation_mark=liquidation_mark,
        market_value=market_value,
        gross_unrealized_pnl=unrealized,
        stressed_liquidation_value=stressed,
    )
