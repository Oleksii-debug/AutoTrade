"""Provider-neutral equity corporate-action and settlement foundation.

This module models evidence-bound economic state transitions. It does not infer
tax residence, does not authorize orders, and never applies adjusted historical
prices as live corporate actions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value, *, name: str, allow_zero: bool = False) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True)
class EquityState:
    symbol: str
    quantity: Decimal
    total_basis: Decimal
    settled_cash: Decimal
    unsettled_cash: Decimal
    currency: str
    borrowed_quantity: Decimal = Decimal("0")
    accrued_financing: Decimal = Decimal("0")
    recalled_quantity: Decimal = Decimal("0")

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        quantity,
        total_basis,
        settled_cash,
        unsettled_cash=0,
        currency: str,
        borrowed_quantity=0,
        accrued_financing=0,
        recalled_quantity=0,
    ) -> "EquityState":
        borrowed = _positive(borrowed_quantity, name="borrowed_quantity", allow_zero=True)
        recalled = _positive(recalled_quantity, name="recalled_quantity", allow_zero=True)
        if recalled > borrowed:
            raise ValueError("recalled_quantity cannot exceed borrowed_quantity")
        return cls(
            symbol=_text(symbol, name="symbol"),
            quantity=_decimal(quantity, name="quantity"),
            total_basis=_positive(total_basis, name="total_basis", allow_zero=True),
            settled_cash=_decimal(settled_cash, name="settled_cash"),
            unsettled_cash=_decimal(unsettled_cash, name="unsettled_cash"),
            currency=_text(currency, name="currency"),
            borrowed_quantity=borrowed,
            accrued_financing=_positive(accrued_financing, name="accrued_financing", allow_zero=True),
            recalled_quantity=recalled,
        )

    @property
    def unit_basis(self) -> Decimal:
        return Decimal("0") if self.quantity == 0 else self.total_basis / abs(self.quantity)


@dataclass(frozen=True)
class CorporateEvent:
    event_id: str
    kind: str
    effective_date: date
    source_revision: str
    payload: Mapping[str, str]

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        kind: str,
        effective_date: date,
        source_revision: str,
        payload: Mapping[str, object],
    ) -> "CorporateEvent":
        normalized_kind = _text(kind, name="kind").upper()
        allowed = {"SPLIT", "CASH_DIVIDEND", "MERGER_CASH", "DELIST"}
        if normalized_kind not in allowed:
            raise ValueError("unsupported corporate event kind")
        if not isinstance(effective_date, date):
            raise ValueError("effective_date is required")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        normalized_payload: dict[str, str] = {}
        for raw_key, raw_value in payload.items():
            key = _text(raw_key, name="payload key")
            if key in normalized_payload:
                raise ValueError(
                    "corporate-event payload keys must be unique after normalization"
                )
            if isinstance(raw_value, bool) or isinstance(raw_value, float):
                raise TypeError("corporate-event numeric payload must use exact decimal input")
            if not isinstance(raw_value, (str, int, Decimal)):
                raise TypeError("corporate-event payload values must be text or exact decimal input")
            normalized_payload[key] = str(raw_value)
        return cls(
            event_id=_text(event_id, name="event_id"),
            kind=normalized_kind,
            effective_date=effective_date,
            source_revision=_text(source_revision, name="source_revision"),
            payload=normalized_payload,
        )


@dataclass(frozen=True)
class Transition:
    event_id: str
    before: EquityState
    after: EquityState
    economic_pnl: Decimal
    reason: str


class CorporateActionBook:
    def __init__(self, state: EquityState):
        self.state = state
        self._events: dict[str, tuple[CorporateEvent, Transition]] = {}

    @property
    def applied_event_ids(self) -> tuple[str, ...]:
        return tuple(self._events)

    def apply(self, event: CorporateEvent) -> Transition:
        existing = self._events.get(event.event_id)
        if existing is not None:
            prior_event, transition = existing
            if prior_event != event:
                raise ValueError("corporate event identity was reused with different content")
            return transition

        before = self.state
        if event.kind == "SPLIT":
            transition = self._split(event)
        elif event.kind == "CASH_DIVIDEND":
            transition = self._cash_dividend(event)
        elif event.kind == "MERGER_CASH":
            transition = self._merger_cash(event)
        elif event.kind == "DELIST":
            transition = self._delist(event)
        else:
            raise AssertionError("unreachable event kind")
        self.state = transition.after
        self._events[event.event_id] = (event, transition)
        return transition

    def _split(self, event: CorporateEvent) -> Transition:
        numerator = _positive(event.payload.get("numerator"), name="numerator")
        denominator = _positive(event.payload.get("denominator"), name="denominator")
        ratio = numerator / denominator
        before = self.state
        after = replace(
            before,
            quantity=before.quantity * ratio,
            borrowed_quantity=before.borrowed_quantity * ratio,
            recalled_quantity=before.recalled_quantity * ratio,
        )
        return Transition(
            event_id=event.event_id,
            before=before,
            after=after,
            economic_pnl=Decimal("0"),
            reason="share quantity adjusted; total basis unchanged",
        )

    def _cash_dividend(self, event: CorporateEvent) -> Transition:
        per_share = _positive(event.payload.get("per_share"), name="per_share", allow_zero=True)
        before = self.state
        entitlement = before.quantity * per_share
        after = replace(before, unsettled_cash=before.unsettled_cash + entitlement)
        return Transition(
            event_id=event.event_id,
            before=before,
            after=after,
            economic_pnl=entitlement,
            reason="dividend recorded as unsettled cash until settlement evidence",
        )

    def _merger_cash(self, event: CorporateEvent) -> Transition:
        cash_per_share = _positive(event.payload.get("cash_per_share"), name="cash_per_share", allow_zero=True)
        before = self.state
        if before.borrowed_quantity != 0:
            raise ValueError("cash merger with unresolved borrowed quantity requires explicit provider handling")
        proceeds = before.quantity * cash_per_share
        pnl = proceeds - before.total_basis
        after = replace(
            before,
            quantity=Decimal("0"),
            total_basis=Decimal("0"),
            unsettled_cash=before.unsettled_cash + proceeds,
        )
        return Transition(
            event_id=event.event_id,
            before=before,
            after=after,
            economic_pnl=pnl,
            reason="shares extinguished and cash consideration recorded unsettled",
        )

    def _delist(self, event: CorporateEvent) -> Transition:
        if "cash_per_share" not in event.payload:
            raise ValueError("delisting cannot erase holdings without evidenced consideration")
        return self._merger_cash(
            CorporateEvent(
                event_id=event.event_id,
                kind="MERGER_CASH",
                effective_date=event.effective_date,
                source_revision=event.source_revision,
                payload={"cash_per_share": event.payload["cash_per_share"]},
            )
        )


def settle_cash(state: EquityState, amount) -> EquityState:
    """Move an evidenced receivable or payable from unsettled to settled cash."""

    value = _decimal(amount, name="amount")
    outstanding = state.unsettled_cash
    if value == 0:
        return state
    if outstanding == 0:
        raise ValueError("cannot settle cash when no unsettled balance exists")
    if (value > 0) != (outstanding > 0):
        raise ValueError("settlement amount must have the same sign as unsettled cash")
    if abs(value) > abs(outstanding):
        raise ValueError("cannot settle more cash than is currently unsettled")
    return replace(
        state,
        unsettled_cash=outstanding - value,
        settled_cash=state.settled_cash + value,
    )


def record_unsettled_purchase(
    state: EquityState,
    *,
    quantity,
    price,
) -> EquityState:
    qty = _positive(quantity, name="quantity")
    unit_price = _positive(price, name="price")
    cost = qty * unit_price
    if cost > state.settled_cash:
        raise ValueError("purchase cannot spend unfunded settled cash")
    return replace(
        state,
        quantity=state.quantity + qty,
        total_basis=state.total_basis + cost,
        settled_cash=state.settled_cash - cost,
    )


def establish_short(
    state: EquityState,
    *,
    quantity,
    sale_price,
) -> EquityState:
    qty = _positive(quantity, name="quantity")
    price = _positive(sale_price, name="sale_price")
    if state.quantity > 0:
        raise ValueError("foundation does not net a long position into a new short implicitly")
    proceeds = qty * price
    return replace(
        state,
        quantity=state.quantity - qty,
        borrowed_quantity=state.borrowed_quantity + qty,
        unsettled_cash=state.unsettled_cash + proceeds,
    )


def accrue_borrow_financing(
    state: EquityState,
    *,
    daily_rate,
    marked_value,
    days: int,
) -> EquityState:
    rate = _positive(daily_rate, name="daily_rate", allow_zero=True)
    value = _positive(marked_value, name="marked_value", allow_zero=True)
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        raise ValueError("days must be a non-negative integer")
    charge = value * rate * Decimal(days)
    return replace(
        state,
        accrued_financing=state.accrued_financing + charge,
        unsettled_cash=state.unsettled_cash - charge,
    )


def record_recall(state: EquityState, quantity) -> EquityState:
    qty = _positive(quantity, name="quantity")
    available = state.borrowed_quantity - state.recalled_quantity
    if qty > available:
        raise ValueError("recall exceeds currently borrowed unrecalled quantity")
    return replace(state, recalled_quantity=state.recalled_quantity + qty)


def cover_recalled_short(state: EquityState, *, quantity, buy_price) -> EquityState:
    qty = _positive(quantity, name="quantity")
    price = _positive(buy_price, name="buy_price")
    if qty > state.recalled_quantity or qty > state.borrowed_quantity:
        raise ValueError("cover quantity exceeds recalled/borrowed quantity")
    cost = qty * price
    if cost > state.settled_cash:
        raise ValueError("cover requires sufficient settled cash in this foundation")
    return replace(
        state,
        quantity=state.quantity + qty,
        borrowed_quantity=state.borrowed_quantity - qty,
        recalled_quantity=state.recalled_quantity - qty,
        settled_cash=state.settled_cash - cost,
    )
