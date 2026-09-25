"""Exact reservation foundation for working and ambiguous AutoTrade exposure."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping


class ReservationConflict(ValueError):
    """Raised when an immutable reservation identity is reused inconsistently."""


class InsufficientAvailable(ValueError):
    """Raised when current availability cannot cover all outstanding reservations."""


TERMINAL_STATES = {"FILLED", "CANCELED", "REJECTED", "PROVEN_ABSENT"}
ACTIVE_STATES = {"WORKING", "UNKNOWN"}


def _decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _amounts(values: Mapping[str, Decimal | str | int], *, allow_zero: bool = False) -> dict[str, Decimal]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError("resource amounts are required")
    normalized: dict[str, Decimal] = {}
    for resource, raw in values.items():
        key = _text(resource, name="resource")
        if key in normalized:
            raise ValueError(
                f"duplicate normalized resource identity: {key}"
            )
        if key in normalized:
            raise ValueError("resource names must be unique after normalization")
        amount = _decimal(raw, name=f"amount[{key}]")
        if amount < 0 or (amount == 0 and not allow_zero):
            raise ValueError("resource amounts must be positive")
        normalized[key] = amount
    return normalized


@dataclass(frozen=True)
class ReservationSnapshot:
    reservation_id: str
    intent_id: str
    original: Mapping[str, Decimal]
    remaining: Mapping[str, Decimal]
    consumed: Mapping[str, Decimal]
    state: str
    resolution_evidence: str | None = None


class ReservationBook:
    """Holds working/UNKNOWN exposure until a proven terminal outcome releases it."""

    def __init__(self) -> None:
        self._records: dict[str, ReservationSnapshot] = {}

    def get(self, reservation_id: str) -> ReservationSnapshot:
        key = _text(reservation_id, name="reservation_id")
        try:
            return self._records[key]
        except KeyError as error:
            raise KeyError(f"Unknown reservation: {key}") from error

    def total_reserved(self, resource: str) -> Decimal:
        key = _text(resource, name="resource")
        return sum(
            (
                record.remaining.get(key, Decimal("0"))
                for record in self._records.values()
                if record.state in ACTIVE_STATES
            ),
            Decimal("0"),
        )

    def reserve(
        self,
        *,
        reservation_id: str,
        intent_id: str,
        requirements: Mapping[str, Decimal | str | int],
        available: Mapping[str, Decimal | str | int],
    ) -> ReservationSnapshot:
        rid = _text(reservation_id, name="reservation_id")
        iid = _text(intent_id, name="intent_id")
        needed = _amounts(requirements)
        availability = _amounts(available, allow_zero=True)

        existing = self._records.get(rid)
        if existing is not None:
            if existing.intent_id != iid or existing.original != needed:
                raise ReservationConflict(
                    "reservation_id was already committed with different content"
                )
            return existing

        if any(record.intent_id == iid for record in self._records.values()):
            raise ReservationConflict(
                "intent_id already has a different reservation"
            )

        for resource, amount in needed.items():
            if resource not in availability:
                raise InsufficientAvailable(f"No availability evidence for {resource}")
            already_reserved = self.total_reserved(resource)
            if already_reserved + amount > availability[resource]:
                raise InsufficientAvailable(
                    f"Insufficient {resource}: available={availability[resource]}, "
                    f"reserved={already_reserved}, requested={amount}"
                )

        snapshot = ReservationSnapshot(
            reservation_id=rid,
            intent_id=iid,
            original=MappingProxyType(dict(needed)),
            remaining=MappingProxyType(dict(needed)),
            consumed=MappingProxyType(
                {resource: Decimal("0") for resource in needed}
            ),
            state="WORKING",
        )
        self._records[rid] = snapshot
        return snapshot

    def consume(
        self,
        reservation_id: str,
        usage: Mapping[str, Decimal | str | int],
    ) -> ReservationSnapshot:
        current = self.get(reservation_id)
        if current.state not in ACTIVE_STATES:
            raise ReservationConflict("Cannot consume a terminal reservation")
        amounts = _amounts(usage)
        remaining = dict(current.remaining)
        consumed = dict(current.consumed)
        for resource, amount in amounts.items():
            if resource not in remaining:
                raise ReservationConflict(f"Resource {resource} was not reserved")
            if amount > remaining[resource]:
                raise ReservationConflict(
                    f"Consumption exceeds remaining reservation for {resource}"
                )
            remaining[resource] -= amount
            consumed[resource] += amount
        updated = ReservationSnapshot(
            reservation_id=current.reservation_id,
            intent_id=current.intent_id,
            original=current.original,
            remaining=MappingProxyType(remaining),
            consumed=MappingProxyType(consumed),
            state=current.state,
            resolution_evidence=current.resolution_evidence,
        )
        self._records[current.reservation_id] = updated
        return updated

    def mark_unknown(self, reservation_id: str) -> ReservationSnapshot:
        current = self.get(reservation_id)
        if current.state in TERMINAL_STATES:
            raise ReservationConflict("A terminal reservation cannot become UNKNOWN")
        if current.state == "UNKNOWN":
            return current
        updated = ReservationSnapshot(
            reservation_id=current.reservation_id,
            intent_id=current.intent_id,
            original=current.original,
            remaining=current.remaining,
            consumed=current.consumed,
            state="UNKNOWN",
        )
        self._records[current.reservation_id] = updated
        return updated

    def mark_terminal(
        self,
        reservation_id: str,
        *,
        outcome: str,
        resolution_evidence: str,
    ) -> ReservationSnapshot:
        current = self.get(reservation_id)
        normalized = _text(outcome, name="outcome").upper()
        if normalized not in TERMINAL_STATES:
            raise ValueError(f"Unsupported terminal outcome: {normalized}")
        evidence = _text(resolution_evidence, name="resolution_evidence")
        any_consumed = any(amount != 0 for amount in current.consumed.values())
        any_remaining = any(amount != 0 for amount in current.remaining.values())
        if normalized == "FILLED" and any_remaining:
            raise ReservationConflict(
                "FILLED cannot release an unconsumed reservation remainder"
            )
        if normalized in {"REJECTED", "PROVEN_ABSENT"} and any_consumed:
            raise ReservationConflict(
                f"{normalized} cannot erase already consumed exposure"
            )
        if current.state in TERMINAL_STATES:
            if current.state != normalized or current.resolution_evidence != evidence:
                raise ReservationConflict(
                    "Terminal reservation cannot be resolved differently"
                )
            return current
        updated = ReservationSnapshot(
            reservation_id=current.reservation_id,
            intent_id=current.intent_id,
            original=current.original,
            remaining=MappingProxyType(
                {resource: Decimal("0") for resource in current.remaining}
            ),
            consumed=current.consumed,
            state=normalized,
            resolution_evidence=evidence,
        )
        self._records[current.reservation_id] = updated
        return updated

    def active(self) -> tuple[ReservationSnapshot, ...]:
        return tuple(
            record
            for record in self._records.values()
            if record.state in ACTIVE_STATES
        )
