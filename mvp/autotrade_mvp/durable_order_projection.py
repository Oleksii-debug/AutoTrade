"""Durable journal-backed adapter for the canonical WP-19 order projection.

This module does not create a second order authority or persistence store.
Every lifecycle mutation is first committed to the canonical JournalStore and
then the in-memory OrderBookProjection is rebuilt from that immutable history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)

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
_PROVIDER_EVIDENCE_OPERATIONS = frozenset(
    {
        "RECORD_FILL",
        "CORRECT_FILL",
        "BUST_FILL",
        "CONFIRM_CANCEL",
        "CONFIRM_EXPIRED",
    }
)


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


def _canonical_uri(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if any(character.isspace() for character in text):
        raise ValueError(f"{name} must be an absolute URI")
    parsed = urlsplit(text)
    if not parsed.scheme:
        raise ValueError(f"{name} must be an absolute URI")
    if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
        raise ValueError(f"{name} must be an absolute URI")
    return text


def _canonical_quantity_value(value: object, *, name: str) -> str:
    """Validate canonical Quantity shape and return its exact decimal value."""
    if not isinstance(value, Mapping):
        raise OrderProjectionConflict(f"{name} must be a canonical Quantity object")
    unknown = set(value) - {"value", "unit"}
    missing = {"value", "unit"} - set(value)
    if unknown or missing:
        raise OrderProjectionConflict(
            f"{name} must contain exactly value and unit"
        )
    raw_value = value.get("value")
    if not isinstance(raw_value, str):
        raise OrderProjectionConflict(f"{name}.value must be a decimal string")
    canonical_value = _decimal_text(raw_value, name=f"{name}.value")
    if canonical_value != raw_value:
        raise OrderProjectionConflict(f"{name}.value must be canonical decimal text")
    _text(value.get("unit"), name=f"{name}.unit")
    return raw_value


def _canonical_evidence_refs(
    value: Sequence[Mapping[str, object]] | None,
) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("evidence_refs must be a sequence of EvidenceRef mappings")
    normalized: list[dict[str, str]] = []
    identities: set[str] = set()
    allowed = {"artifact_id", "sha256", "source_uri", "observed_at", "rights_id"}
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise TypeError(f"evidence_refs[{index}] must be a mapping")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                "evidence ref contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        artifact_id = _text(raw.get("artifact_id"), name="artifact_id")
        try:
            artifact_id = str(UUID(artifact_id))
        except ValueError as error:
            raise ValueError("artifact_id must be a UUID") from error
        digest = _text(raw.get("sha256"), name="sha256")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise ValueError("sha256 must be canonical lowercase SHA-256")
        ref: dict[str, str] = {
            "artifact_id": artifact_id,
            "sha256": digest,
            "observed_at": _instant(raw.get("observed_at"), name="observed_at"),
        }
        if raw.get("source_uri") is not None:
            ref["source_uri"] = _canonical_uri(
                raw.get("source_uri"),
                name="source_uri",
            )
        if raw.get("rights_id") is not None:
            ref["rights_id"] = _text(raw.get("rights_id"), name="rights_id")
        identity = canonical_json(ref)
        if identity in identities:
            raise ValueError("evidence_refs must be unique")
        identities.add(identity)
        normalized.append(ref)
    return tuple(normalized)


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
        evidence_artifact_store: ArtifactStore | None = None,
    ):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, name="provider_id").upper()
        self.account_id = _text(account_id, name="account_id")
        self.environment = _environment(environment)
        self.host_id = _text(host_id, name="host_id")
        self.owner_epoch = _text(owner_epoch, name="owner_epoch")
        if evidence_artifact_store is not None and not isinstance(
            evidence_artifact_store, ArtifactStore
        ):
            raise TypeError("evidence_artifact_store must be ArtifactStore")
        self.evidence_artifact_store = evidence_artifact_store
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

    @staticmethod
    def _requires_provider_evidence(
        operation: object,
        request: Mapping[str, object],
    ) -> bool:
        if operation == "ACKNOWLEDGE":
            status = str(request.get("status", "")).upper()
            return status in {"ACKNOWLEDGED", "ACCEPTED", "REJECTED"}
        return operation in _PROVIDER_EVIDENCE_OPERATIONS

    def _verify_provider_evidence(
        self,
        *,
        operation: object,
        request: Mapping[str, object],
        evidence_refs: Sequence[Mapping[str, object]] | None,
        committed_at: str,
    ) -> tuple[dict[str, str], ...]:
        refs = _canonical_evidence_refs(evidence_refs)
        requires = self._requires_provider_evidence(operation, request)
        if self.environment in {"PAPER", "LIVE"} and requires and not refs:
            raise OrderProjectionConflict(
                "provider-observed lifecycle mutation requires immutable evidence"
            )
        if not refs:
            return ()
        if not requires:
            raise OrderProjectionConflict(
                "local lifecycle mutation must not claim provider-result evidence"
            )
        if self.evidence_artifact_store is None:
            if self.environment in {"PAPER", "LIVE"}:
                raise OrderProjectionConflict(
                    "provider evidence requires the trusted ArtifactStore boundary"
                )
            # REPLAY/SIMULATION are deterministic synthetic environments. They
            # may carry canonical evidence identity without claiming that a
            # real provider artifact has been qualified by the trusted store.
            return refs

        committed = _instant(committed_at, name="committed_at")
        committed_dt = datetime.fromisoformat(
            committed.replace("Z", "+00:00")
        )
        request_hash = payload_digest(dict(request))
        for ref in refs:
            observed_dt = datetime.fromisoformat(
                ref["observed_at"].replace("Z", "+00:00")
            )
            if observed_dt > committed_dt:
                raise OrderProjectionConflict(
                    "provider evidence observation cannot be later than commit time"
                )
            try:
                manifest = self.evidence_artifact_store.load_manifest(
                    ref["artifact_id"]
                )
                self.evidence_artifact_store.read_bytes(ref["artifact_id"])
            except (FileNotFoundError, ArtifactIntegrityError, ValueError) as error:
                raise OrderProjectionConflict(
                    "provider evidence artifact is not resolvable and intact"
                ) from error
            if manifest.get("sha256") != ref["sha256"]:
                raise OrderProjectionConflict(
                    "provider evidence digest differs from immutable artifact"
                )
            metadata = manifest.get("metadata")
            if not isinstance(metadata, dict):
                raise OrderProjectionConflict(
                    "provider evidence artifact requires scoped metadata"
                )
            expected_scope = {
                "provider_id": self.provider_id,
                "account_id": self.account_id,
                "environment": self.environment,
                "order_operation": str(operation),
                "request_hash": request_hash,
                "observed_at": ref["observed_at"],
            }
            for key, expected in expected_scope.items():
                if metadata.get(key) != expected:
                    raise OrderProjectionConflict(
                        f"provider evidence metadata mismatch: {key}"
                    )
            source_uri = ref.get("source_uri")
            if source_uri is not None and source_uri not in manifest.get(
                "source_refs", []
            ):
                raise OrderProjectionConflict(
                    "provider evidence source_uri is not bound by artifact manifest"
                )
            rights_id = ref.get("rights_id")
            if rights_id is not None and metadata.get("rights_id") != rights_id:
                raise OrderProjectionConflict(
                    "provider evidence rights_id is not bound by artifact metadata"
                )
        return refs

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
            evidence_refs = self._verify_provider_evidence(
                operation=operation,
                request=request,
                evidence_refs=event.get("evidence_refs"),
                committed_at=event.get("committed_at"),
            )
            mutation_hash = payload_digest(
                {
                    "request_hash": request_hash,
                    "evidence_refs": list(evidence_refs),
                }
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
                mutation_hash,
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
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> DurableOrderMutationResult:
        key = _text(event_key, name="event_key")
        timestamp = _instant(committed_at, name="committed_at")
        request_hash = payload_digest(request)
        verified_evidence = self._verify_provider_evidence(
            operation=operation,
            request=request,
            evidence_refs=evidence_refs,
            committed_at=timestamp,
        )
        mutation_hash = payload_digest(
            {
                "request_hash": request_hash,
                "evidence_refs": list(verified_evidence),
            }
        )

        self._reload()
        prior = self._idempotency.get(key)
        if prior is not None:
            if prior[0] != mutation_hash:
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
                    [self.aggregate_id, key, mutation_hash]
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
            "evidence_refs": [dict(ref) for ref in verified_evidence],
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
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
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
            evidence_refs=evidence_refs,
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
                        evidence_refs=response.get("evidence"),
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
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
        canonical_execution_fill_hash: str | None = None,
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
        if canonical_execution_fill_hash is not None:
            request["canonical_execution_fill_hash"] = _text(
                canonical_execution_fill_hash,
                name="canonical_execution_fill_hash",
            )
        return self._commit(
            event_key=event_key,
            operation="RECORD_FILL",
            request=request,
            committed_at=committed_at,
            evidence_refs=evidence_refs,
        )

    def ingest_execution_fill(
        self,
        *,
        client_order_id: str,
        execution_fill: Mapping[str, object],
        event_key: str,
        committed_at: str,
    ) -> DurableOrderMutationResult:
        """Apply one canonical ExecutionFill through the existing order authority.

        Provider adapters and reconciliation normalize provider-specific payloads
        before this boundary. This method deliberately does not interpret raw
        provider responses and does not create a second lifecycle state machine.
        """
        if not isinstance(execution_fill, Mapping):
            raise TypeError("execution_fill must be a mapping")

        allowed = {
            "fill_id",
            "provider_execution_id",
            "provider_revision",
            "order_ref",
            "intent_ref",
            "instrument_version",
            "side",
            "last_quantity",
            "last_price",
            "trade_time",
            "receipt_time",
            "fees",
            "liquidity_flag",
            "settlement_date",
            "correction_reference",
            "evidence",
        }
        required = {
            "fill_id",
            "provider_execution_id",
            "instrument_version",
            "side",
            "last_quantity",
            "last_price",
            "trade_time",
            "receipt_time",
            "fees",
            "settlement_date",
            "evidence",
        }
        unknown = set(execution_fill) - allowed
        if unknown:
            raise OrderProjectionConflict(
                "canonical ExecutionFill contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        missing = required - set(execution_fill)
        if missing:
            raise OrderProjectionConflict(
                "canonical ExecutionFill is missing required fields: "
                + ", ".join(sorted(missing))
            )

        client_id = _text(client_order_id, name="client_order_id")
        order = self.order(client_id)
        order_ref = execution_fill.get("order_ref")
        if order_ref is not None and _text(order_ref, name="order_ref") != client_id:
            raise OrderProjectionConflict(
                "canonical ExecutionFill order_ref differs from target order"
            )
        if (
            _text(execution_fill.get("instrument_version"), name="instrument_version")
            != order.instrument
        ):
            raise OrderProjectionConflict(
                "canonical ExecutionFill instrument differs from target order"
            )
        fill_side = _text(execution_fill.get("side"), name="side")
        if fill_side not in {"BUY", "SELL"}:
            raise OrderProjectionConflict(
                "canonical ExecutionFill side must be BUY or SELL"
            )
        if fill_side != order.side:
            raise OrderProjectionConflict(
                "canonical ExecutionFill side differs from target order"
            )

        intent_ref = execution_fill.get("intent_ref")
        if intent_ref is not None:
            normalized_intent_ref = _text(intent_ref, name="intent_ref")
            if (
                order.parent_intent_id is None
                or normalized_intent_ref != order.parent_intent_id
            ):
                raise OrderProjectionConflict(
                    "canonical ExecutionFill intent_ref differs from target order"
                )

        trade_time = _instant(execution_fill.get("trade_time"), name="trade_time")
        receipt_time = _instant(
            execution_fill.get("receipt_time"),
            name="receipt_time",
        )
        committed = _instant(committed_at, name="committed_at")
        trade_dt = datetime.fromisoformat(trade_time.replace("Z", "+00:00"))
        receipt_dt = datetime.fromisoformat(receipt_time.replace("Z", "+00:00"))
        committed_dt = datetime.fromisoformat(committed.replace("Z", "+00:00"))
        if receipt_dt < trade_dt:
            raise OrderProjectionConflict(
                "canonical ExecutionFill receipt_time precedes trade_time"
            )
        if committed_dt < receipt_dt:
            raise OrderProjectionConflict(
                "canonical ExecutionFill cannot be committed before receipt_time"
            )

        quantity_value = _canonical_quantity_value(
            execution_fill.get("last_quantity"),
            name="last_quantity",
        )
        last_price = execution_fill.get("last_price")
        if not isinstance(last_price, str):
            raise OrderProjectionConflict(
                "canonical ExecutionFill last_price must be a decimal string"
            )
        if _decimal_text(last_price, name="last_price") != last_price:
            raise OrderProjectionConflict(
                "canonical ExecutionFill last_price must be canonical decimal text"
            )

        fees = execution_fill.get("fees")
        if isinstance(fees, (str, bytes)) or not isinstance(fees, Sequence):
            raise OrderProjectionConflict("canonical ExecutionFill fees must be an array")
        for index, fee in enumerate(fees):
            if not isinstance(fee, Mapping) or set(fee) != {"amount", "currency"}:
                raise OrderProjectionConflict(
                    f"canonical ExecutionFill fees[{index}] must be Money"
                )
            amount = fee.get("amount")
            if not isinstance(amount, str) or _decimal_text(
                amount, name=f"fees[{index}].amount"
            ) != amount:
                raise OrderProjectionConflict(
                    f"canonical ExecutionFill fees[{index}].amount must be canonical decimal text"
                )
            _text(fee.get("currency"), name=f"fees[{index}].currency")

        liquidity_flag = execution_fill.get("liquidity_flag")
        if liquidity_flag is not None:
            _text(liquidity_flag, name="liquidity_flag")

        evidence = execution_fill.get("evidence")
        if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
            raise OrderProjectionConflict(
                "canonical ExecutionFill evidence must be an array"
            )
        settlement_date = _text(
            execution_fill.get("settlement_date"),
            name="settlement_date",
        )
        try:
            parsed_settlement_date = date.fromisoformat(settlement_date)
        except ValueError as error:
            raise OrderProjectionConflict(
                "canonical ExecutionFill settlement_date must be an ISO calendar date"
            ) from error
        if parsed_settlement_date.isoformat() != settlement_date:
            raise OrderProjectionConflict(
                "canonical ExecutionFill settlement_date must be canonical YYYY-MM-DD"
            )

        canonical_fill: dict[str, object] = {
            "fill_id": _text(execution_fill.get("fill_id"), name="fill_id"),
            "provider_execution_id": _text(
                execution_fill.get("provider_execution_id"),
                name="provider_execution_id",
            ),
            "instrument_version": order.instrument,
            "side": fill_side,
            "last_quantity": {
                "value": quantity_value,
                "unit": _text(
                    execution_fill["last_quantity"].get("unit"),
                    name="last_quantity.unit",
                ),
            },
            "last_price": last_price,
            "trade_time": trade_time,
            "receipt_time": receipt_time,
            "fees": [
                {
                    "amount": str(fee["amount"]),
                    "currency": _text(
                        fee["currency"],
                        name=f"fees[{index}].currency",
                    ),
                }
                for index, fee in enumerate(fees)
            ],
            "settlement_date": settlement_date,
            "evidence": [
                dict(ref) for ref in _canonical_evidence_refs(evidence)
            ],
        }
        for optional in (
            "provider_revision",
            "order_ref",
            "intent_ref",
            "liquidity_flag",
            "correction_reference",
        ):
            if execution_fill.get(optional) is not None:
                canonical_fill[optional] = _text(
                    execution_fill.get(optional),
                    name=optional,
                )
        canonical_execution_fill_hash = payload_digest(canonical_fill)

        correction_reference = execution_fill.get("correction_reference")
        if correction_reference is not None:
            provider_revision = execution_fill.get("provider_revision")
            if provider_revision is None:
                raise OrderProjectionConflict(
                    "corrected ExecutionFill requires provider_revision"
                )
            correction_target = _text(
                correction_reference,
                name="correction_reference",
            )
            incoming_execution_id = _text(
                execution_fill.get("provider_execution_id"),
                name="provider_execution_id",
            )
            target_observation = next(
                (
                    item
                    for item in reversed(order.fill_history)
                    if item.fill_id == correction_target
                    or item.correction_of == correction_target
                ),
                None,
            )
            if target_observation is None:
                raise OrderProjectionConflict(
                    "corrected ExecutionFill references an unknown fill"
                )
            if target_observation.provider_execution_id != incoming_execution_id:
                raise OrderProjectionConflict(
                    "corrected ExecutionFill provider_execution_id differs from target fill"
                )
            root_fill_id = order.provider_execution_index.get(incoming_execution_id)
            if root_fill_id is None:
                raise OrderProjectionConflict(
                    "corrected ExecutionFill execution lineage is not indexed"
                )
            return self.correct_fill(
                event_key=event_key,
                client_order_id=client_id,
                fill_id=root_fill_id,
                correction_fill_id=_text(
                    execution_fill.get("fill_id"),
                    name="fill_id",
                ),
                quantity=quantity_value,
                price=last_price,
                provider_revision=_text(
                    provider_revision,
                    name="provider_revision",
                ),
                committed_at=committed,
                evidence_refs=evidence,
                canonical_execution_fill_hash=canonical_execution_fill_hash,
            )

        return self.record_fill(
            event_key=event_key,
            client_order_id=client_id,
            fill_id=_text(execution_fill.get("fill_id"), name="fill_id"),
            provider_execution_id=_text(
                execution_fill.get("provider_execution_id"),
                name="provider_execution_id",
            ),
            quantity=quantity_value,
            price=last_price,
            provider_revision=_optional_text(
                execution_fill.get("provider_revision"),
                name="provider_revision",
            ),
            committed_at=committed,
            evidence_refs=evidence,
            canonical_execution_fill_hash=canonical_execution_fill_hash,
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
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
        canonical_execution_fill_hash: str | None = None,
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
        if canonical_execution_fill_hash is not None:
            request["canonical_execution_fill_hash"] = _text(
                canonical_execution_fill_hash,
                name="canonical_execution_fill_hash",
            )
        return self._commit(
            event_key=event_key,
            operation="CORRECT_FILL",
            request=request,
            committed_at=committed_at,
            evidence_refs=evidence_refs,
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
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
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
            evidence_refs=evidence_refs,
        )

    def _simple(
        self,
        *,
        event_key: str,
        operation: str,
        client_order_id: str,
        committed_at: str,
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
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
            evidence_refs=evidence_refs,
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
