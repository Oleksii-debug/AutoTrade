"""Evidence-bound margin/borrow financing charges.

No universal interest convention is assumed. Provider/model estimates remain
non-economic until a FINAL charge is observed. Later final revisions post only
their delta so restart/replay cannot double count financing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from .accounting import JournalTransaction, posting, validate_transaction


class FinancingError(ValueError):
    pass


class FinancingConflict(FinancingError):
    pass


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise FinancingError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise FinancingError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise FinancingError(f"{name} must be a finite decimal")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancingError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FinancingError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class FinancingEvent:
    charge_id: str
    revision: int
    kind: Literal["INDICATED", "FINAL"]
    effective_at: datetime
    available_at: datetime
    unit: str
    amount: Decimal
    source_account: str
    evidence_ref: str

    @classmethod
    def create(
        cls,
        *,
        charge_id: str,
        revision: int,
        kind: str,
        effective_at: datetime,
        available_at: datetime,
        unit: str,
        amount,
        source_account: str,
        evidence_ref: str,
    ) -> "FinancingEvent":
        normalized_kind = _text(kind, name="kind").upper()
        if normalized_kind not in {"INDICATED", "FINAL"}:
            raise FinancingError("kind must be INDICATED or FINAL")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise FinancingError("revision must be a positive integer")
        charge = _decimal(amount, name="amount")
        if charge < 0:
            raise FinancingError("financing charge amount must be non-negative")
        effective = _utc(effective_at, name="effective_at")
        available = _utc(available_at, name="available_at")
        if normalized_kind == "FINAL" and available < effective:
            raise FinancingError("final charge cannot be available before effective_at")
        return cls(
            charge_id=_text(charge_id, name="charge_id"),
            revision=revision,
            kind=normalized_kind,
            effective_at=effective,
            available_at=available,
            unit=_text(unit, name="unit"),
            amount=charge,
            source_account=_text(source_account, name="source_account"),
            evidence_ref=_text(evidence_ref, name="evidence_ref"),
        )

    @property
    def economic_charge(self) -> Decimal:
        return self.amount if self.kind == "FINAL" else Decimal("0")


@dataclass(frozen=True)
class FinancingUpdate:
    accepted: bool
    economic_delta: Decimal
    current_revision: int
    current_final_charge: Decimal


class FinancingRevisionBook:
    """Append-only logical revision book for provider-evidenced financing."""

    def __init__(self) -> None:
        self._latest: dict[str, FinancingEvent] = {}
        self._final_charge: dict[str, Decimal] = {}

    def latest(self, charge_id: str) -> FinancingEvent | None:
        return self._latest.get(_text(charge_id, name="charge_id"))

    def record(self, event: FinancingEvent) -> FinancingUpdate:
        if not isinstance(event, FinancingEvent):
            raise TypeError("event must be FinancingEvent")
        previous = self._latest.get(event.charge_id)
        if previous is not None:
            if event.revision < previous.revision:
                raise FinancingConflict("financing revision cannot move backwards")
            if event.revision == previous.revision:
                if event == previous:
                    return FinancingUpdate(
                        accepted=False,
                        economic_delta=Decimal("0"),
                        current_revision=previous.revision,
                        current_final_charge=self._final_charge.get(
                            event.charge_id, Decimal("0")
                        ),
                    )
                raise FinancingConflict(
                    "same financing revision has conflicting content"
                )
            if event.effective_at != previous.effective_at:
                raise FinancingConflict(
                    "financing revision cannot change effective instant"
                )
            if event.available_at < previous.available_at:
                raise FinancingConflict(
                    "financing revision cannot backdate availability"
                )
            if event.unit != previous.unit:
                raise FinancingConflict("financing revision cannot change unit")
            if event.source_account != previous.source_account:
                raise FinancingConflict(
                    "financing revision cannot change source account"
                )
            if previous.kind == "FINAL" and event.kind == "INDICATED":
                raise FinancingConflict(
                    "indicated estimate cannot supersede a final financing charge"
                )

        old_final = self._final_charge.get(event.charge_id, Decimal("0"))
        new_final = event.economic_charge if event.kind == "FINAL" else old_final
        self._latest[event.charge_id] = event
        if event.kind == "FINAL":
            self._final_charge[event.charge_id] = new_final
        return FinancingUpdate(
            accepted=True,
            economic_delta=new_final - old_final,
            current_revision=event.revision,
            current_final_charge=new_final,
        )


def book_financing_delta(
    *,
    transaction_id: str,
    cause_event_id: str,
    unit: str,
    source_account: str,
    economic_delta,
) -> JournalTransaction:
    """Book only the evidenced change in a final financing charge.

    Positive delta is an added cost: the configured source account is credited
    (negative in that unit) and FINANCING_EXPENSE is debited. A negative delta
    reverses part of a previously evidenced charge.
    """

    delta = _decimal(economic_delta, name="economic_delta")
    if delta == 0:
        raise FinancingError("zero financing delta has no economic posting")
    charge_unit = _text(unit, name="unit")
    source = _text(source_account, name="source_account")
    transaction = JournalTransaction(
        transaction_id=_text(transaction_id, name="transaction_id"),
        cause_event_id=_text(cause_event_id, name="cause_event_id"),
        postings=(
            posting(source, charge_unit, -delta),
            posting(f"FINANCING_EXPENSE:{charge_unit}", charge_unit, delta),
        ),
    )
    validate_transaction(transaction)
    return transaction
