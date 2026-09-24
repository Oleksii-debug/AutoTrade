"""Deterministic order/action projection for the AutoTrade execution foundation.

Operational terminal states do not freeze economic truth. Provider fills and
corrections may arrive before acknowledgement or after cancellation, and only
unique effective fills are exposed to downstream accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping


class OrderProjectionConflict(ValueError):
    """Raised when immutable order/fill identity is reused inconsistently."""


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
class FillObservation:
    fill_id: str
    provider_execution_id: str
    intent_id: str
    side: str
    quantity: Decimal
    price: Decimal
    provider_revision: str | None = None
    correction_of: str | None = None


@dataclass(frozen=True)
class IntentSnapshot:
    intent_id: str
    side: str
    ordered_quantity: Decimal
    acknowledged: bool
    provider_order_id: str | None
    cancel_requested: bool
    cancel_confirmed: bool
    rejected: bool
    unknown: bool
    filled_quantity: Decimal
    remaining_quantity: Decimal
    overfill_quantity: Decimal
    operational_state: str
    oco_group: str | None
    parent_intent_id: str | None


@dataclass
class _Intent:
    intent_id: str
    side: str
    ordered_quantity: Decimal
    oco_group: str | None
    parent_intent_id: str | None
    acknowledged: bool = False
    provider_order_id: str | None = None
    cancel_requested: bool = False
    cancel_confirmed: bool = False
    rejected: bool = False
    unknown: bool = False


class OrderProjection:
    """Replay-friendly operational projection with immutable fill identity."""

    def __init__(self) -> None:
        self._intents: dict[str, _Intent] = {}
        self._fills: dict[str, FillObservation] = {}
        self._provider_execution_ids: dict[str, str] = {}
        self._corrections: dict[str, FillObservation] = {}
        self._amend_children: dict[str, str] = {}

    def register_intent(
        self,
        *,
        intent_id: str,
        side: str,
        quantity,
        oco_group: str | None = None,
        parent_intent_id: str | None = None,
    ) -> bool:
        iid = _text(intent_id, name="intent_id")
        normalized_side = _text(side, name="side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        qty = _decimal(quantity, name="quantity")
        if qty <= 0:
            raise ValueError("quantity must be positive")
        group = _text(oco_group, name="oco_group") if oco_group is not None else None
        parent = (
            _text(parent_intent_id, name="parent_intent_id")
            if parent_intent_id is not None
            else None
        )
        if parent == iid:
            raise ValueError("intent cannot amend itself")
        if parent is not None and parent not in self._intents:
            raise KeyError(parent)
        proposed = _Intent(iid, normalized_side, qty, group, parent)
        existing = self._intents.get(iid)
        if existing is not None:
            immutable_existing = (
                existing.intent_id,
                existing.side,
                existing.ordered_quantity,
                existing.oco_group,
                existing.parent_intent_id,
            )
            immutable_proposed = (
                proposed.intent_id,
                proposed.side,
                proposed.ordered_quantity,
                proposed.oco_group,
                proposed.parent_intent_id,
            )
            if immutable_existing != immutable_proposed:
                raise OrderProjectionConflict("intent_id already has different immutable content")
            return False
        if parent is not None:
            prior_child = self._amend_children.get(parent)
            if prior_child is not None and prior_child != iid:
                raise OrderProjectionConflict("parent intent already has another amendment child")
            self._amend_children[parent] = iid
        self._intents[iid] = proposed
        return True

    def acknowledge(self, intent_id: str, *, provider_order_id: str) -> bool:
        intent = self._require_intent(intent_id)
        order_id = _text(provider_order_id, name="provider_order_id")
        if intent.provider_order_id is not None:
            if intent.provider_order_id != order_id:
                raise OrderProjectionConflict("intent has conflicting provider_order_id")
            return False
        intent.acknowledged = True
        intent.provider_order_id = order_id
        intent.unknown = False
        return True

    def reject(self, intent_id: str) -> None:
        intent = self._require_intent(intent_id)
        # Provider status and execution facts can arrive out of order. Preserve
        # a late rejection even when fills are already known; snapshot() exposes
        # the contradiction explicitly instead of erasing economic truth.
        intent.rejected = True
        intent.unknown = False

    def mark_unknown(self, intent_id: str) -> None:
        intent = self._require_intent(intent_id)
        if intent.rejected or intent.cancel_confirmed:
            return
        intent.unknown = True

    def request_cancel(self, intent_id: str) -> None:
        intent = self._require_intent(intent_id)
        if intent.rejected or intent.cancel_confirmed:
            return
        intent.cancel_requested = True

    def confirm_cancel(self, intent_id: str) -> None:
        intent = self._require_intent(intent_id)
        if intent.rejected:
            raise OrderProjectionConflict("rejected intent cannot later be cancellation-confirmed")
        intent.cancel_requested = True
        intent.cancel_confirmed = True
        intent.unknown = False

    def observe_fill(
        self,
        *,
        fill_id: str,
        provider_execution_id: str,
        intent_id: str,
        side: str,
        quantity,
        price,
        provider_revision: str | None = None,
    ) -> bool:
        iid = _text(intent_id, name="intent_id")
        intent = self._require_intent(iid)
        fid = _text(fill_id, name="fill_id")
        execution_id = _text(provider_execution_id, name="provider_execution_id")
        normalized_side = _text(side, name="side").upper()
        if normalized_side != intent.side:
            raise OrderProjectionConflict("fill side conflicts with intent side")
        qty = _decimal(quantity, name="fill quantity")
        px = _decimal(price, name="fill price")
        if qty <= 0 or px <= 0:
            raise ValueError("fill quantity and price must be positive")
        revision = (
            _text(provider_revision, name="provider_revision")
            if provider_revision is not None
            else None
        )
        fill = FillObservation(
            fill_id=fid,
            provider_execution_id=execution_id,
            intent_id=iid,
            side=normalized_side,
            quantity=qty,
            price=px,
            provider_revision=revision,
        )
        existing = self._fills.get(fid)
        if existing is not None:
            if existing != fill:
                raise OrderProjectionConflict("fill_id already has different content")
            return False
        other_fill_id = self._provider_execution_ids.get(execution_id)
        if other_fill_id is not None and other_fill_id != fid:
            other = self._fills[other_fill_id]
            if (
                other.intent_id == iid
                and other.side == normalized_side
                and other.quantity == qty
                and other.price == px
                and other.provider_revision == revision
            ):
                return False
            raise OrderProjectionConflict(
                "provider_execution_id already maps to different fill content"
            )
        self._fills[fid] = fill
        self._provider_execution_ids[execution_id] = fid
        intent.unknown = False
        return True

    def correct_fill(
        self,
        original_fill_id: str,
        *,
        correction_fill_id: str,
        provider_execution_id: str,
        quantity,
        price,
        provider_revision: str,
    ) -> bool:
        original_id = _text(original_fill_id, name="original_fill_id")
        original = self._fills.get(original_id)
        if original is None:
            raise KeyError(original_id)
        correction_id = _text(correction_fill_id, name="correction_fill_id")
        execution_id = _text(provider_execution_id, name="provider_execution_id")
        if execution_id != original.provider_execution_id:
            raise OrderProjectionConflict(
                "fill correction must preserve provider_execution_id"
            )
        revision = _text(provider_revision, name="provider_revision")
        qty = _decimal(quantity, name="corrected quantity")
        px = _decimal(price, name="corrected price")
        if qty <= 0 or px <= 0:
            raise ValueError("corrected quantity and price must be positive")
        correction = FillObservation(
            fill_id=correction_id,
            provider_execution_id=execution_id,
            intent_id=original.intent_id,
            side=original.side,
            quantity=qty,
            price=px,
            provider_revision=revision,
            correction_of=original_id,
        )
        existing = self._corrections.get(original_id)
        if existing is not None:
            if existing != correction:
                raise OrderProjectionConflict("fill already has a different correction")
            return False
        if correction_id in self._fills or any(
            value.fill_id == correction_id for value in self._corrections.values()
        ):
            raise OrderProjectionConflict("correction_fill_id already exists")
        mapped = self._provider_execution_ids.get(execution_id)
        if mapped is not None and mapped != original_id:
            raise OrderProjectionConflict(
                "corrected provider_execution_id belongs to another fill"
            )
        self._corrections[original_id] = correction
        return True

    def effective_fills(self) -> tuple[FillObservation, ...]:
        return tuple(
            self._corrections.get(fill_id, fill)
            for fill_id, fill in self._fills.items()
        )

    def filled_quantity(self, intent_id: str) -> Decimal:
        iid = _text(intent_id, name="intent_id")
        self._require_intent(iid)
        return sum(
            (
                fill.quantity
                for fill in self.effective_fills()
                if fill.intent_id == iid
            ),
            Decimal("0"),
        )

    def snapshot(self, intent_id: str) -> IntentSnapshot:
        intent = self._require_intent(intent_id)
        filled = self.filled_quantity(intent.intent_id)
        remaining = max(intent.ordered_quantity - filled, Decimal("0"))
        overfill = max(filled - intent.ordered_quantity, Decimal("0"))
        if intent.rejected and filled > 0:
            state = "REJECTED_WITH_OVERFILL" if overfill > 0 else "REJECTED_WITH_FILL"
        elif intent.rejected:
            state = "REJECTED"
        elif overfill > 0:
            state = "OVERFILLED_AFTER_CANCEL" if intent.cancel_confirmed else "OVERFILLED"
        elif intent.cancel_confirmed:
            state = "CANCELED_WITH_LATE_FILL" if filled > 0 else "CANCELED"
        elif filled == intent.ordered_quantity:
            state = "FILLED"
        elif filled > 0:
            state = "PARTIALLY_FILLED"
        elif intent.unknown:
            state = "UNKNOWN"
        elif intent.cancel_requested:
            state = "CANCEL_PENDING"
        elif intent.acknowledged:
            state = "ACKNOWLEDGED"
        else:
            state = "SUBMITTED"
        return IntentSnapshot(
            intent_id=intent.intent_id,
            side=intent.side,
            ordered_quantity=intent.ordered_quantity,
            acknowledged=intent.acknowledged,
            provider_order_id=intent.provider_order_id,
            cancel_requested=intent.cancel_requested,
            cancel_confirmed=intent.cancel_confirmed,
            rejected=intent.rejected,
            unknown=intent.unknown,
            filled_quantity=filled,
            remaining_quantity=remaining,
            overfill_quantity=overfill,
            operational_state=state,
            oco_group=intent.oco_group,
            parent_intent_id=intent.parent_intent_id,
        )

    def oco_breaches(self) -> Mapping[str, tuple[str, ...]]:
        groups: dict[str, list[str]] = {}
        for intent in self._intents.values():
            if intent.oco_group is None:
                continue
            if self.filled_quantity(intent.intent_id) > 0:
                groups.setdefault(intent.oco_group, []).append(intent.intent_id)
        return MappingProxyType(
            {
                group: tuple(sorted(intent_ids))
                for group, intent_ids in groups.items()
                if len(intent_ids) > 1
            }
        )

    def amendment_child(self, parent_intent_id: str) -> str | None:
        return self._amend_children.get(_text(parent_intent_id, name="parent_intent_id"))

    def _require_intent(self, intent_id: str) -> _Intent:
        iid = _text(intent_id, name="intent_id")
        try:
            return self._intents[iid]
        except KeyError as error:
            raise KeyError(f"Unknown intent: {iid}") from error
