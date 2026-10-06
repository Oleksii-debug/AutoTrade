"""Exact reservation foundation for working and ambiguous AutoTrade exposure."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Protocol

from .exact_decimal import (
    ExactDecimalError,
    exact_add,
    exact_subtract,
    exact_sum,
    parse_bounded_exact_decimal,
)


class CapitalAvailabilityEvidence(Protocol):
    """Explicit available-capital projection accepted by reservation admission."""

    blocks_new_risk: bool

    def reservation_resources(self) -> dict[str, Decimal]:
        ...


class ReservationConflict(ValueError):
    """Raised when an immutable reservation identity is reused inconsistently."""


class InsufficientAvailable(ValueError):
    """Raised when current availability cannot cover all outstanding reservations."""


TERMINAL_STATES = frozenset({"FILLED", "CANCELED", "REJECTED", "PROVEN_ABSENT"})
ACTIVE_STATES = frozenset({"WORKING", "UNKNOWN"})
POST_BUST_HOLD_STATE = "BUSTED_PENDING_RECONCILIATION"
HELD_STATES = ACTIVE_STATES | frozenset({POST_BUST_HOLD_STATE})


def _decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(
            f"{name} must be an exact finite decimal within the resource envelope"
        ) from error


def _text(value: str, *, name: str) -> str:
    # Reservation identifiers and resource names are financial authority.
    # Reject polymorphic text before caller-controlled strip/normalization code
    # can execute while an immutable reservation identity is being derived.
    if type(value) is not str:
        raise ValueError(f"{name} is required")
    normalized = str.strip(value)
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _amounts(values: dict[str, Decimal | str | int], *, allow_zero: bool = False) -> dict[str, Decimal]:
    # Reservation admission is hard financial authority. Arbitrary Mapping
    # implementations (including MappingProxyType over an executable backing
    # mapping) must not run callbacks while capacity is being normalized.
    if type(values) is not dict:
        raise TypeError("resource amounts must use an exact dict")
    items = tuple(dict.items(values))
    if not items:
        raise ValueError("resource amounts are required")
    normalized: dict[str, Decimal] = {}
    for resource, raw in items:
        key = _text(resource, name="resource")
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
    """Holds working, ambiguous and post-bust unresolved exposure."""

    def __init__(self) -> None:
        self._records: dict[str, ReservationSnapshot] = {}

    @staticmethod
    def _detached_snapshot(record: ReservationSnapshot) -> ReservationSnapshot:
        if type(record) is not ReservationSnapshot:
            raise TypeError("reservation record must be exact ReservationSnapshot")
        return ReservationSnapshot(
            reservation_id=record.reservation_id,
            intent_id=record.intent_id,
            original=MappingProxyType(dict(record.original)),
            remaining=MappingProxyType(dict(record.remaining)),
            consumed=MappingProxyType(dict(record.consumed)),
            state=record.state,
            resolution_evidence=record.resolution_evidence,
        )

    def _get_record(self, reservation_id: str) -> ReservationSnapshot:
        key = _text(reservation_id, name="reservation_id")
        try:
            return self._records[key]
        except KeyError as error:
            raise KeyError(f"Unknown reservation: {key}") from error

    def get(self, reservation_id: str) -> ReservationSnapshot:
        return self._detached_snapshot(self._get_record(reservation_id))

    def total_reserved(self, resource: str) -> Decimal:
        key = _text(resource, name="resource")
        try:
            return exact_sum(
                record.remaining.get(key, Decimal("0"))
                for record in self._records.values()
                if record.state in HELD_STATES
            )
        except ExactDecimalError as error:
            raise ReservationConflict(
                "reserved total exceeds exact decimal authority"
            ) from error

    def reserve(
        self,
        *,
        reservation_id: str,
        intent_id: str,
        requirements: dict[str, Decimal | str | int],
        available: dict[str, Decimal | str | int],
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
            return self._detached_snapshot(existing)

        if any(record.intent_id == iid for record in self._records.values()):
            raise ReservationConflict(
                "intent_id already has a different reservation"
            )

        for resource, amount in needed.items():
            if resource not in availability:
                raise InsufficientAvailable(f"No availability evidence for {resource}")
            already_reserved = self.total_reserved(resource)
            try:
                projected_reserved = exact_add(already_reserved, amount)
            except ExactDecimalError as error:
                raise ReservationConflict(
                    "reservation admission exceeds exact decimal authority"
                ) from error
            if projected_reserved > availability[resource]:
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
        return self._detached_snapshot(snapshot)

    def reserve_from_capital(
        self,
        *,
        reservation_id: str,
        intent_id: str,
        requirements: dict[str, Decimal | str | int],
        capital: CapitalAvailabilityEvidence,
    ) -> ReservationSnapshot:
        """Reserve only from an explicit, non-blocking capital projection."""

        if not hasattr(capital, "reservation_resources"):
            raise TypeError(
                "capital must provide explicit reservation_resources()"
            )
        if getattr(capital, "blocks_new_risk", True):
            raise InsufficientAvailable(
                "capital projection is unresolved and blocks new risk"
            )
        available = capital.reservation_resources()
        if type(available) is not dict:
            raise TypeError(
                "capital reservation_resources() must return an exact dict"
            )
        return self.reserve(
            reservation_id=reservation_id,
            intent_id=intent_id,
            requirements=requirements,
            available=available,
        )

    def consume(
        self,
        reservation_id: str,
        usage: dict[str, Decimal | str | int],
    ) -> ReservationSnapshot:
        current = self._get_record(reservation_id)
        if current.state not in HELD_STATES:
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
            try:
                next_remaining = exact_subtract(remaining[resource], amount)
                next_consumed = exact_add(consumed[resource], amount)
            except ExactDecimalError as error:
                raise ReservationConflict(
                    "reservation consumption exceeds exact decimal authority"
                ) from error
            remaining[resource] = next_remaining
            consumed[resource] = next_consumed
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
        return self._detached_snapshot(updated)

    def consume_and_mark_filled(
        self,
        reservation_id: str,
        usage: dict[str, Decimal | str | int],
        *,
        resolution_evidence: str,
    ) -> ReservationSnapshot:
        """Atomically consume one fill cut and release a proven terminal remainder.

        This projection primitive deliberately does not decide whether an order
        is fully filled. Its caller must already possess canonical terminal OMS
        evidence. The method only guarantees that the local financial state
        cannot expose an intermediate consumed-but-not-terminal cut: validation,
        exact consumption and FILLED terminalization either all succeed or the
        reservation remains unchanged.
        """

        current = self._get_record(reservation_id)
        if current.state not in HELD_STATES:
            raise ReservationConflict(
                "Cannot consume and terminalize a terminal reservation"
            )
        evidence = _text(resolution_evidence, name="resolution_evidence")
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
            try:
                remaining[resource] = exact_subtract(remaining[resource], amount)
                consumed[resource] = exact_add(consumed[resource], amount)
            except ExactDecimalError as error:
                raise ReservationConflict(
                    "reservation consumption exceeds exact decimal authority"
                ) from error

        updated = ReservationSnapshot(
            reservation_id=current.reservation_id,
            intent_id=current.intent_id,
            original=current.original,
            remaining=MappingProxyType(
                {resource: Decimal("0") for resource in current.remaining}
            ),
            consumed=MappingProxyType(consumed),
            state="FILLED",
            resolution_evidence=evidence,
        )
        self._records[current.reservation_id] = updated
        return self._detached_snapshot(updated)

    def restore_consumption(
        self,
        reservation_id: str,
        usage: dict[str, Decimal | str | int],
    ) -> ReservationSnapshot:
        """Apply a fill reversal without inventing or releasing capacity.

        For an unresolved reservation this is the exact inverse of ``consume``.
        A provider bust may also invalidate a previously terminal FILLED cut.
        In that case the prior terminal result is not relabelled WORKING or
        UNKNOWN: the reservation enters a dedicated post-bust hold state and
        reconstitutes every resource to ``original - still_consumed``.

        A late bust after provider-confirmed CANCELED is different: the order
        remainder is already terminal and carries no live execution risk. The
        reversal therefore reduces historical consumed exposure but keeps the
        reservation released (remaining stays zero and CANCELED is preserved).
        The durable caller binds either projection to the matching OMS bust and
        economic reversal in one JournalStore command.
        """

        current = self._get_record(reservation_id)
        terminal_filled_bust = current.state == "FILLED"
        terminal_cancelled_bust = current.state == "CANCELED"
        if (
            current.state not in HELD_STATES
            and not terminal_filled_bust
            and not terminal_cancelled_bust
        ):
            raise ReservationConflict(
                "Cannot restore consumption on a terminal reservation"
            )
        amounts = _amounts(usage)
        remaining = dict(current.remaining)
        consumed = dict(current.consumed)
        for resource, amount in amounts.items():
            if resource not in consumed or resource not in current.original:
                raise ReservationConflict(f"Resource {resource} was not reserved")
            if amount > consumed[resource]:
                raise ReservationConflict(
                    f"Restoration exceeds consumed reservation for {resource}"
                )
            try:
                next_consumed = exact_subtract(consumed[resource], amount)
                next_remaining = (
                    remaining[resource]
                    if terminal_cancelled_bust
                    else exact_add(remaining[resource], amount)
                )
            except ExactDecimalError as error:
                raise ReservationConflict(
                    "reservation restoration exceeds exact decimal authority"
                ) from error
            if next_remaining > current.original[resource]:
                raise ReservationConflict(
                    f"Restoration exceeds original reservation for {resource}"
                )
            remaining[resource] = next_remaining
            consumed[resource] = next_consumed

        state = current.state
        resolution_evidence = current.resolution_evidence
        if terminal_filled_bust:
            # FILLED terminalization zeros all remaining capacity. A later
            # provider bust reopens risk, so restoring only the busted fill's
            # usage would lose the previously released unused buffer. Rebuild
            # the held cut from the immutable original minus still-consumed
            # exposure for every resource.
            rebuilt_remaining: dict[str, Decimal] = {}
            for resource, original in current.original.items():
                if resource not in consumed:
                    raise ReservationConflict(
                        f"Consumed authority is missing reserved resource {resource}"
                    )
                try:
                    rebuilt = exact_subtract(original, consumed[resource])
                except ExactDecimalError as error:
                    raise ReservationConflict(
                        "post-bust reservation rebuild exceeds exact decimal authority"
                    ) from error
                if rebuilt < 0:
                    raise ReservationConflict(
                        f"Consumed reservation exceeds original for {resource}"
                    )
                rebuilt_remaining[resource] = rebuilt
            remaining = rebuilt_remaining
            state = POST_BUST_HOLD_STATE
            # The old terminal evidence remains immutable in journal history,
            # but it no longer describes the current unresolved reservation cut.
            resolution_evidence = None

        updated = ReservationSnapshot(
            reservation_id=current.reservation_id,
            intent_id=current.intent_id,
            original=current.original,
            remaining=MappingProxyType(remaining),
            consumed=MappingProxyType(consumed),
            state=state,
            resolution_evidence=resolution_evidence,
        )
        self._records[current.reservation_id] = updated
        return self._detached_snapshot(updated)

    def mark_unknown(self, reservation_id: str) -> ReservationSnapshot:
        current = self._get_record(reservation_id)
        if current.state in TERMINAL_STATES:
            raise ReservationConflict("A terminal reservation cannot become UNKNOWN")
        if current.state == POST_BUST_HOLD_STATE:
            raise ReservationConflict(
                "A post-bust reservation requires canonical reconciliation before UNKNOWN"
            )
        if current.state == "UNKNOWN":
            return self._detached_snapshot(current)
        updated = ReservationSnapshot(
            reservation_id=current.reservation_id,
            intent_id=current.intent_id,
            original=current.original,
            remaining=current.remaining,
            consumed=current.consumed,
            state="UNKNOWN",
        )
        self._records[current.reservation_id] = updated
        return self._detached_snapshot(updated)

    def mark_terminal(
        self,
        reservation_id: str,
        *,
        outcome: str,
        resolution_evidence: str,
    ) -> ReservationSnapshot:
        current = self._get_record(reservation_id)
        normalized = _text(outcome, name="outcome").upper()
        if normalized not in TERMINAL_STATES:
            raise ValueError(f"Unsupported terminal outcome: {normalized}")
        evidence = _text(resolution_evidence, name="resolution_evidence")
        any_consumed = any(amount != 0 for amount in current.consumed.values())
        if normalized in {"REJECTED", "PROVEN_ABSENT"} and any_consumed:
            raise ReservationConflict(
                f"{normalized} cannot erase already consumed exposure"
            )
        if current.state in TERMINAL_STATES:
            if current.state != normalized or current.resolution_evidence != evidence:
                raise ReservationConflict(
                    "Terminal reservation cannot be resolved differently"
                )
            return self._detached_snapshot(current)
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
        return self._detached_snapshot(updated)

    def active(self) -> tuple[ReservationSnapshot, ...]:
        return tuple(
            self._detached_snapshot(record)
            for record in self._records.values()
            if record.state in HELD_STATES
        )
