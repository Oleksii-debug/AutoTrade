"""Durable journal-backed adapter for the canonical WP-19 order projection.

This module does not create a second order authority or persistence store.
Every lifecycle mutation is first committed to the canonical JournalStore and
then the in-memory OrderBookProjection is rebuilt from that immutable history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping
from uuid import NAMESPACE_URL, uuid5

from .dispatch import submission_attempt_aggregate_id
from .order_projection import (
    OrderBookProjection,
    OrderProjectionConflict,
    OrderSnapshot,
)
from .persistence import JournalStore, canonical_json, payload_digest


_AGGREGATE_TYPE = "order_projection_book"
_EVENT_TYPE = "OrderProjectionMutationCommitted"
_OUTBOX_TOPIC = "autotrade.order-projection.events"


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _environment(value: str) -> str:
    normalized = _text(value, name="environment").upper()
    if normalized not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("unsupported environment")
    return normalized


def _instant(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _decimal_text(value, *, name: str) -> str:
    number = _decimal(value, name=name)
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _optional_text(value: str | None, *, name: str) -> str | None:
    return None if value is None else _text(value, name=name)


def _snapshot_payload(snapshot: OrderSnapshot) -> dict[str, object]:
    return {
        "provider_id": snapshot.provider_id,
        "account_id": snapshot.account_id,
        "environment": snapshot.environment,
        "client_order_id": snapshot.client_order_id,
        "instrument": snapshot.instrument,
        "side": snapshot.side,
        "requested_quantity": _decimal_text(
            snapshot.requested_quantity,
            name="requested_quantity",
        ),
        "state": snapshot.state,
        "filled_quantity": _decimal_text(
            snapshot.filled_quantity,
            name="filled_quantity",
        ),
        "open_quantity": _decimal_text(snapshot.open_quantity, name="open_quantity"),
        "overfill_quantity": _decimal_text(
            snapshot.overfill_quantity,
            name="overfill_quantity",
        ),
        "average_fill_price": (
            None
            if snapshot.average_fill_price is None
            else _decimal_text(
                snapshot.average_fill_price,
                name="average_fill_price",
            )
        ),
        "provider_order_id": snapshot.provider_order_id,
        "submission_attempt_id": snapshot.submission_attempt_id,
        "parent_intent_id": snapshot.parent_intent_id,
        "oco_group_id": snapshot.oco_group_id,
        "oco_violation": snapshot.oco_violation,
        "fill_count": snapshot.fill_count,
        "observation_count": snapshot.observation_count,
        "cancel_requested": snapshot.cancel_requested,
        "cancel_confirmed": snapshot.cancel_confirmed,
        "cancel_command_id": snapshot.cancel_command_id,
        "replace_requested": snapshot.replace_requested,
        "replace_command_id": snapshot.replace_command_id,
        "expired": snapshot.expired,
    }


def _scope_id(provider_id: str, account_id: str, environment: str) -> str:
    canonical = canonical_json([provider_id, account_id, environment])
    return "order-projection:" + str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/order-projection-scope/" + canonical,
        )
    )


@dataclass(frozen=True)
class DurableOrderMutationResult:
    event_id: str
    inserted: bool
    snapshot: OrderSnapshot


class DurableOrderBookProjection:
    """Crash-recoverable adapter over the single canonical OrderBookProjection."""

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        host_id: str,
        owner_epoch: str,
    ):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, name="provider_id").upper()
        self.account_id = _text(account_id, name="account_id")
        self.environment = _environment(environment)
        self.host_id = _text(host_id, name="host_id")
        self.owner_epoch = _text(owner_epoch, name="owner_epoch")
        self.aggregate_id = _scope_id(
            self.provider_id,
            self.account_id,
            self.environment,
        )
        self._book = self._new_book()
        self._idempotency: dict[
            str,
            tuple[str, OrderSnapshot, str],
        ] = {}
        self._reload()

    def _new_book(self) -> OrderBookProjection:
        return OrderBookProjection(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
        )

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.aggregate_id)

    def _scope_payload(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
        }

    @staticmethod
    def _apply(
        book: OrderBookProjection,
        operation: object,
        request: Mapping[str, object],
    ) -> OrderSnapshot:
        client_order_id = _text(
            request.get("client_order_id"),
            name="client_order_id",
        )
        if operation == "CREATE":
            order = book.create(
                client_order_id=client_order_id,
                instrument=request.get("instrument"),
                side=request.get("side"),
                requested_quantity=request.get("requested_quantity"),
                oco_group_id=request.get("oco_group_id"),
                parent_intent_id=request.get("parent_intent_id"),
            )
            return order.snapshot()

        order = book.order(client_order_id)
        if operation == "MARK_SEND_STARTED":
            order.mark_send_started(attempt_id=request.get("attempt_id"))
        elif operation == "ACKNOWLEDGE":
            order.acknowledge(
                provider_order_id=request.get("provider_order_id"),
                status=request.get("status"),
                attempt_id=request.get("attempt_id"),
            )
        elif operation == "RECORD_FILL":
            order.record_fill(
                fill_id=request.get("fill_id"),
                provider_execution_id=request.get("provider_execution_id"),
                quantity=request.get("quantity"),
                price=request.get("price"),
                provider_revision=request.get("provider_revision"),
            )
        elif operation == "CORRECT_FILL":
            order.correct_fill(
                fill_id=request.get("fill_id"),
                quantity=request.get("quantity"),
                price=request.get("price"),
                provider_revision=request.get("provider_revision"),
                correction_fill_id=request.get("correction_fill_id"),
            )
        elif operation == "BUST_FILL":
            order.bust_fill(
                request.get("fill_id"),
                provider_revision=request.get("provider_revision"),
                correction_fill_id=request.get("correction_fill_id"),
            )
        elif operation == "REQUEST_CANCEL":
            order.request_cancel(command_id=request.get("command_id"))
        elif operation == "CONFIRM_CANCEL":
            order.confirm_cancel()
        elif operation == "REQUEST_REPLACE":
            order.request_replace(command_id=request.get("command_id"))
        elif operation == "CONFIRM_EXPIRED":
            order.confirm_expired()
        else:
            raise OrderProjectionConflict(
                f"unsupported durable order operation: {operation}"
            )
        return order.snapshot()

    def _replay(
        self,
        events: list[dict[str, object]],
    ) -> tuple[
        OrderBookProjection,
        dict[str, tuple[str, OrderSnapshot, str]],
    ]:
        book = self._new_book()
        idempotency: dict[str, tuple[str, OrderSnapshot, str]] = {}
        expected_version = 1

        for event in events:
            if event["aggregate_version"] != expected_version:
                raise OrderProjectionConflict(
                    "order projection journal versions are not contiguous"
                )
            expected_version += 1
            if event["event_type"] != _EVENT_TYPE:
                raise OrderProjectionConflict(
                    "order projection journal contains unsupported event type"
                )
            payload = event["payload"]
            if not isinstance(payload, dict):
                raise OrderProjectionConflict(
                    "order projection journal payload must be an object"
                )
            if payload_digest(payload) != event["payload_hash"]:
                raise OrderProjectionConflict(
                    "order projection journal payload hash mismatch"
                )
            if payload.get("scope") != self._scope_payload():
                raise OrderProjectionConflict(
                    "order projection journal scope mismatch"
                )
            operation = payload.get("operation")
            request = payload.get("request")
            event_key = _text(payload.get("event_key"), name="event_key")
            request_hash = _text(
                payload.get("request_hash"),
                name="request_hash",
            )
            expected_snapshot = payload.get("snapshot")
            if not isinstance(request, dict):
                raise OrderProjectionConflict(
                    "order projection journal request must be an object"
                )
            if request_hash != payload_digest(request):
                raise OrderProjectionConflict(
                    "order projection journal request hash mismatch"
                )
            if not isinstance(expected_snapshot, dict):
                raise OrderProjectionConflict(
                    "order projection journal snapshot must be an object"
                )
            if event_key in idempotency:
                raise OrderProjectionConflict(
                    "duplicate durable order event_key in journal"
                )
            try:
                snapshot = self._apply(book, operation, request)
            except Exception as error:
                raise OrderProjectionConflict(
                    "order projection journal cannot replay "
                    f"version {event['aggregate_version']}: {error}"
                ) from error
            if _snapshot_payload(snapshot) != expected_snapshot:
                raise OrderProjectionConflict(
                    "order projection journal snapshot differs from replay"
                )
            idempotency[event_key] = (
                request_hash,
                snapshot,
                str(event["event_id"]),
            )

        return book, idempotency

    def _reload(self) -> None:
        self._book, self._idempotency = self._replay(self._events())

    def _commit(
        self,
        *,
        event_key: str,
        operation: str,
        request: dict[str, object],
        committed_at: str,
    ) -> DurableOrderMutationResult:
        key = _text(event_key, name="event_key")
        timestamp = _instant(committed_at, name="committed_at")
        request_hash = payload_digest(request)

        self._reload()
        prior = self._idempotency.get(key)
        if prior is not None:
            if prior[0] != request_hash:
                raise OrderProjectionConflict(
                    "event_key was already used for a different order request"
                )
            return DurableOrderMutationResult(
                event_id=prior[2],
                inserted=False,
                snapshot=prior[1],
            )

        events = self._events()
        candidate, _ = self._replay(events)
        snapshot = self._apply(candidate, operation, request)
        payload = {
            "schema_version": "1.0.0",
            "scope": self._scope_payload(),
            "event_key": key,
            "operation": operation,
            "request": request,
            "request_hash": request_hash,
            "snapshot": _snapshot_payload(snapshot),
        }
        version = self.store.next_aggregate_version(
            _AGGREGATE_TYPE,
            self.aggregate_id,
        )
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/order-projection/"
                + canonical_json(
                    [self.aggregate_id, key, request_hash]
                ),
            )
        )
        envelope = {
            "event_id": event_id,
            "event_type": _EVENT_TYPE,
            "schema_version": "1.0.0",
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(version),
            "host_id": self.host_id,
            "owner_epoch": self.owner_epoch,
            "environment": self.environment,
            "occurred_at": timestamp,
            "observed_at": timestamp,
            "committed_at": timestamp,
            "correlation_id": event_id,
            "causation_id": None,
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "evidence_refs": [],
        }
        try:
            append_result = self.store.append_event(
                envelope,
                outbox_topic=_OUTBOX_TOPIC,
            )
        except Exception:
            self._reload()
            raise
        self._reload()
        recorded = self._idempotency.get(key)
        if recorded is None:
            raise RuntimeError("durable order event was not replayed after commit")
        return DurableOrderMutationResult(
            event_id=recorded[2],
            inserted=append_result.inserted,
            snapshot=recorded[1],
        )

    @property
    def snapshots(self) -> tuple[OrderSnapshot, ...]:
        return self._book.snapshots()

    def order(self, client_order_id: str):
        return self._book.order(client_order_id)

    def effective_fills(self):
        return self._book.effective_fills()

    def oco_breaches(self):
        return self._book.oco_breaches()

    def active_oco_breaches(self):
        return self._book.active_oco_breaches()

    def create_order(
        self,
        *,
        event_key: str,
        client_order_id: str,
        instrument: str,
        side: str,
        requested_quantity,
        committed_at: str,
        oco_group_id: str | None = None,
        parent_intent_id: str | None = None,
    ) -> DurableOrderMutationResult:
        request = {
            "client_order_id": _text(client_order_id, name="client_order_id"),
            "instrument": _text(instrument, name="instrument"),
            "side": _text(side, name="side").upper(),
            "requested_quantity": _decimal_text(
                requested_quantity,
                name="requested_quantity",
            ),
            "oco_group_id": _optional_text(oco_group_id, name="oco_group_id"),
            "parent_intent_id": _optional_text(
                parent_intent_id,
                name="parent_intent_id",
            ),
        }
        return self._commit(
            event_key=event_key,
            operation="CREATE",
            request=request,
            committed_at=committed_at,
        )

    def mark_send_started(
        self,
        *,
        event_key: str,
        client_order_id: str,
        attempt_id: str,
        committed_at: str,
    ) -> DurableOrderMutationResult:
        request = {
            "client_order_id": _text(client_order_id, name="client_order_id"),
            "attempt_id": _text(attempt_id, name="attempt_id"),
        }
        return self._commit(
            event_key=event_key,
            operation="MARK_SEND_STARTED",
            request=request,
            committed_at=committed_at,
        )

    def acknowledge(
        self,
        *,
        event_key: str,
        client_order_id: str,
        provider_order_id: str | None = None,
        committed_at: str,
        status: str = "ACCEPTED",
        attempt_id: str | None = None,
    ) -> DurableOrderMutationResult:
        request = {
            "client_order_id": _text(client_order_id, name="client_order_id"),
            "provider_order_id": _optional_text(
                provider_order_id,
                name="provider_order_id",
            ),
            "status": _text(status, name="status").upper(),
            "attempt_id": _optional_text(attempt_id, name="attempt_id"),
        }
        return self._commit(
            event_key=event_key,
            operation="ACKNOWLEDGE",
            request=request,
            committed_at=committed_at,
        )

    def sync_submission_attempt(
        self,
        *,
        attempt_id: str,
    ) -> tuple[DurableOrderMutationResult, ...]:
        """Project already-durable WP-18 submission facts without resending."""

        attempt = _text(attempt_id, name="attempt_id")
        aggregate_id = submission_attempt_aggregate_id(
            environment=self.environment,
            account_id=self.account_id,
            attempt_id=attempt,
        )
        events = self.store.load_events("submission_attempt", aggregate_id)
        if not events:
            raise KeyError(attempt)

        for index, event in enumerate(events, start=1):
            if event.get("aggregate_version") != index:
                raise OrderProjectionConflict(
                    "submission attempt journal versions are not contiguous"
                )
        prepared = events[0]
        if prepared.get("event_type") != "SubmissionPrepared":
            raise OrderProjectionConflict(
                "submission attempt does not start with durable preparation"
            )
        prepared_payload = prepared.get("payload")
        if not isinstance(prepared_payload, dict):
            raise OrderProjectionConflict(
                "submission preparation payload must be an object"
            )
        try:
            provider = _text(
                prepared_payload.get("provider"),
                name="submission provider",
            ).upper()
            account_id = _text(
                prepared_payload.get("account_id"),
                name="submission account_id",
            )
            environment = _environment(
                prepared_payload.get("environment")
            )
            client_order_id = _text(
                prepared_payload.get("client_order_id"),
                name="submission client_order_id",
            )
        except (TypeError, ValueError) as error:
            raise OrderProjectionConflict(
                "submission preparation scope is invalid"
            ) from error
        if (
            provider != self.provider_id
            or account_id != self.account_id
            or environment != self.environment
        ):
            raise OrderProjectionConflict(
                "submission attempt scope differs from order projection"
            )
        # Fail early if the durable dispatch refers to no canonical order.
        self.order(client_order_id)

        results: list[DurableOrderMutationResult] = []
        sending_seen = False
        terminal_seen = False
        for event in events[1:]:
            event_type = event.get("event_type")
            committed_at = _text(
                event.get("committed_at"),
                name="submission committed_at",
            )
            source_event_id = _text(
                event.get("event_id"),
                name="submission event_id",
            )
            event_key = "submission-journal:" + source_event_id

            if event_type == "SubmissionSending":
                if sending_seen or terminal_seen:
                    raise OrderProjectionConflict(
                        "submission attempt has invalid send ordering"
                    )
                sending_seen = True
                results.append(
                    self.mark_send_started(
                        event_key=event_key,
                        client_order_id=client_order_id,
                        attempt_id=attempt,
                        committed_at=committed_at,
                    )
                )
                continue

            if event_type == "SubmissionSent":
                if not sending_seen or terminal_seen:
                    raise OrderProjectionConflict(
                        "submission sent outcome lacks one send-start fact"
                    )
                terminal_seen = True
                payload = event.get("payload")
                response = (
                    payload.get("response")
                    if isinstance(payload, dict)
                    else None
                )
                if not isinstance(response, Mapping):
                    raise OrderProjectionConflict(
                        "submission sent response must be an object"
                    )
                if (
                    response.get("attempt_id") != attempt
                    or response.get("client_order_id") != client_order_id
                ):
                    raise OrderProjectionConflict(
                        "submission response identity differs from durable attempt"
                    )
                results.append(
                    self.acknowledge(
                        event_key=event_key,
                        client_order_id=client_order_id,
                        attempt_id=attempt,
                        provider_order_id=response.get("provider_order_id"),
                        status=response.get("outcome"),
                        committed_at=committed_at,
                    )
                )
                continue

            if event_type == "SubmissionUnknown":
                if terminal_seen:
                    raise OrderProjectionConflict(
                        "submission attempt has multiple terminal outcomes"
                    )
                terminal_seen = True
                results.append(
                    self.acknowledge(
                        event_key=event_key,
                        client_order_id=client_order_id,
                        attempt_id=attempt,
                        status="UNKNOWN",
                        committed_at=committed_at,
                    )
                )
                continue

            if event_type == "SubmissionBlocked":
                if sending_seen:
                    raise OrderProjectionConflict(
                        "blocked submission cannot follow send-start"
                    )
                # No provider-side order lifecycle fact exists when the send
                # barrier blocked the attempt. Keep the pre-send order pending.
                continue

        return tuple(results)

    def record_fill(
        self,
        *,
        event_key: str,
        client_order_id: str,
        fill_id: str,
        provider_execution_id: str,
        quantity,
        price,
        committed_at: str,
        provider_revision: str | None = None,
    ) -> DurableOrderMutationResult:
        request = {
            "client_order_id": _text(client_order_id, name="client_order_id"),
            "fill_id": _text(fill_id, name="fill_id"),
            "provider_execution_id": _text(
                provider_execution_id,
                name="provider_execution_id",
            ),
            "quantity": _decimal_text(quantity, name="quantity"),
            "price": _decimal_text(price, name="price"),
            "provider_revision": _optional_text(
                provider_revision,
                name="provider_revision",
            ),
        }
        return self._commit(
            event_key=event_key,
            operation="RECORD_FILL",
            request=request,
            committed_at=committed_at,
        )

    def correct_fill(
        self,
        *,
        event_key: str,
        client_order_id: str,
        fill_id: str,
        quantity,
        price,
        provider_revision: str,
        committed_at: str,
        correction_fill_id: str | None = None,
    ) -> DurableOrderMutationResult:
        request = {
            "client_order_id": _text(client_order_id, name="client_order_id"),
            "fill_id": _text(fill_id, name="fill_id"),
            "quantity": _decimal_text(quantity, name="quantity"),
            "price": _decimal_text(price, name="price"),
            "provider_revision": _text(
                provider_revision,
                name="provider_revision",
            ),
            "correction_fill_id": _optional_text(
                correction_fill_id,
                name="correction_fill_id",
            ),
        }
        return self._commit(
            event_key=event_key,
            operation="CORRECT_FILL",
            request=request,
            committed_at=committed_at,
        )

    def bust_fill(
        self,
        *,
        event_key: str,
        client_order_id: str,
        fill_id: str,
        provider_revision: str,
        committed_at: str,
        correction_fill_id: str | None = None,
    ) -> DurableOrderMutationResult:
        request = {
            "client_order_id": _text(client_order_id, name="client_order_id"),
            "fill_id": _text(fill_id, name="fill_id"),
            "provider_revision": _text(
                provider_revision,
                name="provider_revision",
            ),
            "correction_fill_id": _optional_text(
                correction_fill_id,
                name="correction_fill_id",
            ),
        }
        return self._commit(
            event_key=event_key,
            operation="BUST_FILL",
            request=request,
            committed_at=committed_at,
        )

    def _simple(
        self,
        *,
        event_key: str,
        operation: str,
        client_order_id: str,
        committed_at: str,
    ) -> DurableOrderMutationResult:
        return self._commit(
            event_key=event_key,
            operation=operation,
            request={
                "client_order_id": _text(
                    client_order_id,
                    name="client_order_id",
                )
            },
            committed_at=committed_at,
        )

    def request_cancel(
        self,
        *,
        event_key: str,
        client_order_id: str,
        command_id: str,
        committed_at: str,
    ) -> DurableOrderMutationResult:
        return self._commit(
            event_key=event_key,
            operation="REQUEST_CANCEL",
            request={
                "client_order_id": _text(client_order_id, name="client_order_id"),
                "command_id": _text(command_id, name="command_id"),
            },
            committed_at=committed_at,
        )

    def confirm_cancel(self, **kwargs) -> DurableOrderMutationResult:
        return self._simple(operation="CONFIRM_CANCEL", **kwargs)

    def request_replace(
        self,
        *,
        event_key: str,
        client_order_id: str,
        command_id: str,
        committed_at: str,
    ) -> DurableOrderMutationResult:
        return self._commit(
            event_key=event_key,
            operation="REQUEST_REPLACE",
            request={
                "client_order_id": _text(client_order_id, name="client_order_id"),
                "command_id": _text(command_id, name="command_id"),
            },
            committed_at=committed_at,
        )

    def confirm_expired(self, **kwargs) -> DurableOrderMutationResult:
        return self._simple(operation="CONFIRM_EXPIRED", **kwargs)
