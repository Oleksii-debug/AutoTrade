"""Exact reservation foundation for working and ambiguous AutoTrade exposure."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .persistence import JournalStore, payload_digest


class ReservationConflict(ValueError):
    """Raised when an immutable reservation identity is reused inconsistently."""


class InsufficientAvailable(ValueError):
    """Raised when current availability cannot cover all outstanding reservations."""


TERMINAL_STATES = {"FILLED", "CANCELED", "REJECTED", "PROVEN_ABSENT"}
ACTIVE_STATES = {"WORKING", "UNKNOWN"}
_AGGREGATE_TYPE = "reservation_book"
_AGGREGATE_ID = "canonical"


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


def _amounts(
    values: Mapping[str, Decimal | str | int], *, allow_zero: bool = False
) -> dict[str, Decimal]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError("resource amounts are required")
    normalized: dict[str, Decimal] = {}
    for resource, raw in values.items():
        key = _text(resource, name="resource")
        amount = _decimal(raw, name=f"amount[{key}]")
        if amount < 0 or (amount == 0 and not allow_zero):
            raise ValueError("resource amounts must be positive")
        if key in normalized:
            raise ReservationConflict("duplicate normalized reservation resource")
        normalized[key] = amount
    return normalized


def _amount_payload(values: Mapping[str, Decimal]) -> dict[str, str]:
    return {key: str(values[key]) for key in sorted(values)}


def _event_id(event_type: str, key: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"https://events.autotrade.local/reservations/{event_type}/{key}",
        )
    )


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
    """Holds working/UNKNOWN exposure until a proven terminal outcome releases it.

    When backed by the canonical JournalStore, every mutation is persisted before
    becoming visible in memory and all reservations share one aggregate sequence.
    Competing processes therefore cannot both commit against the same reservation
    state: one append wins and the stale writer fails closed and must retry.
    """

    def __init__(self, store: JournalStore | None = None) -> None:
        if store is not None and not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore or None")
        self.store = store
        self._records: dict[str, ReservationSnapshot] = {}
        self._journal_version = 0
        self._durable_consumes: dict[
            str, tuple[dict[str, Any], ReservationSnapshot]
        ] = {}
        if self.store is not None:
            self._load_durable_state()

    def _load_durable_state(self) -> None:
        if self.store is None:
            return
        events = self.store.load_events(_AGGREGATE_TYPE, _AGGREGATE_ID)
        replay = ReservationBook()
        durable_consumes: dict[
            str, tuple[dict[str, Any], ReservationSnapshot]
        ] = {}

        for expected_version, event in enumerate(events, start=1):
            if event["aggregate_version"] != expected_version:
                raise ReservationConflict("reservation journal sequence is not contiguous")
            payload = event["payload"]
            if not isinstance(payload, dict):
                raise ReservationConflict("reservation event payload must be an object")
            event_type = event["event_type"]
            if event_type == "ReservationCreated":
                replay.reserve(
                    reservation_id=payload.get("reservation_id"),
                    intent_id=payload.get("intent_id"),
                    requirements=payload.get("requirements"),
                    available=payload.get("available"),
                )
            elif event_type == "ReservationConsumed":
                operation_id = _text(
                    payload.get("operation_id"), name="operation_id"
                )
                request = {
                    "reservation_id": _text(
                        payload.get("reservation_id"), name="reservation_id"
                    ),
                    "operation_id": operation_id,
                    "usage": payload.get("usage"),
                }
                if operation_id in durable_consumes:
                    raise ReservationConflict(
                        "durable consume operation_id is duplicated"
                    )
                result = replay.consume(
                    request["reservation_id"],
                    request["usage"],
                )
                durable_consumes[operation_id] = (request, result)
            elif event_type == "ReservationMarkedUnknown":
                replay.mark_unknown(payload.get("reservation_id"))
            elif event_type == "ReservationTerminal":
                replay.mark_terminal(
                    payload.get("reservation_id"),
                    outcome=payload.get("outcome"),
                    resolution_evidence=payload.get("resolution_evidence"),
                )
            else:
                raise ReservationConflict(
                    f"unknown durable reservation event: {event_type}"
                )

        self._records = replay._records
        self._durable_consumes = durable_consumes
        self._journal_version = len(events)

    def _synchronize(self) -> None:
        if self.store is None:
            return
        events = self.store.load_events(_AGGREGATE_TYPE, _AGGREGATE_ID)
        if len(events) != self._journal_version:
            self._load_durable_state()

    def _persist(
        self,
        *,
        event_type: str,
        key: str,
        payload: dict[str, Any],
    ) -> bool:
        if self.store is None:
            return True
        event_id = _event_id(event_type, key)
        existing = self.store.get_event(event_id)
        if existing is not None:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise ReservationConflict(
                    "durable reservation event identity conflicts with existing content"
                )
            self._load_durable_state()
            return False

        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": _AGGREGATE_ID,
            "aggregate_version": self._journal_version + 1,
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": JournalStore._now(),
        }
        try:
            result = self.store.append_event(envelope)
        except ValueError as error:
            existing = self.store.get_event(event_id)
            self._load_durable_state()
            if (
                existing is not None
                and existing["event_type"] == event_type
                and existing["payload"] == payload
            ):
                return False
            raise ReservationConflict(
                "concurrent durable reservation mutation; reload and retry"
            ) from error
        self._journal_version = result.aggregate_version
        return result.inserted

    def get(self, reservation_id: str) -> ReservationSnapshot:
        self._synchronize()
        key = _text(reservation_id, name="reservation_id")
        try:
            return self._records[key]
        except KeyError as error:
            raise KeyError(f"Unknown reservation: {key}") from error

    def total_reserved(self, resource: str) -> Decimal:
        self._synchronize()
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
        self._synchronize()
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
            already_reserved = sum(
                (
                    record.remaining.get(resource, Decimal("0"))
                    for record in self._records.values()
                    if record.state in ACTIVE_STATES
                ),
                Decimal("0"),
            )
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
        payload = {
            "reservation_id": rid,
            "intent_id": iid,
            "requirements": _amount_payload(needed),
            "available": _amount_payload(availability),
        }
        inserted = self._persist(
            event_type="ReservationCreated",
            key=rid,
            payload=payload,
        )
        if not inserted and self.store is not None:
            return self.get(rid)
        self._records[rid] = snapshot
        return snapshot

    def consume(
        self,
        reservation_id: str,
        usage: Mapping[str, Decimal | str | int],
        *,
        operation_id: str | None = None,
    ) -> ReservationSnapshot:
        self._synchronize()
        key = _text(reservation_id, name="reservation_id")
        amounts = _amounts(usage)

        durable_operation: str | None = None
        durable_request: dict[str, Any] | None = None
        if self.store is not None:
            durable_operation = _text(operation_id, name="operation_id")
            durable_request = {
                "reservation_id": key,
                "operation_id": durable_operation,
                "usage": _amount_payload(amounts),
            }
            existing_operation = self._durable_consumes.get(durable_operation)
            if existing_operation is not None:
                prior_request, prior_result = existing_operation
                if prior_request != durable_request:
                    raise ReservationConflict(
                        "operation_id was already used for different reservation consumption"
                    )
                return prior_result

        current = self.get(key)
        if current.state not in ACTIVE_STATES:
            raise ReservationConflict("Cannot consume a terminal reservation")
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

        if self.store is not None:
            assert durable_operation is not None and durable_request is not None
            inserted = self._persist(
                event_type="ReservationConsumed",
                key=durable_operation,
                payload=durable_request,
            )
            if not inserted:
                existing_operation = self._durable_consumes.get(durable_operation)
                if existing_operation is None:
                    raise ReservationConflict(
                        "durable consume retry could not recover committed result"
                    )
                return existing_operation[1]

        self._records[current.reservation_id] = updated
        if durable_operation is not None and durable_request is not None:
            self._durable_consumes[durable_operation] = (
                durable_request,
                updated,
            )
        return updated

    def mark_unknown(self, reservation_id: str) -> ReservationSnapshot:
        self._synchronize()
        current = self.get(reservation_id)
        if current.state in TERMINAL_STATES:
            raise ReservationConflict("A terminal reservation cannot become UNKNOWN")
        if current.state == "UNKNOWN":
            return current
        payload = {"reservation_id": current.reservation_id}
        inserted = self._persist(
            event_type="ReservationMarkedUnknown",
            key=current.reservation_id,
            payload=payload,
        )
        if not inserted and self.store is not None:
            return self.get(current.reservation_id)
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
        self._synchronize()
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

        payload = {
            "reservation_id": current.reservation_id,
            "outcome": normalized,
            "resolution_evidence": evidence,
        }
        inserted = self._persist(
            event_type="ReservationTerminal",
            key=current.reservation_id,
            payload=payload,
        )
        if not inserted and self.store is not None:
            return self.get(current.reservation_id)
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
        self._synchronize()
        return tuple(
            record
            for record in self._records.values()
            if record.state in ACTIVE_STATES
        )
