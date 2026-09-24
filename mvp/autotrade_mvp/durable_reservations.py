"""Durable journal-backed projection for reservation authority.

This module reuses the canonical JournalStore; it does not create a second
persistence database.  Every reservation mutation is first committed as one
versioned journal event, then the in-memory ReservationBook is rebuilt from
that durable history.  A crash between journal commit and process projection is
therefore recovered by replay, while a stale concurrent writer fails the
JournalStore aggregate-version check.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping
from uuid import UUID, NAMESPACE_URL, uuid5

from .persistence import JournalStore, payload_digest
from .reservations import (
    ReservationBook,
    ReservationConflict,
    ReservationSnapshot,
)


_AGGREGATE_TYPE = "reservation_book"
_EVENT_TYPE = "ReservationMutationCommitted"


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()



def _immutable_evidence_ref(value: str) -> str:
    reference = _text(value, name="resolution_evidence")
    marker = "@sha256:"
    if not reference.startswith("artifact:") or marker not in reference:
        raise ValueError(
            "resolution_evidence must bind an immutable artifact and SHA-256 digest"
        )
    artifact_id, digest = reference[len("artifact:"):].split(marker, 1)
    try:
        artifact_id = str(UUID(artifact_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(
            "resolution_evidence artifact identity must be a UUID"
        ) from error
    if len(digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in digest
    ):
        raise ValueError(
            "resolution_evidence must use canonical lowercase SHA-256"
        )
    return f"artifact:{artifact_id}@sha256:{digest}"

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


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _amount_map(values: Mapping[str, object], *, allow_zero: bool) -> dict[str, str]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError("resource amounts are required")
    result: dict[str, str] = {}
    for resource, raw in values.items():
        key = _text(resource, name="resource")
        if key in result:
            raise ValueError("resource names must be unique after normalization")
        amount = _decimal(raw, name=f"amount[{key}]")
        if amount < 0 or (amount == 0 and not allow_zero):
            raise ValueError("resource amounts must be positive")
        result[key] = _decimal_text(amount)
    return dict(sorted(result.items()))


def _snapshot_payload(snapshot: ReservationSnapshot) -> dict[str, object]:
    return {
        "reservation_id": snapshot.reservation_id,
        "intent_id": snapshot.intent_id,
        "original": {
            key: _decimal_text(value)
            for key, value in sorted(snapshot.original.items())
        },
        "remaining": {
            key: _decimal_text(value)
            for key, value in sorted(snapshot.remaining.items())
        },
        "consumed": {
            key: _decimal_text(value)
            for key, value in sorted(snapshot.consumed.items())
        },
        "state": snapshot.state,
        "resolution_evidence": snapshot.resolution_evidence,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _journal_identity(account_id: str, kind: str, external_id: str) -> str:
    """Scope generic JournalStore identities to one reservation account."""

    account = _text(account_id, name="account_id")
    identity_kind = _text(kind, name="identity_kind")
    external = _text(external_id, name="external_id")
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://reservations.autotrade.local/"
            f"{identity_kind}/{account!r}/{external!r}",
        )
    )


class DurableReservationBook:
    """ReservationBook projection with crash/restart and dedupe semantics."""

    def __init__(self, store: JournalStore, *, account_id: str):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.account_id = _text(account_id, name="account_id")
        self._book = ReservationBook()
        self._idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        self._reload()

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.account_id)

    def _replay(
        self,
        events: list[dict[str, object]],
    ) -> tuple[
        ReservationBook,
        dict[str, tuple[str, dict[str, object]]],
    ]:
        book = ReservationBook()
        idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        expected_version = 1

        for event in events:
            if event["aggregate_version"] != expected_version:
                raise ReservationConflict(
                    "reservation journal aggregate versions are not contiguous"
                )
            expected_version += 1
            if event["event_type"] != _EVENT_TYPE:
                raise ReservationConflict(
                    "reservation journal contains an unsupported event type"
                )
            if payload_digest(event["payload"]) != event["payload_hash"]:
                raise ReservationConflict(
                    "reservation journal payload hash does not match stored payload"
                )
            payload = event["payload"]
            if not isinstance(payload, dict):
                raise ReservationConflict("reservation event payload must be an object")
            operation = payload.get("operation")
            request = payload.get("request")
            expected_snapshot = payload.get("snapshot")
            idem = payload.get("idempotency_key")
            request_hash = payload.get("request_hash")
            if not isinstance(request, dict):
                raise ReservationConflict("reservation event request must be an object")
            if not isinstance(expected_snapshot, dict):
                raise ReservationConflict("reservation event snapshot must be an object")
            idem = _text(idem, name="idempotency_key")
            request_hash = _text(request_hash, name="request_hash")
            if request_hash != payload_digest(request):
                raise ReservationConflict(
                    "reservation event request hash does not match request"
                )
            prior = idempotency.get(idem)
            if prior is not None:
                if prior[0] != request_hash:
                    raise ReservationConflict(
                        "reservation idempotency key has conflicting journal requests"
                    )
                raise ReservationConflict(
                    "duplicate reservation idempotency event must not be appended"
                )

            try:
                snapshot = self._apply(book, operation, request)
            except Exception as error:
                raise ReservationConflict(
                    f"reservation journal cannot be replayed at version "
                    f"{event['aggregate_version']}: {error}"
                ) from error
            actual = _snapshot_payload(snapshot)
            if actual != expected_snapshot:
                raise ReservationConflict(
                    "reservation journal snapshot does not match replayed state"
                )
            idempotency[idem] = (request_hash, actual)

        return book, idempotency

    @staticmethod
    def _apply(
        book: ReservationBook,
        operation: object,
        request: dict[str, object],
    ) -> ReservationSnapshot:
        if operation == "RESERVE":
            return book.reserve(
                reservation_id=request["reservation_id"],
                intent_id=request["intent_id"],
                requirements=request["requirements"],
                available=request["available"],
            )
        if operation == "CONSUME":
            return book.consume(
                request["reservation_id"],
                request["usage"],
            )
        if operation == "MARK_UNKNOWN":
            return book.mark_unknown(request["reservation_id"])
        if operation == "MARK_TERMINAL":
            return book.mark_terminal(
                request["reservation_id"],
                outcome=request["outcome"],
                resolution_evidence=request["resolution_evidence"],
            )
        raise ReservationConflict(f"unsupported reservation operation: {operation}")

    def _reload(self) -> None:
        self._book, self._idempotency = self._replay(self._events())

    def _existing(
        self,
        *,
        idempotency_key: str,
        request: dict[str, object],
    ) -> dict[str, object] | None:
        key = _text(idempotency_key, name="idempotency_key")
        existing = self._idempotency.get(key)
        if existing is None:
            return None
        current_hash = payload_digest(request)
        if existing[0] != current_hash:
            raise ReservationConflict(
                "idempotency_key was already used for a different reservation request"
            )
        return existing[1]

    def _commit(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        operation: str,
        request: dict[str, object],
    ) -> ReservationSnapshot:
        cid = _text(command_id, name="command_id")
        idem = _text(idempotency_key, name="idempotency_key")

        # Read one journal snapshot for idempotency, financial availability
        # and aggregate version. A second read here would allow a concurrent
        # identical command to appear between dedupe and candidate replay,
        # causing the same economic mutation to be applied twice locally before
        # commit_command can return its durable idempotent result.
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(idem)
        if existing is not None:
            current_hash = payload_digest(request)
            if existing[0] != current_hash:
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            self._book = candidate
            self._idempotency = idempotency
            return self.get(existing[1]["reservation_id"])

        # The candidate and aggregate version come from that same journal cut.
        try:
            snapshot = self._apply(candidate, operation, request)
        except Exception:
            # Keep the exposed projection synchronized even when evaluation
            # rejects the mutation.
            self._reload()
            raise
        snapshot_value = _snapshot_payload(snapshot)
        next_version = (
            1
            if not events
            else int(events[-1]["aggregate_version"]) + 1
        )
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://reservations.autotrade.local/event/"
                f"{self.account_id!r}/{cid!r}/{next_version}",
            )
        )
        journal_command_id = _journal_identity(
            self.account_id,
            "command",
            cid,
        )
        journal_idempotency_key = _journal_identity(
            self.account_id,
            "idempotency",
            idem,
        )
        payload = {
            "operation": operation,
            "request": request,
            "idempotency_key": idem,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": event_id,
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.account_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _now(),
        }

        # commit_command is the single durable transaction: command dedupe and
        # reservation event either both commit or neither does.
        try:
            self.store.commit_command(
                command_id=journal_command_id,
                idempotency_key=journal_idempotency_key,
                request={
                    "account_id": self.account_id,
                    "operation": operation,
                    "request": request,
                },
                result=snapshot_value,
                state_version=next_version,
                events=[(envelope, None)],
            )
        except Exception:
            # A competing writer may have committed after this projection was
            # built. Never leave this authority object serving stale financial
            # availability after the optimistic-concurrency fence rejects us.
            self._reload()
            raise
        self._reload()
        return self.get(snapshot.reservation_id)

    def get(self, reservation_id: str) -> ReservationSnapshot:
        return self._book.get(reservation_id)

    def total_reserved(self, resource: str) -> Decimal:
        return self._book.total_reserved(resource)

    def active(self) -> tuple[ReservationSnapshot, ...]:
        return self._book.active()

    @property
    def version(self) -> int:
        events = self._events()
        return 0 if not events else int(events[-1]["aggregate_version"])

    def reserve(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        intent_id: str,
        requirements: Mapping[str, object],
        available: Mapping[str, object],
    ) -> ReservationSnapshot:
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "intent_id": _text(intent_id, name="intent_id"),
            "requirements": _amount_map(requirements, allow_zero=False),
            "available": _amount_map(available, allow_zero=True),
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="RESERVE",
            request=request,
        )

    def consume(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        usage: Mapping[str, object],
    ) -> ReservationSnapshot:
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "usage": _amount_map(usage, allow_zero=False),
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="CONSUME",
            request=request,
        )

    def mark_unknown(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
    ) -> ReservationSnapshot:
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="MARK_UNKNOWN",
            request=request,
        )

    def mark_terminal(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        outcome: str,
        resolution_evidence: str,
    ) -> ReservationSnapshot:
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "outcome": _text(outcome, name="outcome").upper(),
            "resolution_evidence": _immutable_evidence_ref(resolution_evidence),
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="MARK_TERMINAL",
            request=request,
        )
