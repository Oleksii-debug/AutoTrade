"""Perpetual funding observations, final charges and corrections.

Indicated funding is evidence only. Economic postings are created only from a
final charged/credited event, and later revisions apply only their delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Literal

from .accounting import JournalTransaction, posting, validate_transaction
from .exact_decimal import (
    ExactDecimalError,
    parse_bounded_exact_decimal,
    exact_multiply,
    exact_subtract,
)


class FundingError(ValueError):
    pass


class FundingConflict(FundingError):
    pass


def _decimal(value: Decimal | str | int, name: str) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        if isinstance(value, Decimal):
            raise FundingError(f"{name} must use an exact built-in Decimal")
        raise FundingError(
            f"{name} must use exact built-in Decimal, string or integer input"
        )
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise FundingError(
            f"{name} exceeds exact arithmetic resource envelope"
        ) from error


def _text(value: str, name: str) -> str:
    # Authority-bearing strings are retained by FundingEvent and later compared
    # for revision identity. Reject str subclasses before strip/upper/equality
    # can dispatch through caller-controlled methods.
    if type(value) is not str or not value.strip():
        raise FundingError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, name: str) -> datetime:
    # Funding chronology is durable identity. Seal both the outer datetime and
    # nested timezone implementation before any timezone callback can execute.
    if type(value) is not datetime or value.tzinfo is None:
        raise FundingError(f"{name} must be timezone-aware")
    if type(value.tzinfo) is not timezone:
        raise FundingError(
            f"{name} must use an exact datetime with built-in timezone"
        )
    return datetime.astimezone(value, timezone.utc)


def _sign_convention(
    value: Literal["POSITIVE_LONG_PAYS", "POSITIVE_LONG_RECEIVES"],
) -> Literal["POSITIVE_LONG_PAYS", "POSITIVE_LONG_RECEIVES"]:
    if type(value) is not str or value not in {
        "POSITIVE_LONG_PAYS",
        "POSITIVE_LONG_RECEIVES",
    }:
        raise FundingError("unsupported funding sign convention")
    return value


def canonical_funding_cash_flow(
    *,
    signed_notional: Decimal | str | int,
    rate: Decimal | str | int,
    sign_convention: Literal["POSITIVE_LONG_PAYS", "POSITIVE_LONG_RECEIVES"],
) -> Decimal:
    """Return account cash flow in the explicitly supplied settlement unit.

    Positive signed_notional is long; negative is short. Economic arithmetic is
    context-independent and consumes the shared bounded exact-decimal authority.
    """

    notional = _decimal(signed_notional, "signed_notional")
    funding_rate = _decimal(rate, "rate")
    convention = _sign_convention(sign_convention)
    try:
        cash_flow = exact_multiply(notional, funding_rate)
        if convention == "POSITIVE_LONG_PAYS":
            return exact_subtract(Decimal("0"), cash_flow)
        return cash_flow
    except ExactDecimalError as error:
        raise FundingError(
            "funding cash flow exceeds exact arithmetic resource envelope"
        ) from error


@dataclass(frozen=True)
class FundingEvent:
    funding_id: str
    revision: int
    kind: Literal["INDICATED", "FINAL"]
    effective_at: datetime
    available_at: datetime
    settlement_currency: str
    signed_notional: Decimal
    rate: Decimal
    sign_convention: Literal["POSITIVE_LONG_PAYS", "POSITIVE_LONG_RECEIVES"]
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "funding_id", _text(self.funding_id, "funding_id"))
        if type(self.revision) is not int or self.revision < 1:
            raise FundingError("revision must be a positive integer")
        if type(self.kind) is not str or self.kind not in {"INDICATED", "FINAL"}:
            raise FundingError("kind must be INDICATED or FINAL")
        object.__setattr__(self, "effective_at", _utc(self.effective_at, "effective_at"))
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, "settlement_currency").upper(),
        )
        object.__setattr__(
            self,
            "signed_notional",
            _decimal(self.signed_notional, "signed_notional"),
        )
        object.__setattr__(self, "rate", _decimal(self.rate, "rate"))
        object.__setattr__(
            self,
            "sign_convention",
            _sign_convention(self.sign_convention),
        )
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))
        if self.kind == "FINAL" and self.available_at < self.effective_at:
            raise FundingError("final funding cannot be available before its effective instant")

    @property
    def economic_cash_flow(self) -> Decimal:
        if self.kind != "FINAL":
            return Decimal("0")
        return canonical_funding_cash_flow(
            signed_notional=self.signed_notional,
            rate=self.rate,
            sign_convention=self.sign_convention,
        )


@dataclass(frozen=True)
class FundingUpdate:
    accepted: bool
    economic_delta: Decimal
    current_revision: int
    current_final_cash_flow: Decimal


def _snapshot_funding_event(value: FundingEvent) -> FundingEvent:
    """Return a revalidated, internally owned exact scalar snapshot.

    Copy on ingress AND on export: Python frozen dataclasses can be changed by
    object.__setattr__ if a caller retains an object reference.
    """
    if type(value) is not FundingEvent:
        raise TypeError("event must be exact FundingEvent")
    return FundingEvent(
        funding_id=value.funding_id,
        revision=value.revision,
        kind=value.kind,
        effective_at=value.effective_at,
        available_at=value.available_at,
        settlement_currency=value.settlement_currency,
        signed_notional=value.signed_notional,
        rate=value.rate,
        sign_convention=value.sign_convention,
        evidence_ref=value.evidence_ref,
    )


class FundingRevisionBook:
    """Append-only logical revision book with delta-only economic corrections."""

    def __init__(self, history: Iterable[FundingEvent] = ()) -> None:
        self._latest: dict[str, FundingEvent] = {}
        self._final_cash_flow: dict[str, Decimal] = {}
        self._history: list[FundingEvent] = []
        for event in history:
            self.record(event)

    @property
    def events(self) -> tuple[FundingEvent, ...]:
        """Immutable accepted revision history for durable replay after restart."""

        return tuple(_snapshot_funding_event(item) for item in self._history)

    def latest(self, funding_id: str) -> FundingEvent | None:
        retained = self._latest.get(_text(funding_id, "funding_id"))
        return None if retained is None else _snapshot_funding_event(retained)

    def record(self, event: FundingEvent) -> FundingUpdate:
        # FundingEvent is subclassable and economic_cash_flow is a virtual
        # property. Terminal revision admission therefore accepts only the exact
        # canonical snapshot type before any semantic/economic member is read.
        event = _snapshot_funding_event(event)
        previous = self._latest.get(event.funding_id)
        if previous is not None:
            if event.revision < previous.revision:
                raise FundingConflict("funding revision cannot move backwards")
            if event.revision == previous.revision:
                if event == previous:
                    return FundingUpdate(
                        accepted=False,
                        economic_delta=Decimal("0"),
                        current_revision=previous.revision,
                        current_final_cash_flow=self._final_cash_flow.get(
                            event.funding_id, Decimal("0")
                        ),
                    )
                raise FundingConflict("same funding revision has conflicting content")
            if event.settlement_currency != previous.settlement_currency:
                raise FundingConflict("funding revision cannot change settlement currency")
            if event.effective_at != previous.effective_at:
                raise FundingConflict("funding revision cannot change effective instant")
            if event.available_at < previous.available_at:
                raise FundingConflict("funding revision cannot backdate availability")
            if event.sign_convention != previous.sign_convention:
                raise FundingConflict("funding revision cannot change sign convention")

        old_final = self._final_cash_flow.get(event.funding_id, Decimal("0"))
        # A later indicated estimate must not erase a previously evidenced final charge.
        if previous is not None and previous.kind == "FINAL" and event.kind == "INDICATED":
            raise FundingConflict("an indicated revision cannot supersede a final funding charge")
        if event.kind == "FINAL":
            # Recompute from the accepted scalar snapshot rather than invoking
            # the overridable convenience property. This keeps one deterministic
            # source for financial authority even if callers inspect the property.
            new_final = canonical_funding_cash_flow(
                signed_notional=event.signed_notional,
                rate=event.rate,
                sign_convention=event.sign_convention,
            )
        else:
            new_final = old_final
        try:
            delta = exact_subtract(new_final, old_final)
        except ExactDecimalError as error:
            # Exact arithmetic failure must occur before any revision state mutates;
            # otherwise restart/retry could observe a revision whose economic delta
            # was never authoritatively established.
            raise FundingError(
                "funding revision delta exceeds exact arithmetic resource envelope"
            ) from error

        self._latest[event.funding_id] = event
        if event.kind == "FINAL":
            self._final_cash_flow[event.funding_id] = new_final
        self._history.append(event)
        return FundingUpdate(
            accepted=True,
            economic_delta=delta,
            current_revision=event.revision,
            current_final_cash_flow=new_final,
        )


def book_funding_delta(
    *,
    transaction_id: str,
    cause_event_id: str,
    settlement_currency: str,
    economic_delta: Decimal | str | int,
) -> JournalTransaction:
    amount = _decimal(economic_delta, "economic_delta")
    if amount == 0:
        raise FundingError("zero funding delta has no economic posting")
    currency = _text(settlement_currency, "settlement_currency").upper()
    try:
        balancing_amount = exact_subtract(Decimal("0"), amount)
    except ExactDecimalError as error:
        raise FundingError(
            "funding posting exceeds exact arithmetic resource envelope"
        ) from error
    transaction = JournalTransaction(
        transaction_id=_text(transaction_id, "transaction_id"),
        cause_event_id=_text(cause_event_id, "cause_event_id"),
        postings=(
            posting(f"CASH:{currency}", currency, amount),
            posting(f"FUNDING_PNL:{currency}", currency, balancing_amount),
        ),
    )
    validate_transaction(transaction)
    return transaction
