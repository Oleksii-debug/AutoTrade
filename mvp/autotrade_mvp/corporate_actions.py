"""Provider-neutral equity corporate-action and settlement foundation.

This module models evidence-bound economic state transitions. It does not infer
tax residence, does not authorize orders, and never applies adjusted historical
prices as live corporate actions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

from .instruments import InstrumentRegistry, InstrumentVersion


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

    def __post_init__(self) -> None:
        quantity = _decimal(self.quantity, name="quantity")
        borrowed = _positive(
            self.borrowed_quantity,
            name="borrowed_quantity",
            allow_zero=True,
        )
        recalled = _positive(
            self.recalled_quantity,
            name="recalled_quantity",
            allow_zero=True,
        )
        if recalled > borrowed:
            raise ValueError("recalled_quantity cannot exceed borrowed_quantity")
        if quantity < 0 and borrowed != -quantity:
            raise ValueError(
                "cash-equity short quantity must be fully matched by borrowed_quantity"
            )
        if quantity >= 0 and borrowed != 0:
            raise ValueError(
                "borrowed_quantity is valid only for an open cash-equity short"
            )
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self,
            "total_basis",
            _positive(self.total_basis, name="total_basis", allow_zero=True),
        )
        object.__setattr__(
            self,
            "settled_cash",
            _decimal(self.settled_cash, name="settled_cash"),
        )
        object.__setattr__(
            self,
            "unsettled_cash",
            _decimal(self.unsettled_cash, name="unsettled_cash"),
        )
        object.__setattr__(
            self,
            "currency",
            _text(self.currency, name="currency").upper(),
        )
        object.__setattr__(self, "borrowed_quantity", borrowed)
        object.__setattr__(
            self,
            "accrued_financing",
            _positive(
                self.accrued_financing,
                name="accrued_financing",
                allow_zero=True,
            ),
        )
        object.__setattr__(self, "recalled_quantity", recalled)

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
            currency=_text(currency, name="currency").upper(),
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
    instrument_id: str
    instrument_version: int
    kind: str
    effective_date: date
    source_revision: str
    payload: Mapping[str, str]
    source_sequence: int | None = None

    def __post_init__(self) -> None:
        kind = _text(self.kind, name="kind").upper()
        if kind not in {
            "SPLIT",
            "CASH_DIVIDEND",
            "MERGER_CASH",
            "DELIST",
            "SYMBOL_CHANGE",
        }:
            raise ValueError("unsupported corporate event kind")
        instrument_id = _text(self.instrument_id, name="instrument_id")
        if (
            not isinstance(self.instrument_version, int)
            or isinstance(self.instrument_version, bool)
            or self.instrument_version < 1
        ):
            raise ValueError("instrument_version must be a positive integer")
        if not isinstance(self.effective_date, date):
            raise ValueError("effective_date is required")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")

        normalized_payload: dict[str, str] = {}
        for raw_key, raw_value in self.payload.items():
            key = _text(raw_key, name="payload key")
            if key in normalized_payload:
                raise ValueError(
                    "corporate-event payload keys must be unique after normalization"
                )
            if isinstance(raw_value, bool) or isinstance(raw_value, float):
                raise TypeError(
                    "corporate-event numeric payload must use exact decimal input"
                )
            if not isinstance(raw_value, (str, int, Decimal)):
                raise TypeError(
                    "corporate-event payload values must be text or exact decimal input"
                )
            normalized_payload[key] = str(raw_value)

        object.__setattr__(self, "event_id", _text(self.event_id, name="event_id"))
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "source_revision",
            _text(self.source_revision, name="source_revision"),
        )
        if self.source_sequence is not None and (
            not isinstance(self.source_sequence, int)
            or isinstance(self.source_sequence, bool)
            or self.source_sequence < 0
        ):
            raise ValueError("source_sequence must be a non-negative integer when provided")
        object.__setattr__(self, "payload", normalized_payload)

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        instrument_id: str,
        instrument_version: int,
        kind: str,
        effective_date: date,
        source_revision: str,
        payload: Mapping[str, object],
        source_sequence: int | None = None,
    ) -> "CorporateEvent":
        normalized_kind = _text(kind, name="kind").upper()
        allowed = {
            "SPLIT",
            "CASH_DIVIDEND",
            "MERGER_CASH",
            "DELIST",
            "SYMBOL_CHANGE",
        }
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
            instrument_id=_text(instrument_id, name="instrument_id"),
            instrument_version=instrument_version,
            kind=normalized_kind,
            effective_date=effective_date,
            source_revision=_text(source_revision, name="source_revision"),
            payload=normalized_payload,
            source_sequence=source_sequence,
        )


@dataclass(frozen=True)
class Transition:
    event_id: str
    before: EquityState
    after: EquityState
    economic_pnl: Decimal
    reason: str


class CorporateActionBook:
    def __init__(
        self,
        state: EquityState,
        *,
        instrument_version: InstrumentVersion,
        registry: InstrumentRegistry,
    ):
        if not isinstance(state, EquityState):
            raise TypeError("state must be EquityState")
        if not isinstance(instrument_version, InstrumentVersion):
            raise TypeError("instrument_version must be InstrumentVersion")
        if not isinstance(registry, InstrumentRegistry):
            raise TypeError("registry must be InstrumentRegistry")
        if instrument_version.asset_class != "CASH_EQUITY":
            raise ValueError("corporate-action book requires a CASH_EQUITY instrument")
        registered = tuple(
            item
            for item in registry.versions(instrument_version.instrument_id)
            if item.version == instrument_version.version
        )
        if len(registered) != 1 or registered[0] != instrument_version:
            raise ValueError(
                "instrument_version must be the exact version registered in InstrumentRegistry"
            )
        if state.symbol != instrument_version.provider_symbol:
            raise ValueError(
                "equity state symbol does not match bound instrument version"
            )
        settlement_currency = _text(
            instrument_version.settlement_currency,
            name="instrument settlement_currency",
        ).upper()
        if state.currency != settlement_currency:
            raise ValueError(
                "equity state currency does not match bound instrument settlement currency"
            )
        self.state = state
        self.instrument_version = instrument_version
        self.registry = registry
        self._events: dict[str, tuple[CorporateEvent, Transition]] = {}
        self._last_effective_date: date | None = None
        self._last_source_sequence: int | None = None

    @classmethod
    def replay(
        cls,
        state: EquityState,
        *,
        instrument_version: InstrumentVersion,
        registry: InstrumentRegistry,
        events: Iterable[CorporateEvent],
    ) -> "CorporateActionBook":
        book = cls(
            state,
            instrument_version=instrument_version,
            registry=registry,
        )
        materialized = tuple(events)
        if not all(isinstance(event, CorporateEvent) for event in materialized):
            raise TypeError("events must contain CorporateEvent values")

        by_date: dict[date, list[CorporateEvent]] = {}
        for event in materialized:
            by_date.setdefault(event.effective_date, []).append(event)
        for effective_date, same_day in by_date.items():
            if len(same_day) < 2:
                continue
            if any(event.source_sequence is None for event in same_day):
                raise ValueError(
                    "same-date corporate events require authoritative source_sequence"
                )
            sequences = [event.source_sequence for event in same_day]
            if len(set(sequences)) != len(sequences):
                raise ValueError(
                    "same-date corporate events require unique source_sequence"
                )

        ordered = sorted(
            materialized,
            key=lambda event: (
                event.effective_date,
                -1 if event.source_sequence is None else event.source_sequence,
            ),
        )
        for event in ordered:
            book.apply(event)
        return book

    @property
    def applied_event_ids(self) -> tuple[str, ...]:
        return tuple(self._events)

    @property
    def events(self) -> tuple[CorporateEvent, ...]:
        """Immutable accepted corporate-event history for durable handoff."""

        return tuple(event for event, _transition in self._events.values())

    def _cash_event_amount(
        self,
        event: CorporateEvent,
        *,
        amount_key: str,
    ) -> Decimal:
        expected_keys = {amount_key, "currency"}
        if set(event.payload) != expected_keys:
            raise ValueError(
                f"{event.kind} requires exactly {amount_key} and currency"
            )
        currency = _text(event.payload.get("currency"), name="currency").upper()
        expected_currency = self.instrument_version.settlement_currency.upper()
        if currency != expected_currency or currency != self.state.currency:
            raise ValueError(
                "corporate-action cash currency does not match bound settlement currency"
            )
        return _positive(
            event.payload.get(amount_key),
            name=amount_key,
            allow_zero=True,
        )

    def apply(self, event: CorporateEvent) -> Transition:
        if not isinstance(event, CorporateEvent):
            raise TypeError("event must be CorporateEvent")
        existing = self._events.get(event.event_id)
        if existing is not None:
            prior_event, transition = existing
            if prior_event != event:
                raise ValueError("corporate event identity was reused with different content")
            return transition

        current = self.instrument_version
        if (
            event.instrument_id != current.instrument_id
            or event.instrument_version != current.version
        ):
            raise ValueError("corporate event instrument identity mismatch")
        if (
            self._last_effective_date is not None
            and event.effective_date < self._last_effective_date
        ):
            raise ValueError(
                "corporate events must be applied in non-decreasing effective-date order"
            )
        if (
            self._last_effective_date is not None
            and event.effective_date == self._last_effective_date
        ):
            if self._last_source_sequence is None or event.source_sequence is None:
                raise ValueError(
                    "same-date corporate events require authoritative source_sequence"
                )
            if event.source_sequence <= self._last_source_sequence:
                raise ValueError(
                    "same-date corporate events must follow increasing source_sequence"
                )

        successor = None
        if event.kind == "SPLIT":
            transition = self._split(event)
        elif event.kind == "CASH_DIVIDEND":
            transition = self._cash_dividend(event)
        elif event.kind == "MERGER_CASH":
            transition = self._merger_cash(event)
        elif event.kind == "DELIST":
            transition = self._delist(event)
        elif event.kind == "SYMBOL_CHANGE":
            transition, successor = self._symbol_change(event)
        else:
            raise AssertionError("unreachable event kind")
        self.state = transition.after
        if successor is not None:
            self.instrument_version = successor
        self._events[event.event_id] = (event, transition)
        self._last_effective_date = event.effective_date
        self._last_source_sequence = event.source_sequence
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
        per_share = self._cash_event_amount(event, amount_key="per_share")
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
        cash_per_share = self._cash_event_amount(
            event,
            amount_key="cash_per_share",
        )
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
                instrument_id=event.instrument_id,
                instrument_version=event.instrument_version,
                kind="MERGER_CASH",
                effective_date=event.effective_date,
                source_revision=event.source_revision,
                payload={
                    "cash_per_share": event.payload["cash_per_share"],
                    "currency": event.payload.get("currency", ""),
                },
            )
        )

    def _symbol_change(
        self,
        event: CorporateEvent,
    ) -> tuple[Transition, InstrumentVersion]:
        if set(event.payload) != {"successor_instrument_version"}:
            raise ValueError(
                "symbol change requires only successor_instrument_version"
            )
        raw_version = event.payload["successor_instrument_version"]
        if not raw_version.isdigit():
            raise ValueError(
                "successor_instrument_version must be a positive integer"
            )
        successor_version = int(raw_version)
        current = self.instrument_version
        if successor_version != current.version + 1:
            raise ValueError(
                "symbol change successor must be the next instrument version"
            )
        matches = tuple(
            item
            for item in self.registry.versions(current.instrument_id)
            if item.version == successor_version
        )
        if len(matches) != 1:
            raise ValueError(
                "symbol change successor is not registered in InstrumentRegistry"
            )
        successor = matches[0]
        if successor.instrument_id != current.instrument_id:
            raise ValueError(
                "symbol change cannot change immutable instrument_id"
            )
        if successor.effective_from.date() != event.effective_date:
            raise ValueError(
                "symbol change effective date does not match successor version"
            )
        if (
            successor.provider_id != current.provider_id
            or successor.venue_id != current.venue_id
        ):
            raise ValueError(
                "symbol change successor must preserve provider and venue identity"
            )
        if successor.provider_symbol == current.provider_symbol:
            raise ValueError(
                "symbol change successor must carry a different provider symbol"
            )

        # SYMBOL_CHANGE is a zero-P&L identity transition, not a generic
        # InstrumentVersion migration. If any field that changes the economic
        # meaning of the held quantity changes, a different corporate-action
        # treatment is required instead of silently preserving quantity/basis.
        economic_identity_fields = (
            "asset_class",
            "base_currency",
            "quote_currency",
            "settlement_currency",
            "quantity_unit",
            "contract_multiplier",
        )
        changed_economic_fields = tuple(
            field
            for field in economic_identity_fields
            if getattr(successor, field) != getattr(current, field)
        )
        if changed_economic_fields:
            raise ValueError(
                "symbol change successor changes economic identity: "
                + ", ".join(changed_economic_fields)
            )

        before = self.state
        after = replace(before, symbol=successor.provider_symbol)
        return (
            Transition(
                event_id=event.event_id,
                before=before,
                after=after,
                economic_pnl=Decimal("0"),
                reason=(
                    "instrument version advanced to registered successor; "
                    "economic position unchanged"
                ),
            ),
            successor,
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
    if state.quantity < 0 or state.borrowed_quantity != 0:
        raise ValueError(
            "long purchase helper cannot implicitly cover an existing cash-equity short"
        )
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
