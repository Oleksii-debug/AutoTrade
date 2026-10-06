"""Durable journal-backed projection for reservation authority.

This module reuses the canonical JournalStore; it does not create a second
persistence database.  Every reservation mutation is first committed as one
versioned journal event, then the in-memory ReservationBook is rebuilt from
that durable history.  A crash between journal commit and process projection is
therefore recovered by replay, while a stale concurrent writer fails the
JournalStore aggregate-version check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Mapping
from uuid import UUID, NAMESPACE_URL, uuid5
import weakref

from research.autotrade_research.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from research.autotrade_research.io.strict_json import strict_json_loads

from .dispatch import submission_attempt_aggregate_id
from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_bounded_exact_decimal,
)
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .reservations import (
    ReservationBook,
    ReservationConflict,
    ReservationSnapshot,
)


_AGGREGATE_TYPE = "reservation_book"
_EVENT_TYPE = "ReservationMutationCommitted"
_COMMAND_ACTOR = "autotrade-reservation-authority"
_RESOLUTION_MEDIA_TYPE = "application/vnd.autotrade.reservation-resolution+json"
_RESOLUTION_EVIDENCE_TYPE = "AUTOTRADE_RESERVATION_RESOLUTION"
_RESOLUTION_SCHEMA_VERSION = 2


def _text(value: str, *, name: str) -> str:
    # Durable reservation identities are authority-bearing.  Do not invoke
    # caller-controlled str subclass methods while deriving journal scope.
    if type(value) is not str:
        raise ValueError(f"{name} is required")
    normalized = str.strip(value)
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized



def _immutable_evidence_ref(value: str) -> tuple[str, str, str]:
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
    return artifact_id, digest, f"artifact:{artifact_id}@sha256:{digest}"

def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(
            f"{name} must be an exact finite decimal within the resource envelope"
        ) from error


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise ValueError(
            "reservation decimal exceeds exact decimal authority"
        ) from error


def _amount_map(values: dict[str, object], *, allow_zero: bool) -> dict[str, str]:
    if type(values) is not dict:
        raise TypeError("resource amounts must use an exact dict")
    items = tuple(dict.items(values))
    if not items:
        raise ValueError("resource amounts are required")
    result: dict[str, str] = {}
    for resource, raw in items:
        key = _text(resource, name="resource")
        if key in result:
            raise ValueError("resource names must be unique after normalization")
        amount = _decimal(raw, name=f"amount[{key}]")
        if amount < 0 or (amount == 0 and not allow_zero):
            raise ValueError("resource amounts must be positive")
        result[key] = _decimal_text(amount)
    return dict(sorted(result.items()))


def _require_inert_json(value: object, *, name: str) -> None:
    """Reject executable JSON-like subclasses before financial replay hashing."""

    if value is None or type(value) in {str, int, float, bool}:
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _require_inert_json(item, name=f"{name}[{index}]")
        return
    if type(value) is dict:
        for key, item in dict.items(value):
            if type(key) is not str:
                raise ReservationConflict(
                    f"{name} object keys must be exact text"
                )
            _require_inert_json(item, name=f"{name}.{key}")
        return
    raise ReservationConflict(
        f"{name} must contain only exact inert JSON values"
    )


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


def reservation_snapshot_digest(snapshot: ReservationSnapshot) -> str:
    """Return the reservation authority's canonical identity for one state cut."""

    if type(snapshot) is not ReservationSnapshot:
        raise TypeError("snapshot must be exact ReservationSnapshot")
    return payload_digest(_snapshot_payload(snapshot))


def _canonical_sha256(value: object, *, name: str) -> str:
    digest = _text(value, name=name)
    if (
        not digest.startswith("sha256:")
        or len(digest) != 71
        or any(ch not in "0123456789abcdef" for ch in digest[7:])
    ):
        raise ReservationConflict(f"{name} must be canonical SHA-256")
    return digest


