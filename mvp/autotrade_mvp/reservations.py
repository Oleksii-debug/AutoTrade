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


    def export_state(self) -> dict[str, object]:
        """Return exact restart state without binary numeric coercion."""

        return {
            "schema_version": 1,
            "records": [
                {
                    "reservation_id": record.reservation_id,
                    "intent_id": record.intent_id,
                    "original": {
                        key: format(value, "f")
                        for key, value in sorted(record.original.items())
                    },
                    "remaining": {
                        key: format(value, "f")
                        for key, value in sorted(record.remaining.items())
                    },
                    "consumed": {
                        key: format(value, "f")
                        for key, value in sorted(record.consumed.items())
                    },
                    "state": record.state,
                    "resolution_evidence": record.resolution_evidence,
                }
                for record in sorted(
                    self._records.values(),
                    key=lambda item: item.reservation_id,
                )
            ],
        }

    @classmethod
    def restore_state(cls, payload: Mapping[str, object]) -> "ReservationBook":
        """Restore fail-closed and preserve UNKNOWN/working reservations."""

        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            raise ReservationConflict("Unsupported reservation state schema")
        rows = payload.get("records")
        if not isinstance(rows, list):
            raise ReservationConflict("Reservation state records must be a list")
        book = cls()
        seen_intents: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ReservationConflict("Reservation state record must be an object")
            required = {
                "reservation_id",
                "intent_id",
                "original",
                "remaining",
                "consumed",
                "state",
                "resolution_evidence",
            }
            if set(row) != required:
                raise ReservationConflict("Reservation state record shape is invalid")
            rid = _text(row["reservation_id"], name="reservation_id")
            iid = _text(row["intent_id"], name="intent_id")
            if rid in book._records or iid in seen_intents:
                raise ReservationConflict("Reservation restart state contains duplicate identity")
            original = _amounts(row["original"])
            remaining = _amounts(row["remaining"], allow_zero=True)
            consumed = _amounts(row["consumed"], allow_zero=True)
            if set(original) != set(remaining) or set(original) != set(consumed):
                raise ReservationConflict("Reservation restart resources do not match")
            if any(
                remaining[key] + consumed[key] != original[key]
                for key in original
            ):
                raise ReservationConflict("Reservation restart amounts violate conservation")
            state = _text(row["state"], name="state").upper()
            if state not in ACTIVE_STATES | TERMINAL_STATES:
                raise ReservationConflict("Reservation restart state is unsupported")
            evidence = row["resolution_evidence"]
            if state in ACTIVE_STATES:
                if evidence is not None:
                    raise ReservationConflict(
                        "Active reservation cannot carry terminal resolution evidence"
                    )
            else:
                evidence = _text(evidence, name="resolution_evidence")
                if any(value != 0 for value in remaining.values()):
                    raise ReservationConflict(
                        "Terminal reservation cannot retain reserved remainder"
                    )
                if state == "FILLED" and any(
                    consumed[key] != original[key] for key in original
                ):
                    raise ReservationConflict(
                        "FILLED restart state must consume the entire reservation"
                    )
                if state in {"REJECTED", "PROVEN_ABSENT"} and any(
                    value != 0 for value in consumed.values()
                ):
                    raise ReservationConflict(
                        f"{state} restart state cannot contain consumed exposure"
                    )
            book._records[rid] = ReservationSnapshot(
                reservation_id=rid,
                intent_id=iid,
                original=MappingProxyType(dict(original)),
                remaining=MappingProxyType(dict(remaining)),
                consumed=MappingProxyType(dict(consumed)),
                state=state,
                resolution_evidence=evidence,
            )
            seen_intents.add(iid)
        return book

    def active(self) -> tuple[ReservationSnapshot, ...]:
        return tuple(
            record
            for record in self._records.values()
            if record.state in ACTIVE_STATES
        )
