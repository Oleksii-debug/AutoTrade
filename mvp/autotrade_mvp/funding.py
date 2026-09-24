"""Perpetual funding observations, final charges and corrections.

Indicated funding is evidence only. Economic postings are created only from a
final charged/credited event, and later revisions apply only their delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from .accounting import JournalTransaction, posting, validate_transaction


class FundingError(ValueError):
    pass


class FundingConflict(FundingError):
    pass


def _decimal(value: Decimal | str | int, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FundingError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise FundingError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise FundingError(f"{name} must be a finite decimal")
    return result


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FundingError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FundingError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def canonical_funding_cash_flow(
    *,
    signed_notional: Decimal | str | int,
    rate: Decimal | str | int,
    sign_convention: Literal["POSITIVE_LONG_PAYS", "POSITIVE_LONG_RECEIVES"],
) -> Decimal:
    """Return account cash flow in the explicitly supplied settlement unit.

    Positive signed_notional is long; negative is short.
    """

    notional = _decimal(signed_notional, "signed_notional")
    funding_rate = _decimal(rate, "rate")
    if sign_convention == "POSITIVE_LONG_PAYS":
        return -(notional * funding_rate)
    if sign_convention == "POSITIVE_LONG_RECEIVES":
        return notional * funding_rate
    raise FundingError("unsupported funding sign convention")


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
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise FundingError("revision must be a positive integer")
        if self.kind not in {"INDICATED", "FINAL"}:
            raise FundingError("kind must be INDICATED or FINAL")
        object.__setattr__(self, "effective_at", _utc(self.effective_at, "effective_at"))
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, "settlement_currency"),
        )
        object.__setattr__(
            self,
            "signed_notional",
            _decimal(self.signed_notional, "signed_notional"),
        )
        object.__setattr__(self, "rate", _decimal(self.rate, "rate"))
        if self.sign_convention not in {"POSITIVE_LONG_PAYS", "POSITIVE_LONG_RECEIVES"}:
            raise FundingError("unsupported funding sign convention")
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))

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


class FundingRevisionBook:
    """Append-only logical revision book with delta-only economic corrections."""

    def __init__(self) -> None:
        self._latest: dict[str, FundingEvent] = {}
        self._final_cash_flow: dict[str, Decimal] = {}

    def latest(self, funding_id: str) -> FundingEvent | None:
        return self._latest.get(_text(funding_id, "funding_id"))

    def record(self, event: FundingEvent) -> FundingUpdate:
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

        old_final = self._final_cash_flow.get(event.funding_id, Decimal("0"))
        new_final = event.economic_cash_flow if event.kind == "FINAL" else old_final
        # A later indicated estimate must not erase a previously evidenced final charge.
        if previous is not None and previous.kind == "FINAL" and event.kind == "INDICATED":
            raise FundingConflict("an indicated revision cannot supersede a final funding charge")

        self._latest[event.funding_id] = event
        if event.kind == "FINAL":
            self._final_cash_flow[event.funding_id] = new_final
        delta = new_final - old_final
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
    currency = _text(settlement_currency, "settlement_currency")
    transaction = JournalTransaction(
        transaction_id=_text(transaction_id, "transaction_id"),
        cause_event_id=_text(cause_event_id, "cause_event_id"),
        postings=(
            posting(f"CASH:{currency}", currency, amount),
            posting(f"FUNDING_PNL:{currency}", currency, -amount),
        ),
    )
    validate_transaction(transaction)
    return transaction