def _filled_resolution_evidence(request: Mapping[str, object]) -> str:
    """Bind reservation FILLED authority to one exact durable OMS fill cut."""

    event_id = _text(request.get("order_fill_event_id"), name="order_fill_event_id")
    try:
        canonical_event_id = str(UUID(event_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ReservationConflict("order_fill_event_id must be a canonical UUID") from error
    if canonical_event_id != event_id:
        raise ReservationConflict("order_fill_event_id must be a canonical UUID")
    payload_hash = _canonical_sha256(
        request.get("order_fill_payload_hash"),
        name="order_fill_payload_hash",
    )
    snapshot_digest = _canonical_sha256(
        request.get("order_fill_snapshot_digest"),
        name="order_fill_snapshot_digest",
    )
    mutation_hash = _canonical_sha256(
        request.get("order_fill_mutation_hash"),
        name="order_fill_mutation_hash",
    )
    _text(request.get("order_fill_provider_id"), name="order_fill_provider_id")
    _text(request.get("order_fill_client_order_id"), name="order_fill_client_order_id")
    _text(request.get("order_fill_fill_id"), name="order_fill_fill_id")
    _text(
        request.get("order_fill_provider_execution_id"),
        name="order_fill_provider_execution_id",
    )
    return (
        f"journal:order-fill:{canonical_event_id}"
        f"@payload:{payload_hash}"
        f"@snapshot:{snapshot_digest}"
        f"@mutation:{mutation_hash}"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _environment(value: str) -> str:
    if type(value) is not str:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    normalized = str.upper(str.strip(value))
    if normalized not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    return normalized


def _journal_identity(
    environment: str,
    account_id: str,
    kind: str,
    external_id: str,
) -> str:
    """Scope generic JournalStore identities to one account/environment tuple."""

    environment = _environment(environment)
    account = _text(account_id, name="account_id")
    identity_kind = _text(kind, name="identity_kind")
    external = _text(external_id, name="external_id")
    canonical = canonical_json(
        [environment, account, identity_kind, external]
    )
    return str(uuid5(NAMESPACE_URL, "reservation-identity:" + canonical))


@dataclass(frozen=True)
class PreparedReservationMutation:
    """One reservation mutation prepared from a single durable journal cut."""

    snapshot: ReservationSnapshot
    snapshot_payload: dict[str, object]
    envelope: dict[str, object] | None
    idempotency_key: str
    request: dict[str, object]
    aggregate_version: int
    already_committed: bool = False


@dataclass(frozen=True)
class _DurableReservationStoreBinding:
    store_ref: weakref.ReferenceType
    store_identity: object
    environment: str
    account_id: str
    scope_id: str
    resolution_artifact_store_ref: weakref.ReferenceType | None
    resolution_artifact_reader_ref: weakref.ReferenceType | None


def _build_reservation_store_binding_accessors():
    """Retain reservation composition in callback-free process state.

    WeakKeyDictionary is intentionally avoided here. Its internal weakref
    carries a caller-discoverable removal callback; manually invoking that
    callback can erase a live trust binding and make reinitialization appear to
    be first composition. Callback-free weakrefs plus identity checks preserve
    fail-closed binding semantics. The registry weak-references selected
    authority resources too, so a dead owner cannot retain JournalStore,
    ArtifactStore, or retained-reader capabilities until a future access.
    """

    bindings: dict[
        int,
        tuple[weakref.ReferenceType, _DurableReservationStoreBinding],
    ] = {}
    lock = RLock()

    def prune_dead() -> None:
        with lock:
            dead = [
                object_id
                for object_id, (value_ref, _binding) in bindings.items()
                if value_ref() is None
            ]
            for object_id in dead:
                bindings.pop(object_id, None)

    def registered_binding(
        value: object,
    ) -> _DurableReservationStoreBinding | None:
        object_id = id(value)
        with lock:
            entry = bindings.get(object_id)
            if entry is None:
                return None
            value_ref, binding = entry
            current = value_ref()
            if current is value:
                return binding
            if current is None:
                bindings.pop(object_id, None)
                return None
            raise ReservationConflict(
                "durable reservation binding identity collision"
            )

    def is_registered(value: object) -> bool:
        try:
            prune_dead()
            return registered_binding(value) is not None
        except TypeError:
            return False

    def initialize(
        value: object,
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        resolution_artifact_store: ArtifactStore | None = None,
        resolution_artifact_root: str | Path | None = None,
    ) -> None:
        if type(value) is not DurableReservationBook:
            raise TypeError("reservation book must be exact DurableReservationBook")
        prune_dead()
        if registered_binding(value) is not None:
            raise ReservationConflict(
                "reservation store authority is already established"
            )
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        try:
            identity = require_exact_journal_store_authority(
                store,
                subject="durable reservation JournalStore",
            )
        except (TypeError, RuntimeError) as error:
            raise ReservationConflict(
                "durable reservation JournalStore authority is invalid"
            ) from error

        normalized_environment = _environment(environment)
        normalized_account = _text(account_id, name="account_id")
        if (
            resolution_artifact_store is not None
            and type(resolution_artifact_store) is not ArtifactStore
        ):
            raise TypeError(
                "resolution_artifact_store must be the canonical ArtifactStore or None"
            )
        if resolution_artifact_store is not None and resolution_artifact_root is None:
            raise TypeError(
                "resolution_artifact_root is required when a publication store is supplied"
            )
        reader = (
            None
            if resolution_artifact_root is None
            else trusted_authenticated_reader(
                resolution_artifact_root,
                publication_store=resolution_artifact_store,
            )
        )
        scope_id = _journal_identity(
            normalized_environment,
            normalized_account,
            "scope",
            "reservation-book",
        )

        object_id = id(value)
        binding = _DurableReservationStoreBinding(
            store_ref=weakref.ref(store),
            store_identity=identity,
            environment=normalized_environment,
            account_id=normalized_account,
            scope_id=scope_id,
            resolution_artifact_store_ref=(
                None
                if resolution_artifact_store is None
                else weakref.ref(resolution_artifact_store)
            ),
            resolution_artifact_reader_ref=(
                None if reader is None else weakref.ref(reader)
            ),
        )
        # The initial precheck is only a fast-fail optimization. A concurrent
        # explicit __init__ can pass it on the same live object, so the
        # authority selection and visible state must be published under one
        # lock with a second exact-identity check. Never let a later initializer
        # overwrite the first closure-owned financial authority.
        with lock:
            entry = bindings.get(object_id)
            if entry is not None:
                current = entry[0]()
                if current is value:
                    raise ReservationConflict(
                        "reservation store authority is already established"
                    )
                if current is not None:
                    raise ReservationConflict(
                        "durable reservation binding identity collision"
                    )
                bindings.pop(object_id, None)

            object.__setattr__(value, "store", store)
            object.__setattr__(value, "environment", normalized_environment)
            object.__setattr__(value, "account_id", normalized_account)
            object.__setattr__(
                value,
                "resolution_artifact_store",
                resolution_artifact_store,
            )
            object.__setattr__(value, "_resolution_artifact_reader", reader)
            object.__setattr__(value, "scope_id", scope_id)
            object.__setattr__(value, "_book", ReservationBook())
            object.__setattr__(value, "_idempotency", {})
            bindings[object_id] = (weakref.ref(value), binding)
            # Keep construction atomic to every other thread. The binding is
            # now present for this thread's re-entrant _reload() calls, but the
            # same registry lock prevents another thread from observing the
            # authority before durable replay has reconstructed _book and
            # _idempotency. On replay failure the binding disappears before
            # any other thread can acquire it.
            try:
                DurableReservationBook._reload(value)
            except BaseException:
                entry = bindings.get(object_id)
                if entry is not None and entry[0]() is value:
                    bindings.pop(object_id, None)
                raise

    def require(value: object) -> tuple[JournalStore, object, str]:
        if type(value) is not DurableReservationBook:
            raise TypeError("reservation book must be exact DurableReservationBook")
        prune_dead()
        binding = registered_binding(value)
        if binding is None:
            raise ReservationConflict(
                "durable reservation store authority is not established"
            )
        state = object.__getattribute__(value, "__dict__")
        state_keys = tuple(state)
        if any(type(name) is not str for name in state_keys):
            raise ReservationConflict(
                "durable reservation instance state keys must be exact str"
            )
        class_owned_names = {
            name for base in DurableReservationBook.__mro__ for name in base.__dict__
        }
        if class_owned_names.intersection(state_keys):
            raise ReservationConflict(
                "durable reservation instance state is shadowed"
            )
        store = binding.store_ref()
        artifact_store = (
            None
            if binding.resolution_artifact_store_ref is None
            else binding.resolution_artifact_store_ref()
        )
        artifact_reader = (
            None
            if binding.resolution_artifact_reader_ref is None
            else binding.resolution_artifact_reader_ref()
        )
        if store is None:
            raise ReservationConflict(
                "durable reservation JournalStore was released while book is live"
            )
        if (
            binding.resolution_artifact_store_ref is not None
            and artifact_store is None
        ) or (
            binding.resolution_artifact_reader_ref is not None
            and artifact_reader is None
        ):
            raise ReservationConflict(
                "durable reservation evidence authority was released while book is live"
            )
        if state.get("store") is not store:
            raise ReservationConflict(
                "durable reservation JournalStore changed after construction"
            )
        if (
            state.get("environment") != binding.environment
            or state.get("account_id") != binding.account_id
            or state.get("scope_id") != binding.scope_id
        ):
            raise ReservationConflict(
                "durable reservation scope changed after construction"
            )
        if (
            state.get("resolution_artifact_store") is not artifact_store
            or state.get("_resolution_artifact_reader") is not artifact_reader
        ):
            raise ReservationConflict(
                "durable reservation evidence authority changed after construction"
            )
        try:
            current = require_exact_journal_store_authority(
                store,
                subject="durable reservation JournalStore",
            )
        except (TypeError, RuntimeError) as error:
            raise ReservationConflict(
                "durable reservation JournalStore authority changed"
            ) from error
        if current != binding.store_identity:
            raise ReservationConflict(
                "durable reservation JournalStore generation changed"
            )
        return store, binding.store_identity, binding.scope_id

    return is_registered, initialize, require


(
    _reservation_store_binding_registered,
    _initialize_reservation_store_binding,
    _require_reservation_store_binding,
) = _build_reservation_store_binding_accessors()
del _build_reservation_store_binding_accessors


def _reservation_store_load_events(
    value: object,
    aggregate_type: str,
    aggregate_id: str,
) -> list[dict[str, object]]:
    store, identity, _ = _require_reservation_store_binding(value)
    with journal_store_authority_scope(store, identity):
        return JournalStore.load_events(store, aggregate_type, aggregate_id)


def _reservation_store_get_event(
    value: object,
    event_id: str,
) -> dict[str, object] | None:
    store, identity, _ = _require_reservation_store_binding(value)
    with journal_store_authority_scope(store, identity):
        return JournalStore.get_event(store, event_id)


def _reservation_store_commit_command(value: object, **kwargs):
    store, identity, _ = _require_reservation_store_binding(value)
    with journal_store_authority_scope(store, identity):
        return JournalStore.commit_command(store, **kwargs)


class DurableReservationBook:
    """ReservationBook projection with crash/restart and dedupe semantics."""

    def __init__(
        self,
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        resolution_artifact_store: ArtifactStore | None = None,
        resolution_artifact_root: str | Path | None = None,
    ):
        _initialize_reservation_store_binding(
            self,
            store,
            environment=environment,
            account_id=account_id,
            resolution_artifact_store=resolution_artifact_store,
            resolution_artifact_root=resolution_artifact_root,
        )

    _BOUND_AUTHORITY_STATE = frozenset(
        {
            "store",
            "environment",
            "account_id",
            "scope_id",
            "resolution_artifact_store",
            "_resolution_artifact_reader",
        }
    )

    def __getattribute__(self, name: str):
        if (
            type(name) is str
            and name != "__dict__"
            and _reservation_store_binding_registered(self)
        ):
            _require_reservation_store_binding(self)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: object) -> None:
        if _reservation_store_binding_registered(self):
            class_owned = any(
                name in base.__dict__ for base in DurableReservationBook.__mro__
            )
            if name in DurableReservationBook._BOUND_AUTHORITY_STATE or class_owned:
                raise ReservationConflict(
                    "durable reservation authority state is immutable"
                )
        object.__setattr__(self, name, value)

    def _events(self) -> list[dict[str, object]]:
        _, _, scope_id = _require_reservation_store_binding(self)
        return _reservation_store_load_events(self, _AGGREGATE_TYPE, scope_id)

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
            _require_inert_json(event, name="reservation journal event")
            if type(event) is not dict:
                raise ReservationConflict(
                    "reservation journal event must be an exact object"
                )
            if event["aggregate_version"] != expected_version:
                raise ReservationConflict(
                    "reservation journal aggregate versions are not contiguous"
                )
            expected_version += 1
            if event["event_type"] != _EVENT_TYPE:
                raise ReservationConflict(
                    "reservation journal contains an unsupported event type"
                )
            payload = event.get("payload")
            if type(payload) is not dict:
                raise ReservationConflict(
                    "reservation event payload must be an exact object"
                )
            if payload_digest(payload) != event["payload_hash"]:
                raise ReservationConflict(
                    "reservation journal payload hash does not match stored payload"
                )
            if payload.get("environment") != self.environment:
                raise ReservationConflict(
                    "reservation journal event environment does not match book scope"
                )
            if payload.get("account_id") != self.account_id:
                raise ReservationConflict(
                    "reservation journal event account does not match book scope"
                )
            operation = payload.get("operation")
            request = payload.get("request")
            expected_snapshot = payload.get("snapshot")
            idem = payload.get("idempotency_key")
            request_hash = payload.get("request_hash")
            if type(request) is not dict:
                raise ReservationConflict(
                    "reservation event request must be an exact object"
                )
            if type(expected_snapshot) is not dict:
                raise ReservationConflict(
                    "reservation event snapshot must be an exact object"
                )
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
                if operation == "MARK_TERMINAL":
                    current = book.get(request.get("reservation_id"))
                    self._verify_resolution_evidence(
                        reservation_id=request.get("reservation_id"),
                        intent_id=current.intent_id,
                        outcome=request.get("outcome"),
                        provider=request.get("provider"),
                        attempt_id=request.get("attempt_id"),
                        resolution_evidence=request.get("resolution_evidence"),
                    )
                elif operation == "MARK_ZERO_WIRE_TERMINAL":
                    current = book.get(request.get("reservation_id"))
                    self._verify_zero_wire_blocked(
                        reservation_id=request.get("reservation_id"),
                        intent_id=current.intent_id,
                        provider=request.get("provider"),
                        attempt_id=request.get("attempt_id"),
                        client_order_id=request.get("client_order_id"),
                        resolution_evidence=request.get("resolution_evidence"),
                    )
                elif operation == "CONSUME_AND_MARK_FILLED":
                    self._verify_durable_order_fill_terminal_evidence(request)
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
        if operation == "CONSUME_AND_MARK_FILLED":
            return book.consume_and_mark_filled(
                request["reservation_id"],
                request["usage"],
                resolution_evidence=_filled_resolution_evidence(request),
            )
        if operation == "RESTORE_CONSUMPTION":
            return book.restore_consumption(
                request["reservation_id"],
                request["usage"],
            )
        if operation == "MARK_UNKNOWN":
            return book.mark_unknown(request["reservation_id"])
        if operation in {"MARK_TERMINAL", "MARK_ZERO_WIRE_TERMINAL"}:
            return book.mark_terminal(
                request["reservation_id"],
                outcome=request["outcome"],
                resolution_evidence=request["resolution_evidence"],
            )
        raise ReservationConflict(f"unsupported reservation operation: {operation}")

    def _verify_durable_order_fill_terminal_evidence(
        self,
        request: Mapping[str, object],
    ) -> str:
        """Verify the exact referenced OMS RECORD_FILL is durable and terminal."""

        expected_evidence = _filled_resolution_evidence(request)
        event_id = _text(
            request.get("order_fill_event_id"),
            name="order_fill_event_id",
        )
        order_event = _reservation_store_get_event(self, event_id)
        if order_event is None:
            raise ReservationConflict(
                "FILLED reservation requires the referenced durable OMS fill event"
            )
        if (
            order_event.get("event_id") != event_id
            or order_event.get("event_type") != "OrderProjectionMutationCommitted"
            or order_event.get("aggregate_type") != "order_projection_book"
        ):
            raise ReservationConflict(
                "referenced OMS fill event has invalid durable identity"
            )
        payload = order_event.get("payload")
        if type(payload) is not dict:
            raise ReservationConflict(
                "referenced OMS fill event payload is invalid"
            )
        actual_payload_hash = payload_digest(payload)
        if (
            actual_payload_hash != order_event.get("payload_hash")
            or actual_payload_hash != request.get("order_fill_payload_hash")
        ):
            raise ReservationConflict(
                "referenced OMS fill event payload hash is invalid"
            )
        scope = payload.get("scope")
        order_request = payload.get("request")
        order_snapshot = payload.get("snapshot")
        evidence_refs = order_event.get("evidence_refs")
        if (
            type(scope) is not dict
            or scope.get("provider_id") != request.get("order_fill_provider_id")
            or scope.get("account_id") != self.account_id
            or scope.get("environment") != self.environment
            or payload.get("operation") != "RECORD_FILL"
            or type(order_request) is not dict
            or type(order_snapshot) is not dict
            or order_snapshot.get("state") != "FILLED"
            or type(evidence_refs) is not list
        ):
            raise ReservationConflict(
                "referenced OMS fill event is not an exact terminal fill for this scope"
            )
        try:
            open_quantity = _decimal(
                order_snapshot.get("open_quantity"),
                name="OMS fill open_quantity",
            )
        except (TypeError, ValueError) as error:
            raise ReservationConflict(
                "referenced OMS fill open quantity is invalid"
            ) from error
        if open_quantity != 0:
            raise ReservationConflict(
                "referenced OMS FILLED snapshot must have zero open quantity"
            )
        if (
            order_request.get("client_order_id")
            != request.get("order_fill_client_order_id")
            or order_request.get("fill_id") != request.get("order_fill_fill_id")
            or order_request.get("provider_execution_id")
            != request.get("order_fill_provider_execution_id")
            or order_snapshot.get("client_order_id")
            != request.get("order_fill_client_order_id")
        ):
            raise ReservationConflict(
                "referenced OMS fill identity differs from reservation authority"
            )
        request_hash = _text(
            payload.get("request_hash"),
            name="OMS fill request_hash",
        )
        if request_hash != payload_digest(order_request):
            raise ReservationConflict(
                "referenced OMS fill request hash is invalid"
            )
        actual_snapshot_digest = payload_digest(order_snapshot)
        if actual_snapshot_digest != request.get("order_fill_snapshot_digest"):
            raise ReservationConflict(
                "referenced OMS fill snapshot differs from reservation authority"
            )
        actual_mutation_hash = payload_digest(
            {
                "request_hash": request_hash,
                "evidence_refs": evidence_refs,
            }
        )
        if actual_mutation_hash != request.get("order_fill_mutation_hash"):
            raise ReservationConflict(
                "referenced OMS fill mutation differs from reservation authority"
            )
        return expected_evidence

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

    def prepare_reserve_mutation(
        self,
        *,
        event_key: str,
        idempotency_key: str,
        reservation_id: str,
        intent_id: str,
        requirements: dict[str, object],
        available: dict[str, object],
        committed_at: str,
    ) -> PreparedReservationMutation:
        """Prepare, but do not commit, a worst-case reservation.

        This is used by the financial admission writer so reservation, risk
        evidence, confirmation consumption, admission and outbox publication
        can share one JournalStore.commit_command transaction. The plan is
        derived from exactly one reservation journal cut; a concurrent writer
        therefore fails the aggregate-version fence at commit rather than
        reusing stale availability.
        """

        key = _text(idempotency_key, name="idempotency_key")
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "intent_id": _text(intent_id, name="intent_id"),
            "requirements": _amount_map(requirements, allow_zero=False),
            "available": _amount_map(available, allow_zero=True),
        }
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(key)
        if existing is not None:
            if existing[0] != payload_digest(request):
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            raise ReservationConflict(
                "reservation mutation is already committed; replay the financial command"
            )

        snapshot = self._apply(candidate, "RESERVE", request)
        snapshot_value = _snapshot_payload(snapshot)
        next_version = (
            1 if not events else int(events[-1]["aggregate_version"]) + 1
        )
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": "RESERVE",
            "request": request,
            "idempotency_key": key,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": _journal_identity(
                self.environment,
                self.account_id,
                "financial-admission-reservation-event",
                _text(event_key, name="event_key"),
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _text(committed_at, name="committed_at"),
        }
        return PreparedReservationMutation(
            snapshot=snapshot,
            snapshot_payload=snapshot_value,
            envelope=envelope,
            idempotency_key=key,
            request=request,
            aggregate_version=next_version,
        )

    def prepare_consume_mutation(
        self,
        *,
        event_key: str,
        idempotency_key: str,
        reservation_id: str,
        usage: dict[str, object],
        committed_at: str,
        expected_snapshot_digest: str | None = None,
    ) -> PreparedReservationMutation:
        """Prepare one reservation consumption for a shared durable commit.

        No reservation state is mutated here. The returned event is derived
        from one immutable reservation-journal cut and can be committed in the
        same JournalStore transaction as canonical economic events. Exact
        replay after acknowledgement loss reports already_committed only when
        the same idempotency key, request and resulting snapshot are already
        present in durable reservation history.
        """

        key = _text(idempotency_key, name="idempotency_key")
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "usage": _amount_map(usage, allow_zero=False),
        }
        expected_cut = (
            None
            if expected_snapshot_digest is None
            else _text(expected_snapshot_digest, name="expected_snapshot_digest")
        )
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(key)
        if existing is not None:
            if existing[0] != payload_digest(request):
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            matching_indexes = tuple(
                index
                for index, event in enumerate(events)
                if isinstance(event.get("payload"), dict)
                and event["payload"].get("idempotency_key") == key
                and event["payload"].get("operation") == "CONSUME"
            )
            if len(matching_indexes) != 1:
                raise ReservationConflict(
                    "committed reservation consumption identity is ambiguous"
                )
            matching_index = matching_indexes[0]
            matching_event = events[matching_index]
            historical_book, _ = self._replay(events[: matching_index + 1])
            snapshot = historical_book.get(request["reservation_id"])
            snapshot_value = _snapshot_payload(snapshot)
            if (
                snapshot_value != existing[1]
                or snapshot_value != matching_event["payload"].get("snapshot")
            ):
                raise ReservationConflict(
                    "committed reservation consumption snapshot does not match replayed state"
                )
            return PreparedReservationMutation(
                snapshot=snapshot,
                snapshot_payload=snapshot_value,
                envelope=None,
                idempotency_key=key,
                request=request,
                aggregate_version=int(matching_event["aggregate_version"]),
                already_committed=True,
            )

        if expected_cut is not None:
            current_snapshot = candidate.get(request["reservation_id"])
            if reservation_snapshot_digest(current_snapshot) != expected_cut:
                raise ReservationConflict(
                    "reservation snapshot changed after provider fill plan derivation"
                )

        snapshot = self._apply(candidate, "CONSUME", request)
        snapshot_value = _snapshot_payload(snapshot)
        next_version = (
            1 if not events else int(events[-1]["aggregate_version"]) + 1
        )
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": "CONSUME",
            "request": request,
            "idempotency_key": key,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": _journal_identity(
                self.environment,
                self.account_id,
                "financial-fill-reservation-event",
                _text(event_key, name="event_key"),
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _text(committed_at, name="committed_at"),
        }
        return PreparedReservationMutation(
            snapshot=snapshot,
            snapshot_payload=snapshot_value,
            envelope=envelope,
            idempotency_key=key,
            request=request,
            aggregate_version=next_version,
        )

    def prepare_consume_and_mark_filled_mutation(
        self,
        *,
        event_key: str,
        idempotency_key: str,
        reservation_id: str,
        usage: dict[str, object],
        order_fill_event_id: str,
        order_fill_payload_hash: str,
        order_fill_snapshot_digest: str,
        order_fill_mutation_hash: str,
        order_fill_provider_id: str,
        order_fill_client_order_id: str,
        order_fill_fill_id: str,
        order_fill_provider_execution_id: str,
        committed_at: str,
        expected_snapshot_digest: str | None = None,
    ) -> PreparedReservationMutation:
        """Prepare one journal-native consume + FILLED transition.

        FILLED authority is derived from the exact prepared durable OMS
        RECORD_FILL event, its post-fill snapshot digest and mutation hash.  A
        historical command that already committed only CONSUME is replayed as
        that historical cut; this method never rewrites legacy reservation
        history merely because the current OMS projection is terminal.
        """

        key = _text(idempotency_key, name="idempotency_key")
        legacy_request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "usage": _amount_map(usage, allow_zero=False),
        }
        request = {
            **legacy_request,
            "order_fill_event_id": _text(
                order_fill_event_id,
                name="order_fill_event_id",
            ),
            "order_fill_payload_hash": _canonical_sha256(
                order_fill_payload_hash,
                name="order_fill_payload_hash",
            ),
            "order_fill_snapshot_digest": _canonical_sha256(
                order_fill_snapshot_digest,
                name="order_fill_snapshot_digest",
            ),
            "order_fill_mutation_hash": _canonical_sha256(
                order_fill_mutation_hash,
                name="order_fill_mutation_hash",
            ),
            "order_fill_provider_id": _text(
                order_fill_provider_id,
                name="order_fill_provider_id",
            ),
            "order_fill_client_order_id": _text(
                order_fill_client_order_id,
                name="order_fill_client_order_id",
            ),
            "order_fill_fill_id": _text(
                order_fill_fill_id,
                name="order_fill_fill_id",
            ),
            "order_fill_provider_execution_id": _text(
                order_fill_provider_execution_id,
                name="order_fill_provider_execution_id",
            ),
        }
        # Validate the composed evidence string before any candidate mutation.
        _filled_resolution_evidence(request)
        expected_cut = (
            None
            if expected_snapshot_digest is None
            else _text(expected_snapshot_digest, name="expected_snapshot_digest")
        )
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(key)
        if existing is not None:
            matching_indexes = tuple(
                index
                for index, event in enumerate(events)
                if isinstance(event.get("payload"), dict)
                and event["payload"].get("idempotency_key") == key
            )
            if len(matching_indexes) != 1:
                raise ReservationConflict(
                    "committed reservation fill mutation identity is ambiguous"
                )
            matching_index = matching_indexes[0]
            matching_event = events[matching_index]
            stored_payload = matching_event["payload"]
            stored_operation = stored_payload.get("operation")
            if (
                stored_operation == "CONSUME"
                and existing[0] == payload_digest(legacy_request)
            ):
                legacy_plan = self.prepare_consume_mutation(
                    event_key=event_key,
                    idempotency_key=key,
                    reservation_id=legacy_request["reservation_id"],
                    usage=legacy_request["usage"],
                    committed_at=committed_at,
                    expected_snapshot_digest=None,
                )
                if not legacy_plan.already_committed:
                    raise ReservationConflict(
                        "historical reservation CONSUME did not resolve as committed"
                    )
                return legacy_plan
            if (
                stored_operation == "CONSUME_AND_MARK_FILLED"
                and existing[0] == payload_digest(request)
            ):
                replay_request = request
            else:
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )

            # Return the exact historical post-mutation cut. A later bust or
            # refill may legitimately move the current projection forward and
            # must not invalidate idempotent recovery of this command.
            historical_book, _ = self._replay(events[: matching_index + 1])
            snapshot = historical_book.get(legacy_request["reservation_id"])
            snapshot_value = _snapshot_payload(snapshot)
            stored_snapshot = stored_payload.get("snapshot")
            if snapshot_value != existing[1] or snapshot_value != stored_snapshot:
                raise ReservationConflict(
                    "committed reservation fill snapshot does not match replayed state"
                )
            return PreparedReservationMutation(
                snapshot=snapshot,
                snapshot_payload=snapshot_value,
                envelope=None,
                idempotency_key=key,
                request=replay_request,
                aggregate_version=int(matching_event["aggregate_version"]),
                already_committed=True,
            )

        if expected_cut is not None:
            current_snapshot = candidate.get(legacy_request["reservation_id"])
            if reservation_snapshot_digest(current_snapshot) != expected_cut:
                raise ReservationConflict(
                    "reservation snapshot changed after provider fill plan derivation"
                )

        snapshot = self._apply(candidate, "CONSUME_AND_MARK_FILLED", request)
        snapshot_value = _snapshot_payload(snapshot)
        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": "CONSUME_AND_MARK_FILLED",
            "request": request,
            "idempotency_key": key,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": _journal_identity(
                self.environment,
                self.account_id,
                "financial-fill-terminal-reservation-event",
                _text(event_key, name="event_key"),
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _text(committed_at, name="committed_at"),
        }
        return PreparedReservationMutation(
            snapshot=snapshot,
            snapshot_payload=snapshot_value,
            envelope=envelope,
            idempotency_key=key,
            request=request,
            aggregate_version=next_version,
        )

    def prepare_restore_consumption_mutation(
        self,
        *,
        event_key: str,
        idempotency_key: str,
        reservation_id: str,
        usage: dict[str, object],
        committed_at: str,
        expected_snapshot_digest: str | None = None,
    ) -> PreparedReservationMutation:
        """Prepare a conservative fill-bust restoration for a shared commit.

        The mutation moves previously consumed amounts back to the active
        reservation's remaining balance. It therefore restores held capacity;
        it does not release capacity to availability. No state is mutated until
        the caller commits this envelope in the canonical JournalStore batch.
        """

        key = _text(idempotency_key, name="idempotency_key")
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "usage": _amount_map(usage, allow_zero=False),
        }
        expected_cut = (
            None
            if expected_snapshot_digest is None
            else _text(expected_snapshot_digest, name="expected_snapshot_digest")
        )
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(key)
        if existing is not None:
            if existing[0] != payload_digest(request):
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            matching_event_indexes = tuple(
                index
                for index, event in enumerate(events)
                if isinstance(event.get("payload"), Mapping)
                and event["payload"].get("idempotency_key") == key
                and event["payload"].get("operation") == "RESTORE_CONSUMPTION"
            )
            if len(matching_event_indexes) != 1:
                raise ReservationConflict(
                    "committed reservation restoration identity is ambiguous"
                )
            committed_event_index = matching_event_indexes[0]
            committed_event = events[committed_event_index]
            committed_snapshot = committed_event["payload"].get("snapshot")
            if committed_snapshot != existing[1]:
                raise ReservationConflict(
                    "committed reservation restoration snapshot authority changed"
                )
            # Exact retry binds to the historical post-restoration cut, not the
            # reservation's current state. Later fills may legitimately consume
            # the same reservation; replaying this command must neither reject
            # that progress nor apply the restoration a second time.
            historical_book, _ = self._replay(events[: committed_event_index + 1])
            snapshot = historical_book.get(request["reservation_id"])
            snapshot_value = _snapshot_payload(snapshot)
            if snapshot_value != committed_snapshot:
                raise ReservationConflict(
                    "committed reservation restoration historical cut changed"
                )
            return PreparedReservationMutation(
                snapshot=snapshot,
                snapshot_payload=snapshot_value,
                envelope=None,
                idempotency_key=key,
                request=request,
                aggregate_version=int(committed_event["aggregate_version"]),
                already_committed=True,
            )

        if expected_cut is not None:
            current_snapshot = candidate.get(request["reservation_id"])
            if reservation_snapshot_digest(current_snapshot) != expected_cut:
                raise ReservationConflict(
                    "reservation snapshot changed after fill-bust restoration derivation"
                )

        snapshot = self._apply(candidate, "RESTORE_CONSUMPTION", request)
        snapshot_value = _snapshot_payload(snapshot)
        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": "RESTORE_CONSUMPTION",
            "request": request,
            "idempotency_key": key,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": _journal_identity(
                self.environment,
                self.account_id,
                "financial-fill-bust-reservation-event",
                _text(event_key, name="event_key"),
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _text(committed_at, name="committed_at"),
        }
        return PreparedReservationMutation(
            snapshot=snapshot,
            snapshot_payload=snapshot_value,
            envelope=envelope,
            idempotency_key=key,
            request=request,
            aggregate_version=next_version,
        )

    def refresh(self) -> None:
        """Reload the reservation projection after an external atomic commit."""

        self._reload()

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
        event_id = _journal_identity(
            self.environment,
            self.account_id,
            "event",
            canonical_json([cid, str(next_version)]),
        )
        journal_command_id = _journal_identity(
            self.environment,
            self.account_id,
            "command",
            cid,
        )
        journal_idempotency_key = _journal_identity(
            self.environment,
            self.account_id,
            "idempotency",
            idem,
        )
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
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
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _now(),
        }

        # commit_command is the single durable transaction: command dedupe and
        # reservation event either both commit or neither does.
        try:
            _reservation_store_commit_command(
                self,
                command_id=journal_command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=journal_idempotency_key,
                request={
                    "environment": self.environment,
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
        self._reload()
        return self._book.get(reservation_id)

    def total_reserved(self, resource: str) -> Decimal:
        self._reload()
        return self._book.total_reserved(resource)

    def active(self) -> tuple[ReservationSnapshot, ...]:
        self._reload()
        return self._book.active()

    @property
    def version(self) -> int:
        events = self._events()
        return 0 if not events else int(events[-1]["aggregate_version"])

    @property
    def state_digest(self) -> str:
        """Content identity for the exact durable reservation journal cut.

        The digest is derived from the canonical account/environment scope and
        the ordered immutable event identities/hashes. It is evidence of this
        projection cut only; it does not create a second reservation authority.
        """

        events = self._events()
        version = 0 if not events else int(events[-1]["aggregate_version"])
        material = {
            "environment": self.environment,
            "account_id": self.account_id,
            "version": version,
            "events": [
                {
                    "event_id": event["event_id"],
                    "aggregate_version": str(event["aggregate_version"]),
                    "payload_hash": event["payload_hash"],
                }
                for event in events
            ],
        }
        return payload_digest(material).removeprefix("sha256:")

    def reserve(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        intent_id: str,
        requirements: dict[str, object],
        available: dict[str, object],
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
        usage: dict[str, object],
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

    def _verify_zero_wire_blocked(
        self,
        *,
        reservation_id: object,
        intent_id: object,
        provider: object,
        attempt_id: object,
        client_order_id: object,
        resolution_evidence: object | None = None,
    ) -> str:
        """Prove a reservation never crossed the irreversible send boundary."""

        _text(reservation_id, name="reservation_id")
        intent = _text(intent_id, name="intent_id")
        provider_name = _text(provider, name="provider").upper()
        attempt = _text(attempt_id, name="attempt_id")
        client = _text(client_order_id, name="client_order_id")
        aggregate_id = submission_attempt_aggregate_id(
            environment=self.environment,
            account_id=self.account_id,
            attempt_id=attempt,
        )
        attempt_events = self.store.load_events(
            "submission_attempt",
            aggregate_id,
        )
        if (
            len(attempt_events) != 2
            or [event.get("event_type") for event in attempt_events]
            != ["SubmissionPrepared", "SubmissionBlocked"]
            or [event.get("aggregate_version") for event in attempt_events]
            != [1, 2]
        ):
            raise ReservationConflict(
                "zero-wire terminal release requires exact Prepared -> Blocked chronology"
            )
        prepared, blocked = attempt_events
        prepared_payload = prepared.get("payload")
        blocked_payload = blocked.get("payload")
        if type(prepared_payload) is not dict or type(blocked_payload) is not dict:
            raise ReservationConflict(
                "zero-wire submission evidence payload is invalid"
            )
        if (
            prepared_payload.get("attempt_id") != attempt
            or prepared_payload.get("intent_id") != intent
            or prepared_payload.get("environment") != self.environment
            or prepared_payload.get("account_id") != self.account_id
            or _text(
                prepared_payload.get("provider"),
                name="submission provider",
            ).upper()
            != provider_name
            or prepared_payload.get("client_order_id") != client
            or blocked_payload.get("client_order_id") != client
            or not isinstance(blocked_payload.get("reason"), str)
            or not blocked_payload["reason"].strip()
        ):
            raise ReservationConflict(
                "zero-wire submission evidence does not match reservation scope"
            )
        blocked_event_id = _text(
            blocked.get("event_id"),
            name="blocked event_id",
        )
        blocked_payload_hash = _text(
            blocked.get("payload_hash"),
            name="blocked payload_hash",
        )
        evidence = (
            f"journal:submission-blocked:{blocked_event_id}"
            f"@{blocked_payload_hash}"
        )
        if (
            resolution_evidence is not None
            and _text(
                resolution_evidence,
                name="resolution_evidence",
            )
            != evidence
        ):
            raise ReservationConflict(
                "zero-wire resolution evidence does not match durable blocked event"
            )
        return evidence

    def prepare_zero_wire_blocked_terminal_mutation(
        self,
        *,
        event_key: str,
        idempotency_key: str,
        reservation_id: str,
        provider: str,
        attempt_id: str,
        client_order_id: str,
        committed_at: str,
    ) -> PreparedReservationMutation:
        """Prepare a REJECTED release proven by exact pre-send durable chronology."""

        key = _text(idempotency_key, name="idempotency_key")
        rid = _text(reservation_id, name="reservation_id")
        provider_name = _text(provider, name="provider").upper()
        attempt = _text(attempt_id, name="attempt_id")
        client = _text(client_order_id, name="client_order_id")
        events = self._events()
        candidate, idempotency = self._replay(events)
        current = candidate.get(rid)
        evidence = self._verify_zero_wire_blocked(
            reservation_id=rid,
            intent_id=current.intent_id,
            provider=provider_name,
            attempt_id=attempt,
            client_order_id=client,
        )
        request = {
            "reservation_id": rid,
            "outcome": "REJECTED",
            "provider": provider_name,
            "attempt_id": attempt,
            "client_order_id": client,
            "resolution_evidence": evidence,
        }
        existing = idempotency.get(key)
        if existing is not None:
            if existing[0] != payload_digest(request):
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            snapshot = candidate.get(rid)
            snapshot_value = _snapshot_payload(snapshot)
            if snapshot_value != existing[1]:
                raise ReservationConflict(
                    "committed zero-wire terminal snapshot does not match replayed state"
                )
            return PreparedReservationMutation(
                snapshot=snapshot,
                snapshot_payload=snapshot_value,
                envelope=None,
                idempotency_key=key,
                request=request,
                aggregate_version=(
                    0 if not events else int(events[-1]["aggregate_version"])
                ),
                already_committed=True,
            )

        if current.state != "WORKING":
            raise ReservationConflict(
                "zero-wire blocked release requires a WORKING reservation"
            )
        if any(amount != 0 for amount in current.consumed.values()):
            raise ReservationConflict(
                "zero-wire blocked release cannot erase consumed exposure"
            )
        snapshot = candidate.mark_terminal(
            rid,
            outcome="REJECTED",
            resolution_evidence=evidence,
        )
        snapshot_value = _snapshot_payload(snapshot)
        next_version = (
            1 if not events else int(events[-1]["aggregate_version"]) + 1
        )
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": "MARK_ZERO_WIRE_TERMINAL",
            "request": request,
            "idempotency_key": key,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": _journal_identity(
                self.environment,
                self.account_id,
                "zero-wire-terminal-reservation-event",
                _text(event_key, name="event_key"),
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _text(committed_at, name="committed_at"),
        }
        return PreparedReservationMutation(
            snapshot=snapshot,
            snapshot_payload=snapshot_value,
            envelope=envelope,
            idempotency_key=key,
            request=request,
            aggregate_version=next_version,
        )

    def _verify_resolution_evidence(
        self,
        *,
        reservation_id: object,
        intent_id: object,
        outcome: object,
        provider: object,
        attempt_id: object,
        resolution_evidence: object,
    ) -> str:
        rid = _text(reservation_id, name="reservation_id")
        intent = _text(intent_id, name="intent_id")
        terminal_outcome = _text(outcome, name="outcome").upper()
        provider_name = _text(provider, name="provider").upper()
        attempt = _text(attempt_id, name="attempt_id")
        artifact_id, digest, evidence = _immutable_evidence_ref(
            resolution_evidence
        )
        if self._resolution_artifact_reader is None:
            raise ReservationConflict(
                "terminal release requires the trusted resolution artifact store"
            )
        try:
            manifest, raw = self._resolution_artifact_reader(artifact_id)
            manifest_hash = manifest.get("manifest_hash")
            if (
                not isinstance(manifest_hash, str)
                or not manifest_hash.startswith("sha256:")
                or len(manifest_hash) != 71
            ):
                raise ArtifactIntegrityError(
                    "resolution evidence manifest lacks canonical integrity binding"
                )
            if manifest.get("sha256") != f"sha256:{digest}":
                raise ArtifactIntegrityError(
                    "resolution evidence reference digest does not match manifest"
                )
            if manifest.get("media_type") != _RESOLUTION_MEDIA_TYPE:
                raise ArtifactIntegrityError(
                    "resolution evidence has an unsupported media type"
                )
            text = raw.decode("utf-8")
            receipt = strict_json_loads(text)
        except (
            ArtifactIntegrityError,
            FileNotFoundError,
            UnicodeError,
            ValueError,
            TypeError,
        ) as error:
            raise ReservationConflict(
                "resolution evidence verification failed"
            ) from error
        if type(receipt) is not dict:
            raise ReservationConflict(
                "resolution evidence receipt must be a JSON object"
            )

        try:
            reconciliation_event_id = _text(
                receipt.get("reconciliation_event_id"),
                name="reconciliation_event_id",
            )
            reconciliation_payload_hash = _text(
                receipt.get("reconciliation_payload_hash"),
                name="reconciliation_payload_hash",
            )
        except (ValueError, TypeError) as error:
            raise ReservationConflict(
                "resolution evidence receipt does not match reservation scope"
            ) from error
        if (
            not reconciliation_payload_hash.startswith("sha256:")
            or len(reconciliation_payload_hash) != 71
            or any(
                ch not in "0123456789abcdef"
                for ch in reconciliation_payload_hash.removeprefix("sha256:")
            )
        ):
            raise ReservationConflict(
                "resolution evidence receipt does not match reservation scope"
            )

        expected = {
            "schema_version": _RESOLUTION_SCHEMA_VERSION,
            "evidence_type": _RESOLUTION_EVIDENCE_TYPE,
            "environment": self.environment,
            "account_id": self.account_id,
            "reservation_id": rid,
            "intent_id": intent,
            "provider": provider_name,
            "attempt_id": attempt,
            "outcome": terminal_outcome,
            "reconciliation_complete": True,
            "reconciliation_event_id": reconciliation_event_id,
            "reconciliation_payload_hash": reconciliation_payload_hash,
        }
        receipt_canonical = canonical_json(receipt)
        expected_canonical = canonical_json(expected)
        if receipt_canonical != expected_canonical:
            raise ReservationConflict(
                "resolution evidence receipt does not match reservation scope"
            )
        if raw != receipt_canonical.encode("utf-8"):
            raise ReservationConflict(
                "resolution evidence receipt must use canonical JSON bytes"
            )

        aggregate_id = submission_attempt_aggregate_id(
            environment=self.environment,
            account_id=self.account_id,
            attempt_id=attempt,
        )
        attempt_events = _reservation_store_load_events(
            self,
            "submission_attempt",
            aggregate_id,
        )
        if not attempt_events:
            raise ReservationConflict(
                "resolution evidence is not bound to a durable submission attempt"
            )
        prepared = attempt_events[0]
        if prepared.get("event_type") != "SubmissionPrepared":
            raise ReservationConflict(
                "submission attempt does not start with durable preparation"
            )
        prepared_payload = prepared.get("payload")
        if not isinstance(prepared_payload, dict):
            raise ReservationConflict(
                "submission attempt preparation payload is invalid"
            )
        if (
            prepared_payload.get("environment") != self.environment
            or prepared_payload.get("account_id") != self.account_id
            or _text(
                prepared_payload.get("provider"),
                name="submission provider",
            ).upper()
            != provider_name
            or prepared_payload.get("intent_id") != intent
        ):
            raise ReservationConflict(
                "resolution evidence does not match durable submission scope"
            )
        client_order_id = _text(
            prepared_payload.get("client_order_id"),
            name="submission client_order_id",
        )
        if (
            terminal_outcome == "PROVEN_ABSENT"
            and not any(
                event.get("event_type") == "SubmissionUnknown"
                for event in attempt_events
            )
        ):
            raise ReservationConflict(
                "PROVEN_ABSENT requires a durable UNKNOWN submission state"
            )

        reconciliation_event = _reservation_store_get_event(
            self,
            reconciliation_event_id,
        )
        if (
            reconciliation_event is None
            or reconciliation_event.get("event_type") != "AccountReconciled"
            or reconciliation_event.get("aggregate_type") != "account_reconciliation"
        ):
            raise ReservationConflict(
                "terminal release requires a matching durable reconciliation checkpoint"
            )
        reconciliation_payload = reconciliation_event.get("payload")
        if not isinstance(reconciliation_payload, dict):
            raise ReservationConflict(
                "durable reconciliation checkpoint payload is invalid"
            )
        if (
            reconciliation_event.get("payload_hash")
            != reconciliation_payload_hash
            or payload_digest(reconciliation_payload)
            != reconciliation_payload_hash
        ):
            raise ReservationConflict(
                "durable reconciliation checkpoint hash does not match receipt"
            )
        if (
            reconciliation_payload.get("provider_id") != provider_name
            or reconciliation_payload.get("account_id") != self.account_id
            or reconciliation_payload.get("environment") != self.environment
        ):
            raise ReservationConflict(
                "durable reconciliation checkpoint scope does not match reservation"
            )
        if (
            reconciliation_payload.get("complete") is not True
            or reconciliation_payload.get("snapshot_consistent") is not True
            or reconciliation_payload.get("blocking_resources") != []
        ):
            raise ReservationConflict(
                "terminal release requires complete non-blocking reconciliation"
            )
        resolutions = reconciliation_payload.get("submission_resolutions")
        if not isinstance(resolutions, list):
            raise ReservationConflict(
                "durable reconciliation checkpoint lacks submission resolutions"
            )
        matching = [
            item
            for item in resolutions
            if isinstance(item, dict)
            and item.get("attempt_id") == attempt
            and item.get("client_order_id") == client_order_id
        ]
        if len(matching) != 1:
            raise ReservationConflict(
                "durable reconciliation checkpoint does not uniquely resolve submission"
            )
        canonical_outcome = _text(
            matching[0].get("outcome"),
            name="reconciliation submission outcome",
        ).upper()
        # Reconciliation can prove that at least one execution exists, but an
        # execution observation alone does not prove that the order is fully
        # filled.  Keep worst-case reservation capacity held until a canonical
        # terminal order/fill projection can prove FILLED semantics.
        required_outcome = {
            "PROVEN_ABSENT": "PROVEN_ABSENT",
        }.get(terminal_outcome)
        if required_outcome is None:
            raise ReservationConflict(
                "terminal outcome lacks canonical reconciliation semantics"
            )
        if canonical_outcome != required_outcome:
            raise ReservationConflict(
                "terminal outcome does not match durable reconciliation resolution"
            )
        return evidence

    def mark_terminal(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        outcome: str,
        provider: str,
        attempt_id: str,
        resolution_evidence: str,
    ) -> ReservationSnapshot:
        rid = _text(reservation_id, name="reservation_id")
        current = self.get(rid)
        terminal_outcome = _text(outcome, name="outcome").upper()
        provider_name = _text(provider, name="provider").upper()
        attempt = _text(attempt_id, name="attempt_id")
        evidence = self._verify_resolution_evidence(
            reservation_id=rid,
            intent_id=current.intent_id,
            outcome=terminal_outcome,
            provider=provider_name,
            attempt_id=attempt,
            resolution_evidence=resolution_evidence,
        )
        request = {
            "reservation_id": rid,
            "outcome": terminal_outcome,
            "provider": provider_name,
            "attempt_id": attempt,
            "resolution_evidence": evidence,
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="MARK_TERMINAL",
            request=request,
        )
