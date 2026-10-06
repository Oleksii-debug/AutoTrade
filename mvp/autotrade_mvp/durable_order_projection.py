"""Durable journal-backed adapter for the canonical WP-19 order projection.

This module does not create a second order authority or persistence store.
Every lifecycle mutation is first committed to the canonical JournalStore and
then the in-memory OrderBookProjection is rebuilt from that immutable history.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from .exact_decimal import (
    parse_bounded_exact_decimal,
    ExactDecimalError,
    exact_sum,
    exact_multiply,
    exact_subtract,
    as_fraction,
    terminating_decimal,
    round_fraction_to_quantum,
)
from typing import Mapping, Sequence
from threading import RLock
import weakref
from uuid import NAMESPACE_URL, UUID, uuid5

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .dispatch import (
    _has_exact_response_markers,
    load_submission_response_binding,
    submission_attempt_aggregate_id,
    submission_response_binding_projection,
)
from .order_projection import (
    OrderBookProjection,
    OrderProjectionConflict,
    OrderSnapshot,
)
from .persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
    require_exact_journal_store_authority,
    journal_store_authority_scope,
)


_AGGREGATE_TYPE = "order_projection_book"
_EVENT_TYPE = "OrderProjectionMutationCommitted"
_OUTBOX_TOPIC = "autotrade.order-projection.events"
_PROVIDER_EVIDENCE_OPERATIONS = frozenset(
    {
        "RECORD_FILL",
        "CORRECT_FILL",
        "BUST_FILL",
        "CONFIRM_CANCEL",
        "REJECT_CANCEL",
        "REJECT_REPLACE",
        "CONFIRM_EXPIRED",
    }
)


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _evidence_text(value: object, *, name: str) -> str:
    """Reduce authority-bearing evidence text without polymorphic dispatch."""

    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be canonical non-empty text")
    return value


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
    if type(value) not in {Decimal, str, int}:
        raise TypeError(f"{name} must use exact Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a bounded finite decimal") from error


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


def _canonical_evidence_refs(
    value: Sequence[Mapping[str, object]] | None,
) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if type(value) is list:
        evidence_items = tuple(list.copy(value))
    elif type(value) is tuple:
        evidence_items = value
    else:
        raise TypeError(
            "evidence_refs must be an exact list or tuple of EvidenceRef mappings"
        )

    normalized: list[dict[str, str]] = []
    seen_artifact_ids: set[str] = set()
    allowed = {"artifact_id", "sha256", "source_uri", "observed_at", "rights_id"}
    for index, raw in enumerate(evidence_items):
        if type(raw) is not dict:
            raise TypeError(f"evidence_refs[{index}] must be an exact dict")
        if any(type(key) is not str for key in raw):
            raise TypeError(f"evidence_refs[{index}] keys must be exact strings")
        item = dict.copy(raw)
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(
                "evidence ref contains unsupported fields: "
                + ", ".join(sorted(str(field) for field in unknown))
            )

        artifact_id = _evidence_text(item.get("artifact_id"), name="artifact_id")
        try:
            canonical_artifact_id = str(UUID(artifact_id))
        except ValueError as error:
            raise ValueError("artifact_id must be a UUID") from error
        if artifact_id != canonical_artifact_id:
            raise ValueError("artifact_id must be a canonical lowercase UUID")

        digest = _evidence_text(item.get("sha256"), name="sha256")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise ValueError("sha256 must be canonical lowercase SHA-256")

        ref: dict[str, str] = {
            "artifact_id": artifact_id,
            "sha256": digest,
            "observed_at": _instant(
                _evidence_text(item.get("observed_at"), name="observed_at"),
                name="observed_at",
            ),
        }
        for optional in ("source_uri", "rights_id"):
            if item.get(optional) is not None:
                ref[optional] = _evidence_text(item.get(optional), name=optional)

        if artifact_id in seen_artifact_ids:
            raise ValueError("evidence_refs must have unique artifact_id values")
        seen_artifact_ids.add(artifact_id)
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


@dataclass(frozen=True)
class PreparedOrderMutation:
    """One order mutation derived from one immutable order-journal cut."""

    event_key: str
    operation: str
    request: dict[str, object]
    mutation_hash: str
    snapshot: OrderSnapshot
    snapshot_payload: dict[str, object]
    event_id: str
    aggregate_version: int
    journal_sequence_cut: int
    envelope: dict[str, object] | None
    outbox_topic: str | None
    already_committed: bool = False


_ORDER_STATE_FIELDS = frozenset(
    {
        "store",
        "provider_id",
        "account_id",
        "environment",
        "host_id",
        "owner_epoch",
        "evidence_artifact_store",
        "_provider_evidence_reader",
        "aggregate_id",
        "_book",
        "_idempotency",
    }
)
_ORDER_SCOPE_FIELDS = (
    "provider_id",
    "account_id",
    "environment",
    "host_id",
    "owner_epoch",
    "aggregate_id",
)


def _order_projection_binding_operations():
    bindings = {}
    lock = RLock()
    artifact_store_type = ArtifactStore
    trusted_reader_factory = trusted_authenticated_reader

    def evidence_namespace(evidence):
        if evidence is None:
            return None
        if type(evidence) is not artifact_store_type:
            raise TypeError(
                "evidence_artifact_store must be the exact canonical ArtifactStore"
            )
        state = object.__getattribute__(evidence, "__dict__")
        values = tuple(
            state[name]
            for name in ("root", "objects", "manifests", "staging", "lock_path")
        )
        return tuple((type(value), value) for value in values)

    def current_entry(value):
        object_id = id(value)
        entry = bindings.get(object_id)
        if entry is None:
            return None
        owner_ref = entry[0]
        owner = owner_ref()
        if owner is value:
            return entry
        if owner is None:
            bindings.pop(object_id, None)
            return None
        raise OrderProjectionConflict(
            "durable OMS selection authority identity collision"
        )

    def registered(value):
        with lock:
            return current_entry(value) is not None

    def bind(value):
        with lock:
            if current_entry(value) is not None:
                raise OrderProjectionConflict(
                    "durable OMS composition is already initialized"
                )
            state = object.__getattribute__(value, "__dict__")
            store = state["store"]
            identity = require_exact_journal_store_authority(
                store,
                subject="durable OMS JournalStore",
            )
            scope = tuple(state[name] for name in _ORDER_SCOPE_FIELDS)
            evidence = state["evidence_artifact_store"]
            frozen_evidence_namespace = evidence_namespace(evidence)
            trusted_reader = None
            if evidence is not None:
                evidence_state = object.__getattribute__(evidence, "__dict__")
                try:
                    trusted_reader = trusted_reader_factory(
                        evidence_state["root"],
                        publication_store=evidence,
                    )
                except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
                    raise OrderProjectionConflict(
                        "provider evidence trusted reader authority is unavailable"
                    ) from error

            # Keep every registry weakref callback-free. Python exposes weakref
            # callbacks through weakref.getweakrefs(), so a cleanup callback on
            # a live OMS would itself become a caller-invokable trust-binding
            # eraser. The live projection owns store/evidence through its normal
            # immutable state and owns the trusted reader through a private
            # immutable field; the registry retains only weak references.
            owner_ref = weakref.ref(value)
            store_ref = weakref.ref(store)
            evidence_ref = None if evidence is None else weakref.ref(evidence)
            reader_ref = (
                None if trusted_reader is None else weakref.ref(trusted_reader)
            )
            object.__setattr__(
                value,
                "_provider_evidence_reader",
                trusted_reader,
            )
            object_id = id(value)
            bindings[object_id] = (
                owner_ref,
                store_ref,
                identity,
                scope,
                evidence_ref,
                frozen_evidence_namespace,
                reader_ref,
            )

            # Publish no partially replayed financial authority to competing
            # threads. The RLock remains held across initial durable replay;
            # re-entrant require() calls from this initializer are permitted,
            # while other threads block until replay either completes or the
            # unpublished binding is removed.
            try:
                DurableOrderBookProjection._reload(value)
            except BaseException:
                entry = bindings.get(object_id)
                if entry is not None and entry[0]() is value:
                    bindings.pop(object_id, None)
                state = object.__getattribute__(value, "__dict__")
                if state.get("_provider_evidence_reader") is trusted_reader:
                    object.__setattr__(
                        value,
                        "_provider_evidence_reader",
                        None,
                    )
                raise

    def require(value):
        if type(value) is not DurableOrderBookProjection:
            raise TypeError("OMS must be exact DurableOrderBookProjection")
        with lock:
            entry = current_entry(value)
            if entry is None:
                raise OrderProjectionConflict(
                    "durable OMS selection authority is unavailable"
                )
            (
                _,
                store_ref,
                identity,
                scope,
                evidence_ref,
                frozen_evidence_namespace,
                reader_ref,
            ) = entry
            store = store_ref()
            evidence = None if evidence_ref is None else evidence_ref()
            trusted_reader = None if reader_ref is None else reader_ref()
            if store is None:
                raise OrderProjectionConflict(
                    "durable OMS selected store authority was lost"
                )
            if evidence_ref is not None and evidence is None:
                raise OrderProjectionConflict(
                    "provider evidence ArtifactStore authority was lost"
                )
            if reader_ref is not None and trusted_reader is None:
                raise OrderProjectionConflict(
                    "provider evidence trusted reader authority was released while OMS is live"
                )

            state = object.__getattribute__(value, "__dict__")
            if (
                type(state) is not dict
                or any(type(key) is not str for key in state)
                or set(state) != _ORDER_STATE_FIELDS
            ):
                raise OrderProjectionConflict("durable OMS instance state is shadowed")
            if any(
                type(state[name]) is not str or state[name] != selected
                for name, selected in zip(_ORDER_SCOPE_FIELDS, scope)
            ):
                raise OrderProjectionConflict("durable OMS scope changed")
            if state["store"] is not store or state["evidence_artifact_store"] is not evidence:
                raise OrderProjectionConflict("durable OMS selected store changed")
            if state["_provider_evidence_reader"] is not trusted_reader:
                raise OrderProjectionConflict(
                    "provider evidence trusted reader authority changed"
                )
            if (
                require_exact_journal_store_authority(
                    store,
                    subject="durable OMS JournalStore",
                )
                != identity
            ):
                raise OrderProjectionConflict(
                    "durable OMS JournalStore generation changed"
                )
            if evidence is not None:
                if type(evidence) is not artifact_store_type:
                    raise OrderProjectionConflict(
                        "provider evidence ArtifactStore authority changed"
                    )
                try:
                    current_namespace = evidence_namespace(evidence)
                except (TypeError, KeyError) as error:
                    raise OrderProjectionConflict(
                        "provider evidence ArtifactStore namespace authority changed"
                    ) from error
                if current_namespace != frozen_evidence_namespace:
                    raise OrderProjectionConflict(
                        "provider evidence ArtifactStore namespace authority changed"
                    )
                if trusted_reader is None:
                    raise OrderProjectionConflict(
                        "provider evidence trusted reader authority is unavailable"
                    )
            elif trusted_reader is not None:
                raise OrderProjectionConflict(
                    "provider evidence trusted reader authority is inconsistent"
                )
            return store, identity

    def read_provider_evidence(value, artifact_id: str):
        require(value)
        with lock:
            entry = current_entry(value)
            if entry is None:
                raise OrderProjectionConflict(
                    "durable OMS selection authority is unavailable"
                )
            reader_ref = entry[6]
            trusted_reader = None if reader_ref is None else reader_ref()
            if trusted_reader is None:
                raise OrderProjectionConflict(
                    "provider evidence trusted reader authority is unavailable"
                )
        return trusted_reader(artifact_id)

    return registered, bind, require, read_provider_evidence

(
    _order_projection_is_registered,
    _bind_order_projection,
    require_exact_order_projection_authority,
    _read_authenticated_provider_evidence,
) = _order_projection_binding_operations()


class DurableOrderBookProjection:
    """Crash-recoverable adapter over the single canonical OrderBookProjection."""

    def __getattribute__(self, name):
        if not name.startswith("__") and _order_projection_is_registered(self):
            require_exact_order_projection_authority(self)
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if _order_projection_is_registered(self) and (
            name in _ORDER_STATE_FIELDS - {"_book", "_idempotency"}
            or any(name in base.__dict__ for base in DurableOrderBookProjection.__mro__)
        ):
            raise OrderProjectionConflict("durable OMS authority state is immutable")
        object.__setattr__(self, name, value)

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
        if type(self) is not DurableOrderBookProjection:
            raise TypeError("OMS must be exact DurableOrderBookProjection")
        if _order_projection_is_registered(self):
            raise OrderProjectionConflict(
                "durable OMS composition is already initialized"
            )
        require_exact_journal_store_authority(store, subject="durable OMS JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, name="provider_id").upper()
        self.account_id = _text(account_id, name="account_id")
        self.environment = _environment(environment)
        self.host_id = _text(host_id, name="host_id")
        self.owner_epoch = _text(owner_epoch, name="owner_epoch")
        if evidence_artifact_store is not None and type(evidence_artifact_store) is not ArtifactStore:
            raise TypeError(
                "evidence_artifact_store must be the exact canonical ArtifactStore"
            )
        self.evidence_artifact_store = evidence_artifact_store
        self._provider_evidence_reader = None
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
        _bind_order_projection(self)

    def _new_book(self) -> OrderBookProjection:
        return OrderBookProjection(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
        )

    def _events(self) -> list[dict[str, object]]:
        store, identity = require_exact_order_projection_authority(self)
        with journal_store_authority_scope(store, identity):
            return JournalStore.load_events(store, _AGGREGATE_TYPE, self.aggregate_id)

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
        _read_authenticated_snapshot=_read_authenticated_provider_evidence,
    ) -> tuple[dict[str, str], ...]:
        requires = self._requires_provider_evidence(operation, request)
        if self.environment in {"PAPER", "LIVE"} and requires:
            # Generic ArtifactStore integrity proves retained bytes only. It
            # cannot prove provider origin or adapter/route semantics, and a
            # caller can publish self-consistent bytes plus matching metadata.
            # Until a sealed provider-origin/normalizer issuer is composed into
            # this projection, production-like lifecycle mutations must remain
            # fail-closed rather than accepting injectable storage evidence.
            raise OrderProjectionConflict(
                "PAPER/LIVE provider lifecycle mutation requires sealed provider-origin authority"
            )
        refs = _canonical_evidence_refs(evidence_refs)
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
                manifest, artifact_bytes = _read_authenticated_snapshot(
                    self,
                    ref["artifact_id"],
                )
            except (FileNotFoundError, ArtifactIntegrityError, OSError, ValueError) as error:
                raise OrderProjectionConflict(
                    "provider evidence artifact is not resolvable and intact"
                ) from error
            artifact_digest = "sha256:" + sha256(artifact_bytes).hexdigest()
            if (
                manifest.get("sha256") != ref["sha256"]
                or artifact_digest != ref["sha256"]
            ):
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
        elif operation == "REJECT_CANCEL":
            order.reject_cancel(
                command_id=request.get("command_id"),
                reason_code=request.get("reason_code"),
            )
        elif operation == "REQUEST_REPLACE":
            order.request_replace(command_id=request.get("command_id"))
        elif operation == "REJECT_REPLACE":
            order.reject_replace(
                command_id=request.get("command_id"),
                reason_code=request.get("reason_code"),
            )
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

    def _prepare(
        self,
        *,
        event_key: str,
        operation: str,
        request: dict[str, object],
        committed_at: str,
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> PreparedOrderMutation:
        """Prepare one durable order mutation without publishing it.

        The mutation and aggregate version are derived from one immutable replay
        cut. JournalStore aggregate-version enforcement then fences a concurrent
        writer if a wider atomic command attempts to publish this plan.
        """

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

        store, identity = require_exact_order_projection_authority(self)
        with journal_store_authority_scope(store, identity):
            journal_sequence_cut = JournalStore.current_journal_sequence(store)
        events = self._events()
        candidate, idempotency = self._replay(events)
        prior = idempotency.get(key)
        if prior is not None:
            if prior[0] != mutation_hash:
                raise OrderProjectionConflict(
                    "event_key was already used for a different order request"
                )
            matching = [
                event
                for event in events
                if str(event.get("event_id")) == prior[2]
            ]
            if len(matching) != 1:
                raise OrderProjectionConflict(
                    "committed order mutation lacks one canonical durable event"
                )
            envelope = dict(matching[0])
            envelope.pop("journal_sequence")
            envelope["aggregate_version"] = str(envelope["aggregate_version"])
            return PreparedOrderMutation(
                event_key=key,
                operation=operation,
                request=dict(request),
                mutation_hash=mutation_hash,
                snapshot=prior[1],
                snapshot_payload=_snapshot_payload(prior[1]),
                event_id=prior[2],
                aggregate_version=int(matching[0]["aggregate_version"]),
                journal_sequence_cut=journal_sequence_cut,
                envelope=envelope,
                outbox_topic=_OUTBOX_TOPIC,
                already_committed=True,
            )

        snapshot = self._apply(candidate, operation, request)
        snapshot_payload = _snapshot_payload(snapshot)
        payload = {
            "schema_version": "1.0.0",
            "scope": self._scope_payload(),
            "event_key": key,
            "operation": operation,
            "request": request,
            "request_hash": request_hash,
            "snapshot": snapshot_payload,
        }
        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/order-projection/"
                + canonical_json([self.aggregate_id, key, mutation_hash]),
            )
        )
        envelope = {
            "event_id": event_id,
            "event_type": _EVENT_TYPE,
            "schema_version": "1.0.0",
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(next_version),
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
        return PreparedOrderMutation(
            event_key=key,
            operation=operation,
            request=dict(request),
            mutation_hash=mutation_hash,
            snapshot=snapshot,
            snapshot_payload=snapshot_payload,
            event_id=event_id,
            aggregate_version=next_version,
            journal_sequence_cut=journal_sequence_cut,
            envelope=envelope,
            outbox_topic=_OUTBOX_TOPIC,
        )

    def _commit_prepared(
        self,
        plan: PreparedOrderMutation,
    ) -> DurableOrderMutationResult:
        if type(plan) is not PreparedOrderMutation:
            raise TypeError("plan must be exact PreparedOrderMutation")
        if plan.already_committed:
            self._reload()
            return DurableOrderMutationResult(
                event_id=plan.event_id,
                inserted=False,
                snapshot=plan.snapshot,
            )
        if plan.envelope is None:
            raise OrderProjectionConflict(
                "fresh prepared order mutation is missing its durable event"
            )
        try:
            store, identity = require_exact_order_projection_authority(self)
            with journal_store_authority_scope(store, identity):
                append_result = JournalStore.append_event(
                    store,
                    plan.envelope,
                    outbox_topic=plan.outbox_topic,
                )
        except Exception:
            self._reload()
            raise
        self._reload()
        recorded = self._idempotency.get(plan.event_key)
        if recorded is None:
            raise RuntimeError("durable order event was not replayed after commit")
        if recorded[0] != plan.mutation_hash or recorded[2] != plan.event_id:
            raise OrderProjectionConflict(
                "committed order mutation differs from prepared authority"
            )
        return DurableOrderMutationResult(
            event_id=recorded[2],
            inserted=append_result.inserted,
            snapshot=recorded[1],
        )

    def _commit(
        self,
        *,
        event_key: str,
        operation: str,
        request: dict[str, object],
        committed_at: str,
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> DurableOrderMutationResult:
        return self._commit_prepared(
            self._prepare(
                event_key=event_key,
                operation=operation,
                request=request,
                committed_at=committed_at,
                evidence_refs=evidence_refs,
            )
        )

    def refresh(self) -> None:
        """Reload the order projection after an external atomic commit."""
        self._reload()

    @property
    def snapshots(self) -> tuple[OrderSnapshot, ...]:
        self._reload()
        return self._book.snapshots()

    def order(self, client_order_id: str):
        self._reload()
        return self._book.order(client_order_id)

    def effective_fills(self):
        self._reload()
        return self._book.effective_fills()

    def oco_breaches(self):
        self._reload()
        return self._book.oco_breaches()

    def active_oco_breaches(self):
        self._reload()
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
        store, identity = require_exact_order_projection_authority(self)
        with journal_store_authority_scope(store, identity):
            events = JournalStore.load_events(store, "submission_attempt", aggregate_id)
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
            environment = _environment(prepared_payload.get("environment"))
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
                if isinstance(payload, dict) and _has_exact_response_markers(payload):
                    # WP-18 proves that one exact provider response returned for
                    # this exact send. It does *not* prove the provider-specific
                    # lifecycle meaning of those bytes. Revalidate the sealed
                    # durable binding here, then keep the generic order
                    # projection UNKNOWN until a provider-specific authenticated
                    # normalizer supplies ACCEPTED/REJECTED evidence. In
                    # particular, transport success/HTTP 2xx must never be
                    # promoted to acknowledgement or fill authority.
                    try:
                        with journal_store_authority_scope(store, identity):
                            binding = load_submission_response_binding(
                                store,
                                environment=self.environment,
                                account_id=self.account_id,
                                attempt_id=attempt,
                            )
                        bound = submission_response_binding_projection(binding)
                    except (KeyError, TypeError, ValueError, RuntimeError) as error:
                        raise OrderProjectionConflict(
                            "exact submission response binding is invalid"
                        ) from error
                    if (
                        bound.get("terminal_state") != "SENT"
                        or bound.get("attempt_id") != attempt
                        or bound.get("client_order_id") != client_order_id
                        or bound.get("environment") != self.environment
                        or bound.get("account_id") != self.account_id
                        or type(bound.get("provider")) is not str
                        or bound["provider"].upper() != provider
                    ):
                        raise OrderProjectionConflict(
                            "exact submission response binding differs from durable attempt"
                        )
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

                # Historical marker-free rows may already contain one
                # provider-neutral normalized lifecycle result. Preserve this
                # compatibility path, but do not use it for the canonical exact
                # WP-18 response contract above.
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
                payload = event.get("payload")
                if isinstance(payload, dict) and _has_exact_response_markers(payload):
                    # Ambiguous post-SEND bytes remain transport evidence only,
                    # but replay must still revalidate the exact durable WP-18
                    # response binding before projecting UNKNOWN. Otherwise a
                    # malformed/substituted terminal row could cross the stable
                    # WP-18 -> WP-19 handoff without exact-response authority.
                    try:
                        with journal_store_authority_scope(store, identity):
                            binding = load_submission_response_binding(
                                store,
                                environment=self.environment,
                                account_id=self.account_id,
                                attempt_id=attempt,
                            )
                        bound = submission_response_binding_projection(binding)
                    except (KeyError, TypeError, ValueError, RuntimeError) as error:
                        raise OrderProjectionConflict(
                            "exact submission response binding is invalid"
                        ) from error
                    if (
                        bound.get("terminal_state") != "UNKNOWN"
                        or bound.get("retry_disposition") != "RECONCILE_FIRST"
                        or bound.get("attempt_id") != attempt
                        or bound.get("client_order_id") != client_order_id
                        or bound.get("environment") != self.environment
                        or bound.get("account_id") != self.account_id
                        or type(bound.get("provider")) is not str
                        or bound["provider"].upper() != provider
                    ):
                        raise OrderProjectionConflict(
                            "exact ambiguous response binding differs from durable attempt"
                        )
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
                continue

        return tuple(results)

    def prepare_record_fill_mutation(
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
    ) -> PreparedOrderMutation:
        """Prepare a provider fill for composition with canonical finances."""

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
        return self._prepare(
            event_key=event_key,
            operation="RECORD_FILL",
            request=request,
            committed_at=committed_at,
            evidence_refs=evidence_refs,
        )

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
            evidence_refs=evidence_refs,
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
            evidence_refs=evidence_refs,
        )

    def prepare_bust_fill_mutation(
        self,
        *,
        event_key: str,
        client_order_id: str,
        fill_id: str,
        provider_revision: str,
        committed_at: str,
        correction_fill_id: str | None = None,
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> PreparedOrderMutation:
        """Prepare a provider-evidenced fill bust for atomic financial reversal."""

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
        return self._prepare(
            event_key=event_key,
            operation="BUST_FILL",
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

    def reject_cancel(
        self,
        *,
        event_key: str,
        client_order_id: str,
        command_id: str,
        reason_code: str,
        committed_at: str,
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> DurableOrderMutationResult:
        return self._commit(
            event_key=event_key,
            operation="REJECT_CANCEL",
            request={
                "client_order_id": _text(
                    client_order_id,
                    name="client_order_id",
                ),
                "command_id": _text(command_id, name="command_id"),
                "reason_code": _text(reason_code, name="reason_code"),
            },
            committed_at=committed_at,
            evidence_refs=evidence_refs,
        )

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

    def reject_replace(
        self,
        *,
        event_key: str,
        client_order_id: str,
        command_id: str,
        reason_code: str,
        committed_at: str,
        evidence_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> DurableOrderMutationResult:
        return self._commit(
            event_key=event_key,
            operation="REJECT_REPLACE",
            request={
                "client_order_id": _text(client_order_id, name="client_order_id"),
                "command_id": _text(command_id, name="command_id"),
                "reason_code": _text(reason_code, name="reason_code"),
            },
            committed_at=committed_at,
            evidence_refs=evidence_refs,
        )

    def confirm_expired(self, **kwargs) -> DurableOrderMutationResult:
        return self._simple(operation="CONFIRM_EXPIRED", **kwargs)
