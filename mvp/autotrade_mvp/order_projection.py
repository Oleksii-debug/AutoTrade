"""Canonical provider-neutral order lifecycle projection.

Acknowledgements never create economic fills. Provider execution identity is
unique, corrections and busts preserve immutable observation history, and
terminal operational states never suppress later economic truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


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
    provider_execution_id: str
    quantity: Decimal
    price: Decimal
    active: bool = True
    provider_revision: str | None = None
    correction_of: str | None = None


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str
    instrument: str
    side: str
    requested_quantity: Decimal
    state: str
    filled_quantity: Decimal
    open_quantity: Decimal
    overfill_quantity: Decimal
    average_fill_price: Decimal | None
    provider_order_id: str | None
    oco_group_id: str | None
    oco_violation: bool
    fill_count: int
    observation_count: int
    cancel_requested: bool
    cancel_confirmed: bool


class OrderProjectionConflict(ValueError):
    """Raised when immutable provider facts conflict."""


class OrderProjection:
    def __init__(
        self,
        *,
        client_order_id: str,
        instrument: str,
        side: str,
        requested_quantity,
        oco_group_id: str | None = None,
        parent_intent_id: str | None = None,
    ):
        self.client_order_id = _text(client_order_id, name="client_order_id")
        self.instrument = _text(instrument, name="instrument")
        normalized_side = _text(side, name="side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        self.side = normalized_side
        requested = _decimal(requested_quantity, name="requested_quantity")
        if requested <= 0:
            raise ValueError("requested_quantity must be positive")
        self.requested_quantity = requested
        self.oco_group_id = (
            _text(oco_group_id, name="oco_group_id")
            if oco_group_id is not None
            else None
        )
        self.parent_intent_id = (
            _text(parent_intent_id, name="parent_intent_id")
            if parent_intent_id is not None
            else None
        )
        if self.parent_intent_id == self.client_order_id:
            raise ValueError("order cannot amend itself")
        self.provider_order_id: str | None = None
        self.submission_state = "PENDING"
        self.cancel_requested = False
        self.cancelled = False
        self.rejected = False
        self._fills: dict[str, FillRecord] = {}
        self._provider_execution_ids: dict[str, str] = {}
        self._revision_records: dict[tuple[str, str], FillRecord] = {}
        self._history: list[FillRecord] = []
        self._oco_violation = False

    def acknowledge(
        self,
        *,
        provider_order_id: str,
        status: str = "ACCEPTED",
    ) -> None:
        order_id = _text(provider_order_id, name="provider_order_id")
        normalized = _text(status, name="status").upper()
        if normalized not in {"ACCEPTED", "REJECTED", "UNKNOWN"}:
            raise ValueError("unsupported submission status")
        if self.provider_order_id is not None and self.provider_order_id != order_id:
            raise OrderProjectionConflict("provider_order_id changed")
        self.provider_order_id = order_id
        if normalized == "REJECTED":
            self.rejected = True
        self.submission_state = normalized

    def record_fill(
        self,
        *,
        fill_id: str,
        provider_execution_id: str,
        quantity,
        price,
        provider_revision: str | None = None,
    ) -> bool:
        fid = _text(fill_id, name="fill_id")
        execution_id = _text(
            provider_execution_id,
            name="provider_execution_id",
        )
        qty = _decimal(quantity, name="quantity")
        px = _decimal(price, name="price")
        if qty <= 0 or px <= 0:
            raise ValueError("fill quantity and price must be positive")
        revision = (
            _text(provider_revision, name="provider_revision")
            if provider_revision is not None
            else None
        )
        candidate = FillRecord(
            fill_id=fid,
            provider_execution_id=execution_id,
            quantity=qty,
            price=px,
            active=True,
            provider_revision=revision,
        )
        if candidate in self._history:
            # Providers may redeliver an older immutable observation after a
            # later correction/bust. Exact historical duplicates are harmless
            # and must never roll the current projection backward.
            return False
        if any(item.fill_id == fid for item in self._history):
            raise OrderProjectionConflict("fill_id already belongs to another observation")

        existing = self._fills.get(fid)
        if existing is not None:
            if existing == candidate:
                return False
            raise OrderProjectionConflict("fill_id already has different content")

        mapped = self._provider_execution_ids.get(execution_id)
        if mapped is not None:
            raise OrderProjectionConflict(
                "provider_execution_id already maps to another fill_id"
            )

        if revision is not None:
            key = (fid, revision)
            if key in self._revision_records:
                raise OrderProjectionConflict(
                    "provider revision already maps to another observation"
                )
            self._revision_records[key] = candidate

        self._fills[fid] = candidate
        self._provider_execution_ids[execution_id] = fid
        self._history.append(candidate)
        return True

    def _apply_revision(
        self,
        *,
        fill_id: str,
        quantity: Decimal,
        price: Decimal,
        active: bool,
        provider_revision: str,
        correction_fill_id: str | None = None,
    ) -> bool:
        fid = _text(fill_id, name="fill_id")
        current = self._fills.get(fid)
        if current is None:
            raise KeyError(fid)
        revision = _text(provider_revision, name="provider_revision")
        observation_id = (
            _text(correction_fill_id, name="correction_fill_id")
            if correction_fill_id is not None
            else fid
        )
        candidate = FillRecord(
            fill_id=observation_id,
            provider_execution_id=current.provider_execution_id,
            quantity=quantity,
            price=price,
            active=active,
            provider_revision=revision,
            correction_of=fid,
        )
        key = (fid, revision)
        prior = self._revision_records.get(key)
        if prior is not None:
            if prior == candidate:
                return False
            raise OrderProjectionConflict(
                "provider revision already has different content"
            )
        if observation_id != fid:
            for item in self._history:
                if item.fill_id == observation_id and item != candidate:
                    raise OrderProjectionConflict(
                        "correction_fill_id already belongs to another observation"
                    )
        self._revision_records[key] = candidate
        self._fills[fid] = candidate
        self._history.append(candidate)
        return True

    def bust_fill(
        self,
        fill_id: str,
        *,
        provider_revision: str,
        correction_fill_id: str | None = None,
    ) -> bool:
        fid = _text(fill_id, name="fill_id")
        existing = self._fills.get(fid)
        if existing is None:
            raise KeyError(fid)
        return self._apply_revision(
            fill_id=fid,
            quantity=existing.quantity,
            price=existing.price,
            active=False,
            provider_revision=provider_revision,
            correction_fill_id=correction_fill_id,
        )

    def correct_fill(
        self,
        *,
        fill_id: str,
        quantity,
        price,
        provider_revision: str,
        correction_fill_id: str | None = None,
    ) -> bool:
        fid = _text(fill_id, name="fill_id")
        existing = self._fills.get(fid)
        if existing is None:
            raise KeyError(fid)
        qty = _decimal(quantity, name="corrected quantity")
        px = _decimal(price, name="corrected price")
        if qty <= 0 or px <= 0:
            raise ValueError("corrected quantity and price must be positive")
        return self._apply_revision(
            fill_id=fid,
            quantity=qty,
            price=px,
            active=existing.active,
            provider_revision=provider_revision,
            correction_fill_id=correction_fill_id,
        )

    def request_cancel(self) -> None:
        """Record a pending cancel request without inventing provider confirmation."""
        if not self.cancelled:
            self.cancel_requested = True

    def confirm_cancel(self) -> None:
        """Record provider-confirmed cancellation of the still-unfilled remainder."""
        self.cancel_requested = True
        self.cancelled = True

    def cancel(self) -> None:
        """Backward-compatible alias for confirmed cancellation evidence."""
        self.confirm_cancel()

    def mark_oco_peer_filled(self) -> None:
        if self.oco_group_id is None:
            raise ValueError("order is not in an OCO group")
        if self.filled_quantity > 0:
            self._oco_violation = True

    @property
    def filled_quantity(self) -> Decimal:
        return sum(
            (fill.quantity for fill in self._fills.values() if fill.active),
            Decimal("0"),
        )

    @property
    def active_fills(self) -> tuple[FillRecord, ...]:
        return tuple(fill for fill in self._fills.values() if fill.active)

    @property
    def fill_history(self) -> tuple[FillRecord, ...]:
        return tuple(self._history)

    @property
    def provider_execution_index(self) -> Mapping[str, str]:
        return dict(self._provider_execution_ids)

    @property
    def average_fill_price(self) -> Decimal | None:
        fills = self.active_fills
        total = sum((fill.quantity for fill in fills), Decimal("0"))
        if total == 0:
            return None
        notional = sum(
            (fill.quantity * fill.price for fill in fills),
            Decimal("0"),
        )
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
            if self.cancelled:
                return "OVERFILLED_AFTER_CANCEL"
            if self.rejected:
                return "OVERFILLED_AFTER_REJECT"
            if self.cancel_requested:
                return "OVERFILLED_DURING_CANCEL"
            return "OVERFILLED"
        if self.cancelled:
            return "PARTIALLY_FILLED_CANCELLED" if filled > 0 else "CANCELLED"
        if self.rejected:
            return "FILLED_AFTER_REJECT" if filled > 0 else "REJECTED"
        if filled == self.requested_quantity:
            return "FILLED"
        if self.cancel_requested:
            return (
                "PARTIALLY_FILLED_CANCEL_REQUESTED"
                if filled > 0
                else "CANCEL_REQUESTED"
            )
        if filled > 0:
            return "PARTIALLY_FILLED"
        if self.submission_state == "ACCEPTED":
            return "WORKING"
        if self.submission_state == "UNKNOWN":
            return "UNKNOWN"
        return "PENDING"

    def snapshot(self) -> OrderSnapshot:
        return OrderSnapshot(
            client_order_id=self.client_order_id,
            instrument=self.instrument,
            side=self.side,
            requested_quantity=self.requested_quantity,
            state=self.state,
            filled_quantity=self.filled_quantity,
            open_quantity=self.open_quantity,
            overfill_quantity=max(
                self.filled_quantity - self.requested_quantity,
                Decimal("0"),
            ),
            average_fill_price=self.average_fill_price,
            provider_order_id=self.provider_order_id,
            oco_group_id=self.oco_group_id,
            oco_violation=self._oco_violation,
            fill_count=len(self.active_fills),
            observation_count=len(self._history),
            cancel_requested=self.cancel_requested,
            cancel_confirmed=self.cancelled,
        )


class OrderBookProjection:
    """Aggregate multiple canonical orders without creating a second order authority."""

    def __init__(self) -> None:
        self._orders: dict[str, OrderProjection] = {}
        self._amend_children: dict[str, str] = {}

    def register(self, order: OrderProjection) -> bool:
        if not isinstance(order, OrderProjection):
            raise TypeError("order must be OrderProjection")
        existing = self._orders.get(order.client_order_id)
        if existing is not None:
            if existing is order:
                return False
            raise OrderProjectionConflict("client_order_id already registered")
        parent = order.parent_intent_id
        if parent is not None:
            if parent not in self._orders:
                raise KeyError(parent)
            child = self._amend_children.get(parent)
            if child is not None and child != order.client_order_id:
                raise OrderProjectionConflict(
                    "parent intent already has another amendment child"
                )
            self._amend_children[parent] = order.client_order_id
        self._orders[order.client_order_id] = order
        return True

    def create(
        self,
        *,
        client_order_id: str,
        instrument: str,
        side: str,
        requested_quantity,
        oco_group_id: str | None = None,
        parent_intent_id: str | None = None,
    ) -> OrderProjection:
        order = OrderProjection(
            client_order_id=client_order_id,
            instrument=instrument,
            side=side,
            requested_quantity=requested_quantity,
            oco_group_id=oco_group_id,
            parent_intent_id=parent_intent_id,
        )
        self.register(order)
        return order

    def order(self, client_order_id: str) -> OrderProjection:
        order_id = _text(client_order_id, name="client_order_id")
        try:
            return self._orders[order_id]
        except KeyError as error:
            raise KeyError(f"Unknown order: {order_id}") from error

    def amendment_child(self, parent_intent_id: str) -> str | None:
        return self._amend_children.get(_text(parent_intent_id, name="parent_intent_id"))

    def effective_fills(self) -> tuple[FillRecord, ...]:
        return tuple(
            fill
            for order in self._orders.values()
            for fill in order.active_fills
        )

    def snapshots(self) -> tuple[OrderSnapshot, ...]:
        return tuple(order.snapshot() for order in self._orders.values())

    def oco_breaches(self) -> Mapping[str, tuple[str, ...]]:
        groups: dict[str, list[str]] = {}
        for order in self._orders.values():
            if order.oco_group_id is None or order.filled_quantity <= 0:
                continue
            groups.setdefault(order.oco_group_id, []).append(order.client_order_id)
        return {
            group: tuple(sorted(order_ids))
            for group, order_ids in groups.items()
            if len(order_ids) > 1
        }


class OcoGroupProjection:
    def __init__(self, group_id: str):
        self.group_id = _text(group_id, name="group_id")
        self._orders: dict[str, OrderProjection] = {}

    def add(self, order: OrderProjection) -> None:
        if not isinstance(order, OrderProjection):
            raise TypeError("order must be OrderProjection")
        if order.oco_group_id != self.group_id:
            raise ValueError("order belongs to another OCO group")
        existing = self._orders.get(order.client_order_id)
        if existing is not None and existing is not order:
            raise OrderProjectionConflict("client_order_id already registered")
        self._orders[order.client_order_id] = order

    def refresh(self) -> bool:
        filled_orders = [
            order for order in self._orders.values()
            if order.filled_quantity > 0
        ]
        violation = len(filled_orders) > 1
        if violation:
            for order in filled_orders:
                order.mark_oco_peer_filled()
        return violation
