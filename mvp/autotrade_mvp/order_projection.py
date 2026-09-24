"""Provider-neutral order lifecycle projection.

Acknowledgements never create economic fills. Unique fill identities are the
only source of executed quantity. Late corrections and busts remain visible
even after a terminal order state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    quantity: Decimal
    price: Decimal
    active: bool = True


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str
    requested_quantity: Decimal
    state: str
    filled_quantity: Decimal
    open_quantity: Decimal
    average_fill_price: Decimal | None
    provider_order_id: str | None
    oco_group_id: str | None
    oco_violation: bool
    fill_count: int


class OrderProjectionConflict(ValueError):
    """Raised when immutable provider facts conflict."""


class OrderProjection:
    def __init__(
        self,
        *,
        client_order_id: str,
        requested_quantity,
        oco_group_id: str | None = None,
    ):
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError("client_order_id is required")
        requested = _decimal(requested_quantity, name="requested_quantity")
        if requested <= 0:
            raise ValueError("requested_quantity must be positive")
        self.client_order_id = client_order_id.strip()
        self.requested_quantity = requested
        self.oco_group_id = oco_group_id
        self.provider_order_id: str | None = None
        self.submission_state = "PENDING"
        self.cancelled = False
        self.rejected = False
        self._fills: dict[str, FillRecord] = {}
        self._oco_violation = False

    def acknowledge(self, *, provider_order_id: str, status: str = "ACCEPTED") -> None:
        if not provider_order_id or not provider_order_id.strip():
            raise ValueError("provider_order_id is required")
        normalized = status.upper()
        if normalized not in {"ACCEPTED", "REJECTED", "UNKNOWN"}:
            raise ValueError("unsupported submission status")
        if self.provider_order_id is not None and self.provider_order_id != provider_order_id:
            raise OrderProjectionConflict("provider_order_id changed")
        self.provider_order_id = provider_order_id
        if normalized == "REJECTED":
            self.rejected = True
        self.submission_state = normalized

    def record_fill(self, *, fill_id: str, quantity, price) -> bool:
        if not fill_id or not fill_id.strip():
            raise ValueError("fill_id is required")
        qty = _decimal(quantity, name="quantity")
        px = _decimal(price, name="price")
        if qty <= 0 or px <= 0:
            raise ValueError("fill quantity and price must be positive")
        candidate = FillRecord(fill_id=fill_id.strip(), quantity=qty, price=px, active=True)
        existing = self._fills.get(candidate.fill_id)
        if existing is not None:
            if existing != candidate:
                raise OrderProjectionConflict("fill_id already has different content")
            return False
        self._fills[candidate.fill_id] = candidate
        return True

    def bust_fill(self, fill_id: str) -> bool:
        existing = self._fills.get(fill_id)
        if existing is None:
            raise KeyError(fill_id)
        if not existing.active:
            return False
        self._fills[fill_id] = FillRecord(
            fill_id=existing.fill_id,
            quantity=existing.quantity,
            price=existing.price,
            active=False,
        )
        return True

    def correct_fill(self, *, fill_id: str, quantity, price) -> None:
        existing = self._fills.get(fill_id)
        if existing is None:
            raise KeyError(fill_id)
        qty = _decimal(quantity, name="quantity")
        px = _decimal(price, name="price")
        if qty <= 0 or px <= 0:
            raise ValueError("corrected quantity and price must be positive")
        self._fills[fill_id] = FillRecord(
            fill_id=fill_id,
            quantity=qty,
            price=px,
            active=existing.active,
        )

    def cancel(self) -> None:
        self.cancelled = True

    def mark_oco_peer_filled(self) -> None:
        if self.oco_group_id is None:
            raise ValueError("order is not in an OCO group")
        if self.filled_quantity > 0:
            self._oco_violation = True

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self._fills.values() if f.active), Decimal("0"))

    @property
    def active_fills(self) -> tuple[FillRecord, ...]:
        return tuple(f for f in self._fills.values() if f.active)

    @property
    def average_fill_price(self) -> Decimal | None:
        fills = self.active_fills
        total = sum((f.quantity for f in fills), Decimal("0"))
        if total == 0:
            return None
        notional = sum((f.quantity * f.price for f in fills), Decimal("0"))
        return notional / total

    @property
    def open_quantity(self) -> Decimal:
        remaining = self.requested_quantity - self.filled_quantity
        return remaining if remaining > 0 else Decimal("0")

    @property
    def state(self) -> str:
        filled = self.filled_quantity
        if self._oco_violation:
            return "OCO_VIOLATION"
        if filled > self.requested_quantity:
            return "OVERFILLED"
        if self.cancelled:
            if filled == self.requested_quantity:
                return "FILLED_AFTER_CANCEL"
            return "PARTIALLY_FILLED_CANCELLED" if filled > 0 else "CANCELLED"
        if self.rejected:
            if filled == self.requested_quantity:
                return "FILLED_AFTER_REJECT"
            return "PARTIALLY_FILLED_AFTER_REJECT" if filled > 0 else "REJECTED"
        if filled == self.requested_quantity:
            return "FILLED"
        if filled > 0:
            return "PARTIALLY_FILLED"
        if self.submission_state == "ACCEPTED":
            return "WORKING"
        if self.submission_state == "UNKNOWN":
            return "UNKNOWN"
        return "PENDING"

    @property
    def economic_terminal_outcome(self) -> str | None:
        state = self.state
        if state in {"FILLED", "FILLED_AFTER_CANCEL", "FILLED_AFTER_REJECT"}:
            return "FILLED"
        if state in {"CANCELLED", "PARTIALLY_FILLED_CANCELLED"}:
            return "CANCELLED"
        if state in {"REJECTED", "PARTIALLY_FILLED_AFTER_REJECT"}:
            return "REJECTED"
        return None

    def snapshot(self) -> OrderSnapshot:
        return OrderSnapshot(
            client_order_id=self.client_order_id,
            requested_quantity=self.requested_quantity,
            state=self.state,
            filled_quantity=self.filled_quantity,
            open_quantity=self.open_quantity,
            average_fill_price=self.average_fill_price,
            provider_order_id=self.provider_order_id,
            oco_group_id=self.oco_group_id,
            oco_violation=self._oco_violation,
            fill_count=len(self.active_fills),
        )


class OcoGroupProjection:
    def __init__(self, group_id: str):
        if not group_id or not group_id.strip():
            raise ValueError("group_id is required")
        self.group_id = group_id.strip()
        self._orders: dict[str, OrderProjection] = {}

    def add(self, order: OrderProjection) -> None:
        if order.oco_group_id != self.group_id:
            raise ValueError("order belongs to another OCO group")
        existing = self._orders.get(order.client_order_id)
        if existing is not None and existing is not order:
            raise OrderProjectionConflict("client_order_id already registered")
        self._orders[order.client_order_id] = order

    def refresh(self) -> bool:
        filled_orders = [o for o in self._orders.values() if o.filled_quantity > 0]
        violation = len(filled_orders) > 1
        if violation:
            for order in filled_orders:
                order.mark_oco_peer_filled()
        return violation
