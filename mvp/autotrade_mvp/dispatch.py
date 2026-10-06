"""Durable guarded submission attempts for the AutoTrade MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import builtins as _builtins
import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5, uuid4
from weakref import ref as weakref_ref

from autotrade_numeric.exact_decimal import (
    ExactDecimalError,
    parse_bounded_json_integer_token,
    parse_bounded_json_number_token,
)

from .persistence import JournalStore, canonical_json, payload_digest
from .provider_response_limits import (
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_json_depth,
    require_provider_response_bytes,
)


AuthorityCheck = Callable[[str, str], tuple[bool, str]]
SenderCheck = Callable[[str, int], None]
TransportSend = Callable[[str, Mapping[str, Any], Callable[[], None]], Any]

_SUBMISSION_RESPONSE_BINDING_TOKEN = object()
_EXACT_RESPONSE_MARKERS = frozenset(
    {"response_encoding", "response_text", "response_sha256"}
)


def _has_exact_response_markers(payload: Mapping[str, Any]) -> bool:
    return any(marker in payload for marker in _EXACT_RESPONSE_MARKERS)


def _exact_response_terminal_semantics_are_canonical(
    event_type: str,
    payload: Mapping[str, Any],
) -> bool:
    if not _has_exact_response_markers(payload):
        return True
    if event_type == "SubmissionSent":
        return (
            "reason" not in payload
            and "retry_disposition" not in payload
        )
    if event_type == "SubmissionUnknown":
        reason = payload.get("reason")
        return (
            type(reason) is str
            and bool(reason.strip())
            and payload.get("retry_disposition") == "RECONCILE_FIRST"
        )
    return False


def _decode_exact_json_bytes(raw: bytes) -> Any:
    if type(raw) is not bytes or not raw:
        raise ValueError("provider response bytes must be non-empty bytes")

    # Consume the one shared #652 transport/consumer structural envelope
    # BEFORE stdlib JSON can materialize an unbounded nested object graph.
    # Translate only after leaving the helper error handler, preserving
    # the existing redacted public exception boundary.
    structural_failure = False
    try:
        require_provider_json_depth(raw)
    except ValueError:
        structural_failure = True
    if structural_failure:
        raise ValueError("provider response exceeds shared JSON resource budget")

    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(
                    "provider response contains duplicate JSON keys"
                )
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = None
    if text is None:
        # Raise outside the codec exception handler: suppressing display
        # chaining alone still leaves raw bytes reachable via __context__.
        raise ValueError("provider response must be exact UTF-8 JSON bytes")

    json_failure = False
    parser_recursion_failure = False
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=no_duplicate_keys,
            parse_float=parse_bounded_json_number_token,
            parse_int=parse_bounded_json_integer_token,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError(
                    "provider response contains non-finite JSON constant"
                )
            ),
        )
    except RecursionError:
        # A defensive fallback: the shared scanner normally fails at depth
        # 65 before stdlib parsing; future decoder changes must also remain
        # deterministic and must not retain an untrusted parser exception.
        parser_recursion_failure = True
    except json.JSONDecodeError:
        # JSONDecodeError retains the complete provider document. Translate
        # only after leaving this handler so it cannot remain in __context__.
        json_failure = True
    except ExactDecimalError as error:
        # The shared bounded numeric exception contains only fixed resource
        # diagnostics and no provider token/document material.
        raise ValueError(
            "provider response contains invalid or oversized exact JSON number"
        ) from error
    except ValueError:
        # Duplicate-key and parse_constant rejections are fixed diagnostics.
        raise

    if json_failure:
        raise ValueError("provider response must be exact UTF-8 JSON bytes")
    if parser_recursion_failure:
        raise ValueError("provider response exceeds shared JSON resource budget")
    return decoded


@dataclass(frozen=True)
class ExactJsonTransportResponse:
    """Exact provider wire bytes returned after the guarded send barrier."""

    response_bytes: bytes
    http_status: int | None = None
    requires_reconciliation: bool = False
    ambiguity_reason: str | None = None

    def __post_init__(self) -> None:
        raw = self.response_bytes
        if type(self.requires_reconciliation) is not bool:
            raise TypeError("requires_reconciliation must be boolean")
        try:
            require_provider_response_bytes(
                raw,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                allow_empty=self.requires_reconciliation,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "provider response bytes violate shared byte budget"
            ) from error
        if self.http_status is not None and (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ValueError("http_status must be an integer 100..599 when provided")
        if self.requires_reconciliation:
            if (
                type(self.ambiguity_reason) is not str
                or not self.ambiguity_reason.strip()
            ):
                raise ValueError(
                    "ambiguous exact response requires a non-empty ambiguity_reason"
                )
            object.__setattr__(
                self,
                "ambiguity_reason",
                self.ambiguity_reason.strip(),
            )
        else:
            # Definitive responses remain strict JSON. Opaque wire bytes are
            # admissible only after the provider classifier has already made
            # the irreversible post-SEND result reconciliation-required.
            _decode_exact_json_bytes(raw)
            if self.ambiguity_reason is not None:
                raise ValueError(
                    "ambiguity_reason is only valid when reconciliation is required"
                )
        _register_exact_transport_response(self)

    @property
    def response_text(self) -> str:
        snapshot = _require_canonical_exact_transport_response(self)
        return snapshot[0].decode("utf-8")

    @property
    def response_sha256(self) -> str:
        snapshot = _require_canonical_exact_transport_response(self)
        return "sha256:" + sha256(snapshot[0]).hexdigest()

    @property
    def payload(self) -> Any:
        snapshot = _require_canonical_exact_transport_response(self)
        return _decode_exact_json_bytes(snapshot[0])


def _install_exact_transport_response_authority():
    """Seal provider post-SEND classification to its validated construction state."""

    response_type = ExactJsonTransportResponse
    canonical_type = type
    canonical_id = id
    canonical_tuple = tuple
    canonical_frozenset = frozenset
    canonical_len = len
    canonical_dict = dict
    canonical_bool = bool
    canonical_int = int
    canonical_str = str
    canonical_bytes = bytes
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    canonical_weakref_ref = weakref_ref
    canonical_require_response_bytes = require_provider_response_bytes
    require_response_bytes_code = canonical_require_response_bytes.__code__
    canonical_decode = _decode_exact_json_bytes
    decode_code = canonical_decode.__code__
    canonical_sha256 = sha256
    canonical_hard_response_bytes = HARD_MAX_PROVIDER_RESPONSE_BYTES
    states: dict[int, tuple[object, tuple[object, ...]]] = {}
    installed: list[object] = []

    def authority_changed():
        raise ValueError("exact transport response authority is unavailable")

    def implementation_changed():
        if (
            ExactJsonTransportResponse is not response_type
            or type is not canonical_type
            or id is not canonical_id
            or tuple is not canonical_tuple
            or frozenset is not canonical_frozenset
            or len is not canonical_len
            or dict is not canonical_dict
            or bool is not canonical_bool
            or int is not canonical_int
            or str is not canonical_str
            or bytes is not canonical_bytes
            or object is not canonical_object
            or weakref_ref is not canonical_weakref_ref
            or require_provider_response_bytes is not canonical_require_response_bytes
            or canonical_require_response_bytes.__code__ is not require_response_bytes_code
            or _decode_exact_json_bytes is not canonical_decode
            or canonical_decode.__code__ is not decode_code
            or sha256 is not canonical_sha256
            or type(HARD_MAX_PROVIDER_RESPONSE_BYTES) is not canonical_int
            or HARD_MAX_PROVIDER_RESPONSE_BYTES != canonical_hard_response_bytes
            or canonical_len(installed) != 2
            or _register_exact_transport_response is not installed[0]
            or _require_canonical_exact_transport_response is not installed[1]
        ):
            authority_changed()

    field_names = (
        "response_bytes",
        "http_status",
        "requires_reconciliation",
        "ambiguity_reason",
    )
    exact_field_names = canonical_frozenset(field_names)

    def raw_snapshot(value):
        state = object_getattribute(value, "__dict__")
        if canonical_type(state) is not canonical_dict:
            authority_changed()
        if canonical_frozenset(state) != exact_field_names:
            authority_changed()
        return canonical_tuple(state[name] for name in field_names)

    def prune():
        for object_id, (value_ref, _snapshot) in canonical_tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(value):
        implementation_changed()
        if canonical_type(value) is not response_type:
            authority_changed()
        current = raw_snapshot(value)
        if canonical_type(current[0]) is not canonical_bytes:
            authority_changed()
        if current[1] is not None and canonical_type(current[1]) is not canonical_int:
            authority_changed()
        if canonical_type(current[2]) is not canonical_bool:
            authority_changed()
        if current[3] is not None and canonical_type(current[3]) is not canonical_str:
            authority_changed()
        prune()
        object_id = canonical_id(value)
        previous = states.get(object_id)
        if previous is not None and previous[0]() is not None:
            authority_changed()
        states[object_id] = (canonical_weakref_ref(value), current)
        return value

    def require(value):
        implementation_changed()
        if canonical_type(value) is not response_type:
            authority_changed()
        prune()
        state = states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            authority_changed()
        expected = state[1]
        current = raw_snapshot(value)
        if (
            canonical_type(current[0]) is not canonical_bytes
            or current[0] is not expected[0]
            or current[1] != expected[1]
            or canonical_type(current[2]) is not canonical_bool
            or current[2] is not expected[2]
            or current[3] != expected[3]
        ):
            authority_changed()
        if current[1] is not None and canonical_type(current[1]) is not canonical_int:
            authority_changed()
        if current[3] is not None and canonical_type(current[3]) is not canonical_str:
            authority_changed()
        canonical_require_response_bytes(
            current[0],
            max_bytes=canonical_hard_response_bytes,
            allow_empty=current[2],
        )
        return expected

    installed.extend((register, require))
    return register, require


(
    _register_exact_transport_response,
    _require_canonical_exact_transport_response,
) = _install_exact_transport_response_authority()
del _install_exact_transport_response_authority


def _snapshot_exact_transport_response(
    response: ExactJsonTransportResponse,
    _canonical_authority=_require_canonical_exact_transport_response,
    _canonical_decoder=_decode_exact_json_bytes,
    _canonical_sha256=sha256,
) -> tuple[str, str, str, Any, int | None, bool, str | None]:
    """Revalidate issuer authority at the post-SEND consumption boundary."""

    if (
        _require_canonical_exact_transport_response is not _canonical_authority
        or _decode_exact_json_bytes is not _canonical_decoder
        or sha256 is not _canonical_sha256
    ):
        raise ValueError("exact transport response authority is unavailable")
    (
        raw,
        http_status,
        requires_reconciliation,
        ambiguity_reason,
    ) = _canonical_authority(response)

    if requires_reconciliation:
        try:
            _canonical_decoder(raw)
        except (TypeError, ValueError):
            response_text = raw.hex()
            response_encoding = "hex"
        else:
            response_text = raw.decode("utf-8")
            response_encoding = "utf-8-json"
        outcome_response = None
    else:
        outcome_response = _canonical_decoder(raw)
        response_text = raw.decode("utf-8")
        response_encoding = "utf-8-json"

    return (
        response_text,
        response_encoding,
        "sha256:" + _canonical_sha256(raw).hexdigest(),
        outcome_response,
        http_status,
        requires_reconciliation,
        ambiguity_reason,
    )


@dataclass(frozen=True)
class SubmissionResponseBinding:
    """Journal-derived immutable binding between one send and exact response bytes."""

    attempt_id: str
    aggregate_id: str
    provider: str
    request_hash: str
    client_order_id: str
    environment: str
    account_id: str
    prepared_at: str
    sent_at: str
    submission_scope: Mapping[str, Any]
    submission_scope_hash: str
    response_bytes: bytes
    response_sha256: str
    response_encoding: str = "utf-8-json"
    terminal_state: str = "SENT"
    ambiguity_reason: str | None = None
    retry_disposition: str | None = None
    http_status: int | None = None
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _SUBMISSION_RESPONSE_BINDING_TOKEN:
            raise ValueError(
                "submission response bindings must be loaded from the durable journal"
            )
        for value, name in (
            (self.attempt_id, "attempt_id"),
            (self.aggregate_id, "aggregate_id"),
            (self.provider, "provider"),
            (self.client_order_id, "client_order_id"),
            (self.account_id, "account_id"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} is required")
        if (
            type(self.request_hash) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_hash) is None
        ):
            raise ValueError("request_hash must be a canonical SHA-256 digest")
        if (
            type(self.submission_scope_hash) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.submission_scope_hash) is None
        ):
            raise ValueError(
                "submission_scope_hash must be a canonical SHA-256 digest"
            )
        if (
            type(self.response_sha256) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.response_sha256) is None
        ):
            raise ValueError("response_sha256 must be a canonical SHA-256 digest")
        if (
            type(self.response_encoding) is not str
            or self.response_encoding not in {"utf-8-json", "hex"}
        ):
            raise ValueError("durable provider response encoding is invalid")
        if (
            type(self.terminal_state) is not str
            or self.terminal_state not in {"SENT", "UNKNOWN"}
        ):
            raise ValueError("terminal_state must be SENT or UNKNOWN")
        if self.terminal_state == "UNKNOWN":
            if (
                type(self.ambiguity_reason) is not str
                or not self.ambiguity_reason.strip()
                or self.ambiguity_reason != self.ambiguity_reason.strip()
            ):
                raise ValueError(
                    "UNKNOWN durable response requires a canonical ambiguity_reason"
                )
            if self.retry_disposition != "RECONCILE_FIRST":
                raise ValueError(
                    "UNKNOWN durable response must remain RECONCILE_FIRST"
                )
        elif self.ambiguity_reason is not None or self.retry_disposition is not None:
            raise ValueError(
                "SENT durable response cannot carry UNKNOWN retry semantics"
            )
        if self.response_encoding == "hex" and self.terminal_state != "UNKNOWN":
            raise ValueError("opaque provider response must remain UNKNOWN")
        try:
            require_provider_response_bytes(
                self.response_bytes,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                allow_empty=(
                    self.response_encoding == "hex"
                    and self.terminal_state == "UNKNOWN"
                ),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "durable provider response bytes violate shared byte budget"
            ) from error
        if (
            "sha256:" + sha256(self.response_bytes).hexdigest()
            != self.response_sha256
        ):
            raise ValueError("durable provider response digest mismatch")
        if self.response_encoding == "utf-8-json":
            _decode_exact_json_bytes(self.response_bytes)
        if self.http_status is not None and (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ValueError("durable provider HTTP status must be an integer 100..599")
        if type(self.environment) is not str:
            raise ValueError("invalid durable submission environment")
        environment = self.environment.upper()
        if environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("invalid durable submission environment")
        object.__setattr__(self, "environment", environment)
        expected_aggregate_id = submission_attempt_aggregate_id(
            environment=environment,
            account_id=self.account_id,
            attempt_id=self.attempt_id,
        )
        if self.aggregate_id != expected_aggregate_id:
            raise ValueError(
                "aggregate_id mismatches durable submission identity"
            )
        if type(self.submission_scope) is not dict:
            raise TypeError("submission_scope must be an exact dict")
        canonical_scope = json.loads(canonical_json(dict.copy(self.submission_scope)))
        expected_scope_hash = (
            "sha256:"
            + sha256(canonical_json(canonical_scope).encode("utf-8")).hexdigest()
        )
        if expected_scope_hash != self.submission_scope_hash:
            raise ValueError("durable submission scope digest mismatch")
        object.__setattr__(
            self,
            "submission_scope",
            _freeze_json(canonical_scope),
        )
        durable_instants: dict[str, datetime] = {}
        for value, name in (
            (self.prepared_at, "prepared_at"),
            (self.sent_at, "sent_at"),
        ):
            point = _instant(value)
            canonical = point.isoformat().replace("+00:00", "Z")
            if canonical != value:
                raise ValueError(f"{name} must be canonical UTC text")
            durable_instants[name] = point
        if durable_instants["sent_at"] < durable_instants["prepared_at"]:
            raise ValueError("sent_at must not precede prepared_at")

    @property
    def payload(self) -> Any:
        if self.response_encoding != "utf-8-json":
            raise ValueError("opaque provider response has no JSON payload")
        return _freeze_json(_decode_exact_json_bytes(self.response_bytes))


def _detach_submission_json(
    value: Any,
    *,
    _active_containers: set[int] | None = None,
) -> Any:
    """Detach caller JSON into an exact-builtin, callback-free value graph."""

    if value is None or type(value) in (str, int, float, bool):
        return value
    if type(value) not in (list, tuple, dict):
        raise TypeError(
            "submission JSON values must use exact built-in JSON containers and scalars"
        )

    active = set() if _active_containers is None else _active_containers
    identity = id(value)
    if identity in active:
        raise ValueError("submission JSON value contains a circular reference")
    active.add(identity)
    try:
        if type(value) in (list, tuple):
            return [
                _detach_submission_json(item, _active_containers=active)
                for item in value
            ]

        detached: dict[str, Any] = {}
        for key, item in dict.items(value):
            if type(key) is not str:
                raise TypeError("submission JSON object keys must be exact strings")
            detached[key] = _detach_submission_json(
                item,
                _active_containers=active,
            )
        return detached
    finally:
        active.remove(identity)


def _freeze_json(value: Any) -> Any:
    """Recursively freeze a canonical JSON value before it reaches transport."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


class DispatchBlocked(RuntimeError):
    """Raised inside a provider wrapper when the final send barrier rejects."""


class _DispatchAuthorityChanged(PermissionError):
    """The invocation-selected dispatcher authority was retargeted."""


def _validated_authority_result(result: Any) -> tuple[bool, str]:
    """Fail closed unless authority returns the exact typed decision contract."""
    if type(result) is not tuple or len(result) != 2:
        return False, "authority_check_invalid_result"
    allowed, reason = result
    if type(allowed) is not bool:
        return False, "authority_check_invalid_allowed"
    if type(reason) is not str or not reason.strip():
        return False, "authority_check_invalid_reason"
    return allowed, reason.strip()


@dataclass(frozen=True)
class DispatchOutcome:
    status: str
    client_order_id: str
    response: Any | None
    reason: str


def _identity_digest(*parts: str) -> str:
    """Hash a canonical tuple without delimiter-boundary ambiguity."""
    return sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()


def submission_attempt_aggregate_id(
    *,
    environment: str,
    account_id: str,
    attempt_id: str,
) -> str:
    """Return the canonical durable aggregate identity for one send attempt."""

    normalized_environment = (
        environment.strip().upper() if type(environment) is str else ""
    )
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    if type(account_id) is not str or not account_id.strip():
        raise ValueError("account_id is required")
    if type(attempt_id) is not str or not attempt_id.strip():
        raise ValueError("attempt_id is required")
    return "submission-attempt:" + _identity_digest(
        normalized_environment,
        account_id.strip(),
        attempt_id.strip(),
    )


def _install_journal_store_authority():
    """Freeze submission journal dispatch to canonical class operations."""

    store_type = JournalStore
    canonical_getattr = getattr
    canonical_vars = vars
    identity_descriptor = store_type.__dict__.get("store_identity")
    operations = {
        "append_event": store_type.append_event,
        "load_events": store_type.load_events,
    }
    class_mro = store_type.__mro__
    class_surfaces = tuple(
        (base, tuple(base.__dict__.items()))
        for base in class_mro
        if base is not object
    )
    if identity_descriptor is None:
        raise RuntimeError("submission journal identity authority is unavailable")

    def snapshot(store: JournalStore) -> tuple[object, object]:
        if JournalStore is not store_type:
            raise RuntimeError("submission journal class authority changed")
        if store_type.__dict__.get("store_identity") is not identity_descriptor:
            raise RuntimeError("submission journal identity authority changed")
        for operation_name, operation in operations.items():
            if canonical_getattr(store_type, operation_name, None) is not operation:
                raise RuntimeError(
                    f"submission journal operation changed: {operation_name}"
                )
        if store_type.__mro__ != class_mro:
            raise RuntimeError("submission journal class authority changed")
        for base, members in class_surfaces:
            current = base.__dict__
            if len(current) != len(members):
                raise RuntimeError("submission journal class authority changed")
            for member_name, member in members:
                if (
                    member_name not in current
                    or current[member_name] is not member
                ):
                    raise RuntimeError("submission journal class authority changed")
        if type(store) is not store_type:
            raise TypeError("store must be the canonical JournalStore")
        state = canonical_vars(store)
        class_owned_names = {
            name
            for base in store_type.__mro__
            for name in base.__dict__
        }
        if class_owned_names.intersection(state):
            raise TypeError("canonical JournalStore instance state is shadowed")
        if "path" not in state or "_store_identity" not in state:
            raise TypeError("canonical JournalStore backing state is unavailable")
        path = state["path"]
        identity = identity_descriptor.__get__(store, store_type)
        if canonical_getattr(identity, "canonical_path", None) != str(path):
            raise PermissionError("canonical JournalStore backing identity changed")
        return path, identity

    def call(
        store: JournalStore,
        operation_name: str,
        /,
        *args,
        **kwargs,
    ):
        snapshot(store)
        if type(operation_name) is not str or operation_name not in operations:
            raise TypeError("unsupported submission journal operation")
        result = operations[operation_name](store, *args, **kwargs)
        # Revalidate after the call as well. The captured append/load function
        # still performs dynamic class dispatch internally (_connect,
        # _decode_event_row, SCHEMA_VERSION, etc.); a persistent class mutation
        # during the operation must never be accepted as submission authority.
        snapshot(store)
        return result

    return snapshot, call


_canonical_journal_authority_snapshot, _journal_store_call = (
    _install_journal_store_authority()
)
del _install_journal_store_authority


def load_submission_response_binding(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    attempt_id: str,
) -> SubmissionResponseBinding:
    """Load exact provider response provenance from the canonical submission journal."""

    # This factory mints the private durable-response binding token. Reject
    # subclasses plus any exact-instance shadow state before the authority read.
    # Full product-selected store capability/recovery composition remains owned
    # by the canonical WP-48/WP-49 lineage.
    _canonical_journal_authority_snapshot(store)
    aggregate_id = submission_attempt_aggregate_id(
        environment=environment,
        account_id=account_id,
        attempt_id=attempt_id,
    )
    # Resolve the method from the canonical class after rejecting all instance
    # shadow state; never dispatch through a caller-attached load_events.
    events = _journal_store_call(store, "load_events", "submission_attempt", aggregate_id)
    if not events:
        raise ValueError("durable submission attempt was not found")
    event_types = [event.get("event_type") for event in events]
    if (
        len(event_types) != 3
        or event_types[:2] != ["SubmissionPrepared", "SubmissionSending"]
        or event_types[2] not in {"SubmissionSent", "SubmissionUnknown"}
    ):
        raise ValueError(
            "durable exact response requires Prepared -> Sending -> Sent/Unknown"
        )
    aggregate_versions = [event.get("aggregate_version") for event in events]
    if (
        any(type(version) is not int for version in aggregate_versions)
        or aggregate_versions != [1, 2, 3]
    ):
        raise ValueError(
            "durable exact response requires aggregate versions 1 -> 2 -> 3"
        )

    prepared, sending, sent = events
    prepared_instant = _canonical_submission_event_instant(
        prepared,
        event_name="SubmissionPrepared",
    )
    sending_instant = _canonical_submission_event_instant(
        sending,
        event_name="SubmissionSending",
    )
    terminal_instant = _canonical_submission_event_instant(
        sent,
        event_name="terminal",
    )
    if not (prepared_instant <= sending_instant <= terminal_instant):
        raise ValueError("durable submission chronology is not monotonic")

    payload = prepared.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("durable SubmissionPrepared payload is invalid")
    sent_payload = sent.get("payload")
    if not isinstance(sent_payload, dict):
        raise ValueError("durable terminal submission payload is invalid")
    terminal_state = (
        "UNKNOWN"
        if sent["event_type"] == "SubmissionUnknown"
        else "SENT"
    )
    ambiguity_reason = sent_payload.get("reason")
    retry_disposition = sent_payload.get("retry_disposition")
    if terminal_state == "UNKNOWN":
        if (
            type(ambiguity_reason) is not str
            or not ambiguity_reason.strip()
            or ambiguity_reason != ambiguity_reason.strip()
            or retry_disposition != "RECONCILE_FIRST"
        ):
            raise ValueError(
                "response-bearing SubmissionUnknown must remain RECONCILE_FIRST"
            )
    elif ambiguity_reason is not None or retry_disposition is not None:
        raise ValueError("SubmissionSent cannot carry UNKNOWN retry semantics")

    response_text = sent_payload.get("response_text")
    response_sha256 = sent_payload.get("response_sha256")
    response_encoding = sent_payload.get("response_encoding")
    if type(response_text) is not str or type(response_sha256) is not str:
        raise ValueError(
            "durable exact provider response bytes are unavailable"
        )
    if response_encoding == "utf-8-json":
        if not response_text:
            raise ValueError(
                "durable exact provider response bytes are unavailable"
            )
        response_bytes = response_text.encode("utf-8")
        _decode_exact_json_bytes(response_bytes)
    elif response_encoding == "hex" and terminal_state == "UNKNOWN":
        if (
            len(response_text) > HARD_MAX_PROVIDER_RESPONSE_BYTES * 2
            or len(response_text) % 2
        ):
            raise ValueError(
                "durable exact provider response bytes are unavailable"
            )
        try:
            response_bytes = bytes.fromhex(response_text)
        except ValueError as error:
            raise ValueError(
                "durable exact provider response bytes are unavailable"
            ) from error
        if response_text != response_bytes.hex():
            raise ValueError(
                "durable exact provider response bytes are unavailable"
            )
        require_provider_response_bytes(
            response_bytes,
            max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            allow_empty=True,
        )
    else:
        raise ValueError(
            "durable exact provider response bytes are unavailable"
        )
    if "sha256:" + sha256(response_bytes).hexdigest() != response_sha256:
        raise ValueError("durable provider response digest mismatch")
    http_status = sent_payload.get("http_status")
    if http_status is not None and (
        type(http_status) is not int
        or http_status < 100
        or http_status > 599
    ):
        raise ValueError("durable provider HTTP status is invalid")
    scope = payload.get("submission_scope")
    scope_hash = payload.get("submission_scope_hash")
    if not isinstance(scope, dict) or not isinstance(scope_hash, str):
        raise ValueError("durable submission scope is unavailable")
    prepared_at = payload.get("prepared_at")
    sent_at = sent.get("observed_at")
    if not isinstance(prepared_at, str) or not isinstance(sent_at, str):
        raise ValueError("durable submission timestamps are unavailable")
    if prepared_at != prepared.get("committed_at"):
        raise ValueError(
            "durable SubmissionPrepared prepared_at mismatches event chronology"
        )
    durable_text: dict[str, str] = {}
    for field_name in (
        "attempt_id",
        "provider",
        "request_hash",
        "client_order_id",
        "environment",
        "account_id",
    ):
        field_value = payload.get(field_name)
        if type(field_value) is not str or not field_value:
            raise ValueError(
                f"durable SubmissionPrepared {field_name} must be exact non-empty text"
            )
        durable_text[field_name] = field_value

    expected_environment = environment.strip().upper()
    expected_account_id = account_id.strip()
    if durable_text["attempt_id"] != attempt_id:
        raise ValueError(
            "durable SubmissionPrepared attempt_id mismatches selected submission identity"
        )
    if durable_text["environment"] != expected_environment:
        raise ValueError(
            "durable SubmissionPrepared environment mismatches selected submission identity"
        )
    if durable_text["account_id"] != expected_account_id:
        raise ValueError(
            "durable SubmissionPrepared account_id mismatches selected submission identity"
        )

    expected_scope_key = _identity_digest(
        expected_environment,
        expected_account_id,
    )
    for event_name, event in (
        ("SubmissionPrepared", prepared),
        ("SubmissionSending", sending),
        ("terminal", sent),
    ):
        if event.get("event_id") != _event_id(
            expected_scope_key,
            durable_text["attempt_id"],
            event["event_type"],
            event["aggregate_version"],
        ):
            raise ValueError(
                f"durable {event_name} event_id mismatches canonical submission event identity"
            )
        if event.get("aggregate_type") != "submission_attempt":
            raise ValueError(
                f"durable {event_name} aggregate_type mismatches submission authority"
            )
        if event.get("aggregate_id") != aggregate_id:
            raise ValueError(
                f"durable {event_name} aggregate_id mismatches selected submission identity"
            )
        event_environment = event.get("environment")
        if (
            type(event_environment) is not str
            or event_environment != expected_environment
        ):
            raise ValueError(
                f"durable {event_name} environment mismatches selected submission identity"
            )

    for event_name, event in (("SubmissionSending", sending), ("terminal", sent)):
        event_payload = event.get("payload")
        if type(event_payload) is not dict:
            raise ValueError(f"durable {event_name} payload is invalid")
        event_client_order_id = event_payload.get("client_order_id")
        if (
            type(event_client_order_id) is not str
            or event_client_order_id != durable_text["client_order_id"]
        ):
            raise ValueError(
                f"durable {event_name} client_order_id mismatches SubmissionPrepared"
            )

    prepared_owner_token = payload.get("owner_token")
    prepared_owner_epoch = payload.get("owner_epoch")
    sending_payload = sending.get("payload")
    if (
        type(prepared_owner_token) is not str
        or not prepared_owner_token
        or type(prepared_owner_epoch) is not int
        or prepared_owner_epoch < 1
        or prepared.get("owner_epoch") != str(prepared_owner_epoch)
        or type(sending_payload) is not dict
        or sending_payload.get("owner_token") != prepared_owner_token
        or sending_payload.get("owner_epoch") != prepared_owner_epoch
        or sending.get("owner_epoch") != str(prepared_owner_epoch)
        or sent.get("owner_epoch") != str(prepared_owner_epoch)
    ):
        raise ValueError("durable exact response sender ownership is not continuous")

    return SubmissionResponseBinding(
        attempt_id=durable_text["attempt_id"],
        aggregate_id=aggregate_id,
        provider=durable_text["provider"],
        request_hash=durable_text["request_hash"],
        client_order_id=durable_text["client_order_id"],
        environment=durable_text["environment"],
        account_id=durable_text["account_id"],
        prepared_at=prepared_at,
        sent_at=sent_at,
        submission_scope=scope,
        submission_scope_hash=scope_hash,
        response_bytes=response_bytes,
        response_sha256=response_sha256,
        response_encoding=response_encoding,
        terminal_state=terminal_state,
        ambiguity_reason=ambiguity_reason,
        retry_disposition=retry_disposition,
        http_status=http_status,
        _factory_token=_SUBMISSION_RESPONSE_BINDING_TOKEN,
    )


def _install_submission_response_binding_authority(loader):
    """Seal durable response bindings to the exact loader and resource authorities."""

    binding_type = SubmissionResponseBinding
    binding_token = _SUBMISSION_RESPONSE_BINDING_TOKEN
    error_type = ValueError
    canonical_type = type
    canonical_id = id
    canonical_tuple = tuple
    canonical_frozenset = frozenset
    canonical_dict = dict
    canonical_range = range
    canonical_enumerate = enumerate
    canonical_str = str
    canonical_int = int
    canonical_bytes = bytes
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    canonical_getattr = getattr
    mapping_proxy_type = MappingProxyType
    canonical_weakref_ref = weakref_ref
    canonical_loader = loader
    loader_code = loader.__code__
    canonical_journal_snapshot = _canonical_journal_authority_snapshot
    journal_snapshot_code = canonical_journal_snapshot.__code__
    canonical_journal_call = _journal_store_call
    journal_call_code = canonical_journal_call.__code__
    canonical_binding_init = SubmissionResponseBinding.__init__
    canonical_binding_post_init = SubmissionResponseBinding.__post_init__
    binding_init_code = canonical_binding_init.__code__
    binding_post_init_code = canonical_binding_post_init.__code__
    canonical_json_function = canonical_json
    canonical_json_module = json
    canonical_re_module = re
    canonical_sha256 = sha256
    canonical_attempt_id = submission_attempt_aggregate_id
    attempt_id_code = canonical_attempt_id.__code__
    canonical_decode = _decode_exact_json_bytes
    decode_code = canonical_decode.__code__
    canonical_require_json_depth = require_provider_json_depth
    require_json_depth_code = canonical_require_json_depth.__code__
    canonical_require_response_bytes = require_provider_response_bytes
    require_response_bytes_code = canonical_require_response_bytes.__code__
    canonical_hard_response_bytes = HARD_MAX_PROVIDER_RESPONSE_BYTES
    canonical_freeze = _freeze_json
    freeze_code = canonical_freeze.__code__
    canonical_instant = _instant
    instant_code = canonical_instant.__code__
    canonical_journal_type = JournalStore
    canonical_journal_load_events = JournalStore.load_events
    journal_load_events_code = canonical_journal_load_events.__code__
    canonical_journal_decode_event_row = JournalStore._decode_event_row
    journal_decode_event_row_code = canonical_journal_decode_event_row.__code__
    canonical_journal_connect = JournalStore._connect
    journal_connect_code = canonical_journal_connect.__code__
    canonical_journal_require_text = JournalStore._require_text
    journal_require_text_code = canonical_journal_require_text.__code__
    canonical_journal_store_identity = JournalStore.store_identity
    canonical_journal_schema_version = JournalStore.SCHEMA_VERSION
    canonical_event_instant = _canonical_submission_event_instant
    event_instant_code = canonical_event_instant.__code__
    canonical_terminal_semantics = _exact_response_terminal_semantics_are_canonical
    terminal_semantics_code = canonical_terminal_semantics.__code__
    canonical_event_id = _event_id
    event_id_code = canonical_event_id.__code__
    canonical_identity_digest = _identity_digest
    identity_digest_code = canonical_identity_digest.__code__
    canonical_marker_helper = _has_exact_response_markers
    marker_helper_code = canonical_marker_helper.__code__
    canonical_response_markers = _EXACT_RESPONSE_MARKERS
    canonical_uuid5 = uuid5
    canonical_namespace_url = NAMESPACE_URL
    canonical_isinstance = isinstance
    canonical_len = len
    canonical_any = any
    canonical_set = set
    canonical_datetime = datetime
    canonical_timezone = timezone

    states: dict[int, tuple[object, tuple[object, ...]]] = {}
    field_names = (
        "attempt_id",
        "aggregate_id",
        "provider",
        "request_hash",
        "client_order_id",
        "environment",
        "account_id",
        "prepared_at",
        "sent_at",
        "submission_scope",
        "submission_scope_hash",
        "response_bytes",
        "response_sha256",
        "response_encoding",
        "terminal_state",
        "ambiguity_reason",
        "retry_disposition",
        "http_status",
        "_factory_token",
    )

    exact_field_names = canonical_frozenset(field_names)

    def authority_changed():
        raise error_type("submission response binding authority is unavailable")

    def implementation_changed():
        if (
            SubmissionResponseBinding is not binding_type
            or _SUBMISSION_RESPONSE_BINDING_TOKEN is not binding_token
            or ValueError is not error_type
            or type is not canonical_type
            or id is not canonical_id
            or tuple is not canonical_tuple
            or frozenset is not canonical_frozenset
            or dict is not canonical_dict
            or range is not canonical_range
            or enumerate is not canonical_enumerate
            or str is not canonical_str
            or int is not canonical_int
            or bytes is not canonical_bytes
            or object is not canonical_object
            or getattr is not canonical_getattr
            or MappingProxyType is not mapping_proxy_type
            or weakref_ref is not canonical_weakref_ref
            or JournalStore is not canonical_journal_type
            or JournalStore.load_events is not canonical_journal_load_events
            or canonical_journal_load_events.__code__ is not journal_load_events_code
            or JournalStore._decode_event_row is not canonical_journal_decode_event_row
            or canonical_journal_decode_event_row.__code__
            is not journal_decode_event_row_code
            or JournalStore._connect is not canonical_journal_connect
            or canonical_journal_connect.__code__ is not journal_connect_code
            or JournalStore._require_text is not canonical_journal_require_text
            or canonical_journal_require_text.__code__ is not journal_require_text_code
            or JournalStore.store_identity is not canonical_journal_store_identity
            or JournalStore.SCHEMA_VERSION != canonical_journal_schema_version
            or _canonical_submission_event_instant is not canonical_event_instant
            or canonical_getattr(canonical_event_instant, "__code__", None)
            is not event_instant_code
            or _exact_response_terminal_semantics_are_canonical
            is not canonical_terminal_semantics
            or canonical_getattr(canonical_terminal_semantics, "__code__", None)
            is not terminal_semantics_code
            or _event_id is not canonical_event_id
            or canonical_getattr(canonical_event_id, "__code__", None)
            is not event_id_code
            or _identity_digest is not canonical_identity_digest
            or canonical_getattr(canonical_identity_digest, "__code__", None)
            is not identity_digest_code
            or _has_exact_response_markers is not canonical_marker_helper
            or canonical_getattr(canonical_marker_helper, "__code__", None)
            is not marker_helper_code
            or _EXACT_RESPONSE_MARKERS is not canonical_response_markers
            or uuid5 is not canonical_uuid5
            or NAMESPACE_URL is not canonical_namespace_url
            or isinstance is not canonical_isinstance
            or len is not canonical_len
            or any is not canonical_any
            or set is not canonical_set
            or datetime is not canonical_datetime
            or timezone is not canonical_timezone
            or _canonical_journal_authority_snapshot is not canonical_journal_snapshot
            or canonical_getattr(canonical_journal_snapshot, "__code__", None)
            is not journal_snapshot_code
            or _journal_store_call is not canonical_journal_call
            or canonical_getattr(canonical_journal_call, "__code__", None)
            is not journal_call_code
            or SubmissionResponseBinding.__init__ is not canonical_binding_init
            or canonical_getattr(canonical_binding_init, "__code__", None)
            is not binding_init_code
            or SubmissionResponseBinding.__post_init__ is not canonical_binding_post_init
            or canonical_getattr(canonical_binding_post_init, "__code__", None)
            is not binding_post_init_code
            or canonical_json is not canonical_json_function
            or json is not canonical_json_module
            or re is not canonical_re_module
            or sha256 is not canonical_sha256
            or submission_attempt_aggregate_id is not canonical_attempt_id
            or canonical_getattr(canonical_attempt_id, "__code__", None)
            is not attempt_id_code
            or _decode_exact_json_bytes is not canonical_decode
            or canonical_getattr(canonical_decode, "__code__", None)
            is not decode_code
            or require_provider_json_depth is not canonical_require_json_depth
            or canonical_getattr(canonical_require_json_depth, "__code__", None)
            is not require_json_depth_code
            or require_provider_response_bytes is not canonical_require_response_bytes
            or canonical_getattr(canonical_require_response_bytes, "__code__", None)
            is not require_response_bytes_code
            or type(HARD_MAX_PROVIDER_RESPONSE_BYTES) is not canonical_int
            or HARD_MAX_PROVIDER_RESPONSE_BYTES != canonical_hard_response_bytes
            or _freeze_json is not canonical_freeze
            or canonical_getattr(canonical_freeze, "__code__", None)
            is not freeze_code
            or _instant is not canonical_instant
            or canonical_getattr(canonical_instant, "__code__", None)
            is not instant_code
            or canonical_getattr(canonical_loader, "__code__", None)
            is not loader_code
        ):
            authority_changed()

    def raw_snapshot(value):
        state = object_getattribute(value, "__dict__")
        if canonical_type(state) is not canonical_dict:
            authority_changed()
        if canonical_frozenset(state) != exact_field_names:
            authority_changed()
        return canonical_tuple(state[name] for name in field_names)

    def prune():
        for object_id, (value_ref, _snapshot) in canonical_tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(value):
        implementation_changed()
        if canonical_type(value) is not binding_type:
            authority_changed()
        current = raw_snapshot(value)
        if current[-1] is not binding_token:
            authority_changed()
        if canonical_type(current[9]) is not mapping_proxy_type:
            authority_changed()
        if canonical_type(current[11]) is not canonical_bytes:
            authority_changed()
        prune()
        object_id = canonical_id(value)
        previous = states.get(object_id)
        if previous is not None and previous[0]() is not None:
            authority_changed()
        states[object_id] = (canonical_weakref_ref(value), current)

    def require_canonical_submission_response_binding(value):
        implementation_changed()
        if canonical_type(value) is not binding_type:
            authority_changed()
        prune()
        state = states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            authority_changed()
        expected = state[1]
        current = raw_snapshot(value)

        for index in canonical_range(9):
            if canonical_type(current[index]) is not canonical_str:
                authority_changed()
            if current[index] != expected[index]:
                authority_changed()
        if canonical_type(current[9]) is not mapping_proxy_type or current[9] is not expected[9]:
            authority_changed()
        for index in (10, 12, 13, 14):
            if canonical_type(current[index]) is not canonical_str or current[index] != expected[index]:
                authority_changed()
        if canonical_type(current[11]) is not canonical_bytes or current[11] is not expected[11]:
            authority_changed()
        for index in (15, 16):
            if current[index] is None:
                if expected[index] is not None:
                    authority_changed()
            elif (
                canonical_type(current[index]) is not canonical_str
                or current[index] != expected[index]
            ):
                authority_changed()
        if current[17] is None:
            if expected[17] is not None:
                authority_changed()
        elif (
            canonical_type(current[17]) is not canonical_int
            or current[17] != expected[17]
        ):
            authority_changed()
        if current[18] is not binding_token:
            authority_changed()
        return expected

    def submission_response_binding_projection(value):
        current = require_canonical_submission_response_binding(value)
        return mapping_proxy_type(
            {
                name: current[index]
                for index, name in canonical_enumerate(field_names[:-1])
            }
        )

    def registered_loader(
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        attempt_id: str,
    ) -> SubmissionResponseBinding:
        implementation_changed()
        value = canonical_loader(
            store,
            environment=environment,
            account_id=account_id,
            attempt_id=attempt_id,
        )
        implementation_changed()
        register(value)
        return value

    return (
        registered_loader,
        require_canonical_submission_response_binding,
        submission_response_binding_projection,
    )


def stable_client_order_id(
    provider: str,
    intent_id: str,
    *,
    environment: str,
    account_id: str,
    max_length: int = 32,
    client_id_format: str = "TOKEN",
) -> str:
    if type(provider) is not str or not provider.strip():
        raise ValueError("provider is required")
    if type(intent_id) is not str or not intent_id.strip():
        raise ValueError("intent_id is required")
    normalized_environment = environment.strip().upper() if type(environment) is str else ""
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    if type(account_id) is not str or not account_id.strip():
        raise ValueError("account_id is required")
    if type(client_id_format) is not str:
        raise TypeError("client_id_format must be text")
    normalized_format = client_id_format.strip().upper()
    if normalized_format not in {"TOKEN", "UUID"}:
        raise ValueError("client_id_format must be TOKEN or UUID")
    digest = _identity_digest(
        provider.strip().lower(),
        normalized_environment,
        account_id.strip(),
        intent_id.strip(),
    )
    if normalized_format == "UUID":
        if (
            type(max_length) is not int
            or max_length < 36
        ):
            raise ValueError(
                "UUID client-order identity requires max_length of at least 36"
            )
        return str(uuid5(NAMESPACE_URL, "client-order:" + digest))
    if type(max_length) is not int or max_length < 20:
        raise ValueError(
            "max_length must be an integer of at least 20 "
            "to preserve client-order identity entropy"
        )
    return ("at-" + digest)[:max_length]


def _instant(value: str) -> datetime:
    if type(value) is not str or not value.strip():
        raise ValueError("now must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("now must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("now must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_submission_event_instant(
    event: Mapping[str, Any],
    *,
    event_name: str,
) -> datetime:
    canonical_values: list[str] = []
    instants: list[datetime] = []
    for field_name in ("occurred_at", "observed_at", "committed_at"):
        value = event.get(field_name)
        if type(value) is not str:
            raise ValueError(
                f"durable {event_name} timestamp authority is invalid"
            )
        try:
            point = _instant(value)
        except ValueError as error:
            raise ValueError(
                f"durable {event_name} timestamp authority is invalid"
            ) from error
        canonical = point.isoformat().replace("+00:00", "Z")
        if canonical != value:
            raise ValueError(
                f"durable {event_name} timestamp authority is invalid"
            )
        canonical_values.append(value)
        instants.append(point)
    if len(set(canonical_values)) != 1:
        raise ValueError(
            f"durable {event_name} timestamp authority is invalid"
        )
    return instants[0]


def _prepared_lease_state(
    *,
    prepared_at: str,
    now: str,
    lease_seconds: int,
) -> str:
    """Return BEFORE, ACTIVE or EXPIRED using exact microsecond chronology."""

    elapsed = _instant(now) - _instant(prepared_at)
    if elapsed < timedelta(0):
        return "BEFORE"
    elapsed_microseconds = (
        (elapsed.days * 86400 + elapsed.seconds) * 1_000_000
        + elapsed.microseconds
    )
    lease_microseconds = lease_seconds * 1_000_000
    return "ACTIVE" if elapsed_microseconds < lease_microseconds else "EXPIRED"


def _event_id(scope_key: str, attempt_id: str, event_type: str, version: int) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "dispatch-event:" + _identity_digest(
                scope_key,
                attempt_id,
                event_type,
                str(version),
            ),
        )
    )


def _envelope(
    *,
    scope_key: str,
    aggregate_id: str,
    environment: str,
    attempt_id: str,
    event_type: str,
    version: int,
    payload: dict[str, Any],
    now: str,
    owner_epoch: int,
) -> dict[str, Any]:
    timestamp = _instant(now).isoformat().replace("+00:00", "Z")
    # Detach the complete event payload before hashing.  This is especially
    # important after the irreversible send barrier: a provider response may
    # contain dict/list/string subclasses whose JSON hooks would otherwise run
    # inside payload_digest() before persistence gets a chance to apply its own
    # exact-builtin fence.
    canonical_payload = _detach_submission_json(payload)
    return {
        "event_id": _event_id(scope_key, attempt_id, event_type, version),
        "event_type": event_type,
        "schema_version": "1.0.0",
        "aggregate_type": "submission_attempt",
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "host_id": "local-mvp",
        "owner_epoch": str(owner_epoch),
        "environment": environment,
        "occurred_at": timestamp,
        "observed_at": timestamp,
        "committed_at": timestamp,
        "correlation_id": str(
            uuid5(
                NAMESPACE_URL,
                "dispatch-correlation:" + _identity_digest(scope_key, attempt_id),
            )
        ),
        "causation_id": None,
        "payload": canonical_payload,
        "payload_hash": payload_digest(canonical_payload),
        "evidence_refs": [],
    }


(
    load_submission_response_binding,
    require_canonical_submission_response_binding,
    submission_response_binding_projection,
) = _install_submission_response_binding_authority(
    load_submission_response_binding
)
del _install_submission_response_binding_authority


class GuardedDispatcher:
    """Persist-before-send dispatcher that never blindly retries ambiguity.

    Provider wrappers receive a final_guard callback and must invoke it
    immediately before the actual outbound request. Provider qualification must
    prove that contract with an outbound-request counter.
    """

    def __init__(
        self,
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        owner_token: str | None = None,
        owner_epoch: int = 1,
        prepared_lease_seconds: int = 60,
    ):
        # SubmissionPrepared/Sending/Sent/Unknown is financial send-state
        # authority. Capture the exact selected physical generation and reject
        # caller-polymorphic or instance-shadowed journal state before any read
        # or append can run. A stronger sealed product-issued capability remains
        # a recovery/persistence integration responsibility.
        (
            self._journal_store_path,
            self._journal_store_identity,
        ) = _canonical_journal_authority_snapshot(store)
        self.store = store
        normalized_environment = (
            environment.strip().upper() if type(environment) is str else ""
        )
        if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
        if type(account_id) is not str or not account_id.strip():
            raise ValueError("account_id is required")
        self.environment = normalized_environment
        self.account_id = account_id.strip()
        self.scope_key = _identity_digest(self.environment, self.account_id)
        if owner_token is not None and (type(owner_token) is not str or not owner_token.strip()):
            raise ValueError("owner_token must be exact non-empty text when provided")
        self.owner_token = owner_token if owner_token is not None else str(uuid4())
        if type(owner_epoch) is not int or owner_epoch < 1:
            raise ValueError("owner_epoch must be a positive integer")
        self.owner_epoch = owner_epoch
        if type(prepared_lease_seconds) is not int or prepared_lease_seconds < 1:
            raise ValueError("prepared_lease_seconds must be a positive exact integer")
        self.prepared_lease_seconds = prepared_lease_seconds
        self._dispatch_authority_state = (
            self.environment,
            self.account_id,
            self.scope_key,
            self.owner_token,
            self.owner_epoch,
            self.prepared_lease_seconds,
        )

    def _require_dispatch_authority_state(self) -> None:
        expected = self._dispatch_authority_state
        if type(expected) is not tuple or len(expected) != 6:
            raise PermissionError("dispatcher authority state changed")
        if (
            type(self.environment) is not str
            or type(self.account_id) is not str
            or type(self.scope_key) is not str
            or type(self.owner_token) is not str
            or type(self.owner_epoch) is not int
            or type(self.prepared_lease_seconds) is not int
        ):
            raise PermissionError("dispatcher authority state changed")
        current = (
            self.environment,
            self.account_id,
            self.scope_key,
            self.owner_token,
            self.owner_epoch,
            self.prepared_lease_seconds,
        )
        if current != expected:
            raise PermissionError("dispatcher authority state changed")

    def _journal_store_authority(self) -> JournalStore:
        self._require_dispatch_authority_state()
        store = self.store
        path, identity = _canonical_journal_authority_snapshot(store)
        if (
            path != self._journal_store_path
            or identity != self._journal_store_identity
        ):
            raise PermissionError("submission journal authority changed")
        return store

    def _aggregate_id(self, attempt_id: str) -> str:
        return submission_attempt_aggregate_id(
            environment=self.environment,
            account_id=self.account_id,
            attempt_id=attempt_id,
        )

    def _events(self, attempt_id: str) -> list[dict[str, Any]]:
        store = self._journal_store_authority()
        return _journal_store_call(
            store,
            "load_events",
            "submission_attempt",
            self._aggregate_id(attempt_id),
        )

    def _append(
        self,
        *,
        attempt_id: str,
        event_type: str,
        version: int,
        payload: dict[str, Any],
        now: str,
    ):
        store = self._journal_store_authority()
        return _journal_store_call(
            store,
            "append_event",
            _envelope(
                scope_key=self.scope_key,
                aggregate_id=self._aggregate_id(attempt_id),
                environment=self.environment,
                attempt_id=attempt_id,
                event_type=event_type,
                version=version,
                payload=payload,
                now=now,
                owner_epoch=self.owner_epoch,
            ),
            outbox_topic="autotrade.submission.events",
        )

    @staticmethod
    def _outcome_from_terminal(event: dict[str, Any], client_order_id: str) -> DispatchOutcome:
        payload = event["payload"]
        if not _exact_response_terminal_semantics_are_canonical(
            event["event_type"],
            payload,
        ):
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "exact_response_terminal_semantics_invalid",
            )
        if event["event_type"] == "SubmissionSent":
            if _has_exact_response_markers(payload):
                # Any reserved exact marker commits the row to the SHA-bound
                # evidence contract. Partial/mislabeled exact rows must never
                # fall through to the historical response mirror.
                if payload.get("response_encoding") != "utf-8-json":
                    return DispatchOutcome(
                        "UNKNOWN", client_order_id, None, "exact_response_invalid"
                    )
                http_status = payload.get("http_status")
                if http_status is not None and (
                    type(http_status) is not int
                    or http_status < 100
                    or http_status > 599
                ):
                    return DispatchOutcome(
                        "UNKNOWN", client_order_id, None, "exact_response_invalid"
                    )
                response_text = payload.get("response_text")
                response_hash = payload.get("response_sha256")
                if (
                    type(response_text) is not str
                    or not response_text
                    or type(response_hash) is not str
                ):
                    return DispatchOutcome(
                        "UNKNOWN", client_order_id, None, "exact_response_unavailable"
                    )
                try:
                    raw = response_text.encode("utf-8", errors="strict")
                    if "sha256:" + sha256(raw).hexdigest() != response_hash:
                        raise ValueError("SHA-bound exact response mismatch")
                    exact_payload = _decode_exact_json_bytes(raw)
                except (UnicodeError, ValueError, TypeError):
                    return DispatchOutcome(
                        "UNKNOWN", client_order_id, None, "exact_response_invalid"
                    )
                return DispatchOutcome(
                    "SENT", client_order_id, exact_payload, "sent_confirmed"
                )
            # Only marker-free historical rows may use the legacy mirror.
            return DispatchOutcome(
                "SENT", client_order_id, payload.get("response"), "sent_confirmed"
            )
        if event["event_type"] == "SubmissionBlocked":
            return DispatchOutcome("BLOCKED", client_order_id, None, payload.get("reason", "blocked"))
        if event["event_type"] == "SubmissionUnknown":
            return DispatchOutcome("UNKNOWN", client_order_id, None, payload.get("reason", "unknown"))
        raise ValueError("event is not terminal")

    def _existing_history_is_canonical(
        self,
        *,
        events: list[dict[str, Any]],
        attempt_id: str,
        client_order_id: str,
        expected_prepared: dict[str, Any],
    ) -> bool:
        if not events:
            return False
        aggregate_id = self._aggregate_id(attempt_id)
        versions: list[int] = []
        event_types: list[str] = []
        event_instants: list[datetime] = []
        for event in events:
            if type(event) is not dict:
                return False
            version = event.get("aggregate_version")
            event_type = event.get("event_type")
            payload = event.get("payload")
            if type(version) is not int or type(event_type) is not str:
                return False
            if event.get("event_id") != _event_id(
                self.scope_key,
                attempt_id,
                event_type,
                version,
            ):
                return False
            if (
                event.get("aggregate_type") != "submission_attempt"
                or event.get("aggregate_id") != aggregate_id
                or event.get("environment") != self.environment
                or type(payload) is not dict
            ):
                return False
            durable_client_order_id = payload.get("client_order_id")
            if (
                type(durable_client_order_id) is not str
                or durable_client_order_id != client_order_id
            ):
                return False
            try:
                event_instant = _canonical_submission_event_instant(
                    event,
                    event_name=event_type,
                )
            except ValueError:
                return False
            versions.append(version)
            event_types.append(event_type)
            event_instants.append(event_instant)

        if versions != list(range(1, len(events) + 1)):
            return False
        if any(
            earlier > later
            for earlier, later in zip(event_instants, event_instants[1:])
        ):
            return False
        prepared_payload = events[0].get("payload")
        if (
            type(prepared_payload) is not dict
            or prepared_payload.get("prepared_at") != events[0].get("committed_at")
            or type(expected_prepared) is not dict
        ):
            return False
        try:
            durable_prepared_projection = {
                key: prepared_payload[key]
                for key in expected_prepared
            }
            if (
                canonical_json(durable_prepared_projection)
                != canonical_json(expected_prepared)
            ):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        prepared_scope = prepared_payload.get("submission_scope")
        prepared_scope_hash = prepared_payload.get("submission_scope_hash")
        if (
            type(prepared_scope) is not dict
            or type(prepared_scope_hash) is not str
            or "sha256:"
            + sha256(canonical_json(prepared_scope).encode("utf-8")).hexdigest()
            != prepared_scope_hash
        ):
            return False

        prepared_owner_token = prepared_payload.get("owner_token")
        prepared_owner_epoch = prepared_payload.get("owner_epoch")
        if (
            type(prepared_owner_token) is not str
            or not prepared_owner_token
            or type(prepared_owner_epoch) is not int
            or prepared_owner_epoch < 1
            or events[0].get("owner_epoch") != str(prepared_owner_epoch)
        ):
            return False
        if len(events) >= 2 and event_types[1] == "SubmissionSending":
            sending_payload = events[1].get("payload")
            if (
                type(sending_payload) is not dict
                or type(sending_payload.get("owner_token")) is not str
                or sending_payload.get("owner_token") != prepared_owner_token
                or type(sending_payload.get("owner_epoch")) is not int
                or sending_payload.get("owner_epoch") != prepared_owner_epoch
                or events[1].get("owner_epoch") != str(prepared_owner_epoch)
            ):
                return False
        if event_types[-1] == "SubmissionSent" or (
            event_types[-1] == "SubmissionUnknown"
            and _has_exact_response_markers(events[-1]["payload"])
        ):
            if events[-1].get("owner_epoch") != str(prepared_owner_epoch):
                return False
        history_shape = tuple(event_types)
        if history_shape not in {
            ("SubmissionPrepared",),
            ("SubmissionPrepared", "SubmissionBlocked"),
            ("SubmissionPrepared", "SubmissionUnknown"),
            ("SubmissionPrepared", "SubmissionSending"),
            ("SubmissionPrepared", "SubmissionBlocked", "SubmissionUnknown"),
            ("SubmissionPrepared", "SubmissionSending", "SubmissionSent"),
            ("SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"),
        }:
            return False
        terminal_type = event_types[-1]
        if terminal_type in {"SubmissionSent", "SubmissionUnknown"}:
            return _exact_response_terminal_semantics_are_canonical(
                terminal_type,
                events[-1]["payload"],
            )
        return True

    def _terminal_outcome_from_existing_history(
        self,
        *,
        events: list[dict[str, Any]],
        attempt_id: str,
        client_order_id: str,
        expected_prepared: dict[str, Any],
    ) -> DispatchOutcome:
        if not self._existing_history_is_canonical(
            events=events,
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            expected_prepared=expected_prepared,
        ):
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "durable_submission_history_invalid",
            )
        last = events[-1]
        if last["event_type"] not in {
            "SubmissionSent",
            "SubmissionBlocked",
            "SubmissionUnknown",
        }:
            raise ValueError("durable submission history is not terminal")
        return self._outcome_from_terminal(last, client_order_id)

    def _recover_existing(
        self,
        *,
        attempt_id: str,
        client_order_id: str,
        now: str,
        expected_prepared: dict[str, Any],
    ) -> DispatchOutcome:
        events = self._events(attempt_id)
        if not events:
            raise RuntimeError("submission attempt disappeared")
        if not self._existing_history_is_canonical(
            events=events,
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            expected_prepared=expected_prepared,
        ):
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "durable_submission_history_invalid",
            )
        last = events[-1]
        if last["event_type"] in {"SubmissionSent", "SubmissionBlocked", "SubmissionUnknown"}:
            return self._terminal_outcome_from_existing_history(
                events=events,
                attempt_id=attempt_id,
                client_order_id=client_order_id,
            expected_prepared=expected_prepared,
            )
        if last["event_type"] == "SubmissionSending":
            # Recovery time is caller/process input, but the durable Sending row
            # is already a causal lower bound. Never let a restarted or skewed
            # clock place terminal UNKNOWN chronologically before the send
            # barrier it is resolving.
            sending_at = _instant(last["committed_at"])
            recovery_at = _instant(now)
            terminal_at = max(sending_at, recovery_at).isoformat().replace(
                "+00:00", "Z"
            )
            try:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=last["aggregate_version"] + 1,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": "recovered_after_send_barrier_without_terminal_result",
                    },
                    now=terminal_at,
                )
            except ValueError:
                # Multiple recovery owners may observe the same Sending cut.
                # The first terminal append wins; every loser must converge on
                # that durable result instead of surfacing an aggregate-version
                # race as an operational retry signal.
                current = self._events(attempt_id)
                if not current:
                    raise RuntimeError("submission attempt disappeared")
                current_last = current[-1]
                if current_last["event_type"] in {
                    "SubmissionSent",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                }:
                    return self._terminal_outcome_from_existing_history(
                        events=current,
                        attempt_id=attempt_id,
                        client_order_id=client_order_id,
                        expected_prepared=expected_prepared,
                    )
                raise
            current = self._events(attempt_id)
            return self._terminal_outcome_from_existing_history(
                events=current,
                attempt_id=attempt_id,
                client_order_id=client_order_id,
            expected_prepared=expected_prepared,
            )
        if last["event_type"] != "SubmissionPrepared":
            raise RuntimeError(f"unsupported submission attempt state: {last['event_type']}")

        lease_state = _prepared_lease_state(
            prepared_at=last["payload"]["prepared_at"],
            now=now,
            lease_seconds=self.prepared_lease_seconds,
        )
        if lease_state == "BEFORE":
            return DispatchOutcome("IN_PROGRESS", client_order_id, None, "clock_before_prepared_timestamp")
        if lease_state == "ACTIVE":
            return DispatchOutcome("IN_PROGRESS", client_order_id, None, "prepared_owner_lease_active")
        # SubmissionPrepared is durably before the irreversible boundary. If its
        # lease expires while no SubmissionSending exists, the journal proves
        # zero wire. Fence the stale owner as BLOCKED; UNKNOWN remains reserved
        # for states that may actually have crossed the provider boundary.
        try:
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionBlocked",
                version=last["aggregate_version"] + 1,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "prepared_owner_lease_expired_before_send",
                },
                now=now,
            )
        except ValueError:
            # The final send barrier may win the aggregate-version race after
            # recovery observed Prepared but before it can commit Blocked.
            # Re-read durable truth rather than leaking a CAS conflict or
            # fabricating zero-wire safety.  Once Sending exists the outcome is
            # ambiguous and must converge through the existing UNKNOWN path.
            current = self._events(attempt_id)
            if not current:
                raise RuntimeError("submission attempt disappeared")
            current_last = current[-1]
            if current_last["event_type"] in {
                "SubmissionSent",
                "SubmissionBlocked",
                "SubmissionUnknown",
            }:
                return self._terminal_outcome_from_existing_history(
                    events=current,
                    attempt_id=attempt_id,
                    client_order_id=client_order_id,
                    expected_prepared=expected_prepared,
                )
            if current_last["event_type"] == "SubmissionSending":
                return self._recover_existing(
                    attempt_id=attempt_id,
                    client_order_id=client_order_id,
                    now=now,
                    expected_prepared=expected_prepared,
                )
            raise
        current = self._events(attempt_id)
        return self._terminal_outcome_from_existing_history(
            events=current,
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            expected_prepared=expected_prepared,
        )

    def dispatch(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        provider: str,
        request: Mapping[str, Any],
        now: str,
        authority_check: AuthorityCheck,
        transport_send: TransportSend,
        client_id_max_length: int = 32,
        client_id_format: str = "TOKEN",
        final_barrier_clock: Callable[[], str] | None = None,
        sender_check: SenderCheck | None = None,
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        self._require_dispatch_authority_state()
        dispatch_call_authority = (
            self.store,
            self._journal_store_path,
            self._journal_store_identity,
            self._dispatch_authority_state,
            self.environment,
            self.account_id,
            self.scope_key,
            self.owner_token,
            self.owner_epoch,
            self.prepared_lease_seconds,
        )
        dispatcher_class_surfaces = tuple(
            (base, tuple(base.__dict__.items()))
            for base in type(self).__mro__
            if base is not object
        )
        dispatcher_class_owned_names = frozenset(
            name
            for base, members in dispatcher_class_surfaces
            for name, _member in members
        )
        dispatcher_class_executable_states = []
        for _base, members in dispatcher_class_surfaces:
            for _name, member in members:
                member_code = getattr(member, "__code__", None)
                if member_code is None:
                    continue
                member_kwdefaults = getattr(member, "__kwdefaults__", None)
                if (
                    member_kwdefaults is not None
                    and type(member_kwdefaults) is not dict
                ):
                    raise RuntimeError(
                        "dispatcher method keyword defaults are unavailable"
                    )
                member_kwdefaults_copy = (
                    dict(member_kwdefaults)
                    if type(member_kwdefaults) is dict
                    else None
                )
                member_kwdefaults_fingerprint = (
                    tuple(
                        (id(key), id(value))
                        for key, value in dict.items(member_kwdefaults)
                    )
                    if type(member_kwdefaults) is dict
                    else None
                )
                dispatcher_class_executable_states.append(
                    (
                        member,
                        member_code,
                        getattr(member, "__defaults__", None),
                        member_kwdefaults,
                        member_kwdefaults_copy,
                        member_kwdefaults_fingerprint,
                    )
                )
        dispatcher_class_executable_states = tuple(
            dispatcher_class_executable_states
        )
        initial_instance_state = vars(self)
        initial_instance_class_shadow = tuple(
            (name, initial_instance_state[name])
            for name in dispatcher_class_owned_names
            if name in initial_instance_state
        )

        def restore_dispatcher_class_surface() -> None:
            # A callback may mutate GuardedDispatcher after the send barrier.
            # Restore the exact per-call class surface and executable function
            # state before any reconciliation read/write can dispatch through
            # self._events/self._append again.
            for base, members in dispatcher_class_surfaces:
                expected_members = dict(members)
                current_names = set(base.__dict__)
                for name in current_names - set(expected_members):
                    delattr(base, name)
                for name, member in members:
                    if (
                        name not in base.__dict__
                        or base.__dict__[name] is not member
                    ):
                        setattr(base, name, member)
            for (
                member,
                expected_code,
                expected_defaults,
                expected_kwdefaults,
                expected_kwdefaults_copy,
                expected_kwdefaults_fingerprint,
            ) in dispatcher_class_executable_states:
                if getattr(member, "__code__", None) is not expected_code:
                    setattr(member, "__code__", expected_code)
                if getattr(member, "__defaults__", None) is not expected_defaults:
                    setattr(member, "__defaults__", expected_defaults)
                current_kwdefaults = getattr(member, "__kwdefaults__", None)
                if current_kwdefaults is not expected_kwdefaults:
                    setattr(member, "__kwdefaults__", expected_kwdefaults)
                if (
                    expected_kwdefaults_fingerprint is not None
                    and type(expected_kwdefaults) is dict
                ):
                    current_fingerprint = tuple(
                        (id(key), id(value))
                        for key, value in dict.items(expected_kwdefaults)
                    )
                    if current_fingerprint != expected_kwdefaults_fingerprint:
                        dict.clear(expected_kwdefaults)
                        dict.update(
                            expected_kwdefaults,
                            expected_kwdefaults_copy,
                        )

        def require_dispatch_call_authority() -> None:
            (
                authority_store,
                authority_path,
                authority_identity,
                authority_state,
                environment,
                account_id,
                scope_key,
                owner_token,
                owner_epoch,
                prepared_lease_seconds,
            ) = dispatch_call_authority
            current = (
                self.environment,
                self.account_id,
                self.scope_key,
                self.owner_token,
                self.owner_epoch,
                self.prepared_lease_seconds,
            )
            expected = (
                environment,
                account_id,
                scope_key,
                owner_token,
                owner_epoch,
                prepared_lease_seconds,
            )
            current_instance_state = vars(self)
            expected_shadow = dict(initial_instance_class_shadow)
            current_shadow_names = {
                name
                for name in dispatcher_class_owned_names
                if name in current_instance_state
            }
            shadow_is_unchanged = (
                current_shadow_names == set(expected_shadow)
                and all(
                    current_instance_state[name] is member
                    for name, member in initial_instance_class_shadow
                )
            )
            class_surface_is_unchanged = True
            for base, members in dispatcher_class_surfaces:
                current_members = base.__dict__
                if len(current_members) != len(members):
                    class_surface_is_unchanged = False
                    break
                if any(
                    name not in current_members or current_members[name] is not member
                    for name, member in members
                ):
                    class_surface_is_unchanged = False
                    break
            executable_state_is_unchanged = True
            for (
                member,
                expected_code,
                expected_defaults,
                expected_kwdefaults,
                _expected_kwdefaults_copy,
                expected_kwdefaults_fingerprint,
            ) in dispatcher_class_executable_states:
                if (
                    getattr(member, "__code__", None) is not expected_code
                    or getattr(member, "__defaults__", None)
                    is not expected_defaults
                    or getattr(member, "__kwdefaults__", None)
                    is not expected_kwdefaults
                ):
                    executable_state_is_unchanged = False
                    break
                if (
                    expected_kwdefaults_fingerprint is not None
                    and type(expected_kwdefaults) is dict
                    and tuple(
                        (id(key), id(value))
                        for key, value in dict.items(expected_kwdefaults)
                    )
                    != expected_kwdefaults_fingerprint
                ):
                    executable_state_is_unchanged = False
                    break
            if (
                self.store is authority_store
                and self._journal_store_path is authority_path
                and self._journal_store_identity is authority_identity
                and self._dispatch_authority_state is authority_state
                and shadow_is_unchanged
                and class_surface_is_unchanged
                and executable_state_is_unchanged
                and type(self.environment) is str
                and type(self.account_id) is str
                and type(self.scope_key) is str
                and type(self.owner_token) is str
                and type(self.owner_epoch) is int
                and type(self.prepared_lease_seconds) is int
                and current == expected
            ):
                return

            # Restore the exact invocation-selected authority before propagating
            # the failure. Error handling must not continue through a store,
            # scope, instance shadow or rebound class method that an external
            # callback retargeted.
            if (
                not class_surface_is_unchanged
                or not executable_state_is_unchanged
            ):
                restore_dispatcher_class_surface()
            self.store = authority_store
            self._journal_store_path = authority_path
            self._journal_store_identity = authority_identity
            self._dispatch_authority_state = authority_state
            for name in dispatcher_class_owned_names:
                if name in expected_shadow:
                    current_instance_state[name] = expected_shadow[name]
                else:
                    current_instance_state.pop(name, None)
            (
                self.environment,
                self.account_id,
                self.scope_key,
                self.owner_token,
                self.owner_epoch,
                self.prepared_lease_seconds,
            ) = expected
            raise _DispatchAuthorityChanged(
                "dispatcher authority changed during dispatch"
            )

        for value, name in (
            (attempt_id, "attempt_id"),
            (intent_id, "intent_id"),
            (intent_hash, "intent_hash"),
            (provider, "provider"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} is required")
        if type(request) is not dict:
            raise TypeError("request must be an exact dict")
        _instant(now)
        request_dict = _detach_submission_json(request)
        request_canonical = canonical_json(request_dict)
        request_frozen = _freeze_json(request_dict)
        request_hash = "sha256:" + sha256(request_canonical.encode("utf-8")).hexdigest()
        if submission_scope is None:
            scope_dict: dict[str, Any] = {}
        else:
            if type(submission_scope) is not dict:
                raise TypeError("submission_scope must be an exact dict")
            scope_dict = _detach_submission_json(submission_scope)
        scope_canonical = canonical_json(scope_dict)
        submission_scope_hash = (
            "sha256:" + sha256(scope_canonical.encode("utf-8")).hexdigest()
        )
        client_order_id = stable_client_order_id(
            provider,
            intent_id,
            environment=self.environment,
            account_id=self.account_id,
            max_length=client_id_max_length,
            client_id_format=client_id_format,
        )

        expected_prepared = {
            "attempt_id": attempt_id,
            "intent_id": intent_id,
            "intent_hash": intent_hash,
            "provider": provider,
            "request_hash": request_hash,
            "client_order_id": client_order_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "submission_scope": scope_dict,
            "submission_scope_hash": submission_scope_hash,
        }
        existing = self._events(attempt_id)
        if existing:
            prepared = existing[0].get("payload")
            if type(prepared) is not dict:
                return DispatchOutcome(
                    "UNKNOWN",
                    client_order_id,
                    None,
                    "durable_submission_history_invalid",
                )
            if any(
                prepared.get(key) != value
                for key, value in expected_prepared.items()
            ):
                raise ValueError("attempt_id conflicts with existing submission content")
            return self._recover_existing(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                now=now,
                expected_prepared=expected_prepared,
            )

        prepared_payload = {
            "attempt_id": attempt_id,
            "intent_id": intent_id,
            "intent_hash": intent_hash,
            "provider": provider,
            "request_hash": request_hash,
            "client_order_id": client_order_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "owner_token": self.owner_token,
            "owner_epoch": self.owner_epoch,
            "prepared_at": _instant(now).isoformat().replace("+00:00", "Z"),
            "submission_scope": scope_dict,
            "submission_scope_hash": submission_scope_hash,
        }
        prepared = self._append(
            attempt_id=attempt_id,
            event_type="SubmissionPrepared",
            version=1,
            payload=prepared_payload,
            now=now,
        )
        if not prepared.inserted:
            return self._recover_existing(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                now=now,
                expected_prepared=expected_prepared,
            )

        try:
            authority_result = authority_check(intent_hash, now)
        except Exception as error:
            require_dispatch_call_authority()
            reason = f"authority_check_failed_before_send:{type(error).__name__}"
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionBlocked",
                version=2,
                payload={"client_order_id": client_order_id, "reason": reason},
                now=now,
            )
            return DispatchOutcome(
                "BLOCKED",
                client_order_id,
                None,
                "authority_check_failed_before_send",
            )
        require_dispatch_call_authority()
        allowed, reason = _validated_authority_result(authority_result)
        if not allowed:
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionBlocked",
                version=2,
                payload={"client_order_id": client_order_id, "reason": reason},
                now=now,
            )
            return DispatchOutcome("BLOCKED", client_order_id, None, reason)

        guard_called = False
        barrier_passed = False
        barrier_now = now

        def authority_change_after_send_outcome() -> DispatchOutcome:
            # SubmissionSending was durably committed before barrier_passed became
            # true. Do not call any dispatcher method after detecting a mutable
            # dispatcher/class authority change: that method surface itself may
            # be the thing a hostile callback rebound. The durable Sending cut
            # is already sufficient to forbid blind resend; normal recovery will
            # converge it to terminal UNKNOWN on the original journal.
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "dispatcher_authority_changed_after_send_barrier",
            )

        def final_guard() -> None:
            nonlocal guard_called, barrier_passed, barrier_now
            require_transport_module_authority()
            if guard_called:
                raise RuntimeError("final send guard may be consumed only once")
            guard_called = True
            require_dispatch_call_authority()
            # request_frozen is a recursively immutable canonical JSON snapshot.
            # Transport cannot pass the barrier for one payload and then mutate
            # the same object before its actual provider call.
            if final_barrier_clock is not None:
                try:
                    barrier_now = final_barrier_clock()
                    parsed_barrier_now = _instant(barrier_now)
                except Exception as error:
                    require_dispatch_call_authority()
                    barrier_now = now
                    reason = (
                        "final_barrier_clock_failed:"
                        + type(error).__name__
                    )
                    self._append(
                        attempt_id=attempt_id,
                        event_type="SubmissionBlocked",
                        version=2,
                        payload={
                            "client_order_id": client_order_id,
                            "reason": reason,
                        },
                        now=barrier_now,
                    )
                    raise DispatchBlocked(reason) from error
                require_dispatch_call_authority()
                if parsed_barrier_now < _instant(now):
                    barrier_now = now
                    self._append(
                        attempt_id=attempt_id,
                        event_type="SubmissionBlocked",
                        version=2,
                        payload={
                            "client_order_id": client_order_id,
                            "reason": "final_barrier_clock_moved_backwards",
                        },
                        now=barrier_now,
                    )
                    raise DispatchBlocked("final_barrier_clock_moved_backwards")
            if self.environment in {"PAPER", "LIVE"} and sender_check is None:
                barrier_reason = "sender_fence_required"
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionBlocked",
                    version=2,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": barrier_reason,
                        "owner_token": self.owner_token,
                        "owner_epoch": self.owner_epoch,
                    },
                    now=barrier_now,
                )
                raise DispatchBlocked(barrier_reason)
            if sender_check is not None:
                try:
                    sender_check(self.owner_token, self.owner_epoch)
                    require_dispatch_call_authority()
                except _DispatchAuthorityChanged:
                    raise
                except Exception as error:
                    require_dispatch_call_authority()
                    barrier_reason = f"sender_fence_rejected:{type(error).__name__}"
                    self._append(
                        attempt_id=attempt_id,
                        event_type="SubmissionBlocked",
                        version=2,
                        payload={
                            "client_order_id": client_order_id,
                            "reason": barrier_reason,
                            "owner_token": self.owner_token,
                            "owner_epoch": self.owner_epoch,
                        },
                        now=barrier_now,
                    )
                    raise DispatchBlocked(barrier_reason) from error
            try:
                authority_result = authority_check(intent_hash, barrier_now)
            except _DispatchAuthorityChanged:
                raise
            except Exception as error:
                require_dispatch_call_authority()
                barrier_reason = (
                    "authority_check_failed_at_final_barrier:"
                    + type(error).__name__
                )
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionBlocked",
                    version=2,
                    payload={"client_order_id": client_order_id, "reason": barrier_reason},
                    now=barrier_now,
                )
                raise DispatchBlocked(barrier_reason) from error
            require_dispatch_call_authority()
            allowed_now, barrier_reason = _validated_authority_result(authority_result)
            if not allowed_now:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionBlocked",
                    version=2,
                    payload={"client_order_id": client_order_id, "reason": barrier_reason},
                    now=barrier_now,
                )
                raise DispatchBlocked(barrier_reason)
            durable_before_send = self._events(attempt_id)
            if (
                not durable_before_send
                or durable_before_send[-1]["event_type"] != "SubmissionPrepared"
                or not self._existing_history_is_canonical(
                    events=durable_before_send,
                    attempt_id=attempt_id,
                    client_order_id=client_order_id,
                    expected_prepared=prepared_payload,
                )
            ):
                raise DispatchBlocked(
                    "submission_changed_during_final_send_validation"
                )
            durable_prepared_payload = durable_before_send[0].get("payload")
            if (
                type(durable_prepared_payload) is not dict
                or type(durable_prepared_payload.get("prepared_at")) is not str
            ):
                raise DispatchBlocked("submission_prepared_chronology_invalid")
            lease_state = _prepared_lease_state(
                prepared_at=durable_prepared_payload["prepared_at"],
                now=barrier_now,
                lease_seconds=self.prepared_lease_seconds,
            )
            if lease_state != "ACTIVE":
                barrier_reason = (
                    "prepared_owner_lease_expired_before_send"
                    if lease_state == "EXPIRED"
                    else "final_barrier_clock_moved_before_prepared"
                )
                try:
                    self._append(
                        attempt_id=attempt_id,
                        event_type="SubmissionBlocked",
                        version=2,
                        payload={
                            "client_order_id": client_order_id,
                            "reason": barrier_reason,
                            "owner_token": self.owner_token,
                            "owner_epoch": self.owner_epoch,
                        },
                        now=barrier_now if lease_state == "EXPIRED" else now,
                    )
                except ValueError:
                    # Another owner may have won the same version-2 race. The
                    # outer DispatchBlocked handler re-reads that durable truth.
                    pass
                raise DispatchBlocked(barrier_reason)
            try:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionSending",
                    version=2,
                    payload={
                        "client_order_id": client_order_id,
                        "owner_token": self.owner_token,
                        "owner_epoch": self.owner_epoch,
                        "reason": "final_send_barrier_passed",
                    },
                    now=barrier_now,
                )
            except ValueError as error:
                # A concurrent recovery may have terminalized the Prepared
                # attempt as zero-wire BLOCKED after its lease expired. Never
                # let a stale final_guard cross that durable fence.
                latest = self._events(attempt_id)
                if latest and latest[-1]["event_type"] == "SubmissionPrepared":
                    reason = "submission_changed_during_final_send_validation"
                    self._append(
                        attempt_id=attempt_id,
                        event_type="SubmissionBlocked",
                        version=2,
                        payload={
                            "client_order_id": client_order_id,
                            "reason": reason,
                        },
                        now=barrier_now,
                    )
                raise DispatchBlocked(
                    "submission_changed_during_final_send_validation"
                ) from error
            barrier_passed = True

        exact_response_snapshot = _snapshot_exact_transport_response
        exact_response_type = ExactJsonTransportResponse
        snapshot_getattr = getattr
        snapshot_setattr = setattr
        snapshot_type = type
        snapshot_id = id
        snapshot_frozenset = frozenset
        snapshot_delattr = delattr
        snapshot_isinstance = isinstance
        snapshot_str = str
        snapshot_int = int
        snapshot_exception = Exception
        snapshot_value_error = ValueError
        snapshot_type_error = TypeError
        snapshot_dispatch_blocked = DispatchBlocked
        snapshot_dispatch_authority_changed = _DispatchAuthorityChanged
        snapshot_tuple = tuple
        snapshot_dict = dict
        snapshot_len = len
        snapshot_module_globals = globals()
        snapshot_module_globals_get = snapshot_module_globals.get
        snapshot_module_globals_set = snapshot_module_globals.__setitem__
        snapshot_module_globals_pop = snapshot_module_globals.pop
        snapshot_builtin_missing = object()
        snapshot_postsend_global_names = (
            "_builtins",
            "type",
            "id",
            "tuple",
            "frozenset",
            "dict",
            "set",
            "len",
            "any",
            "all",
            "vars",
            "getattr",
            "setattr",
            "delattr",
            "isinstance",
            "str",
            "int",
            "bool",
            "max",
            "range",
            "enumerate",
            "Exception",
            "ValueError",
            "TypeError",
            "RuntimeError",
            "PermissionError",
            "DispatchBlocked",
            "_DispatchAuthorityChanged",
        )
        snapshot_postsend_global_state = snapshot_tuple(
            (
                name,
                snapshot_module_globals_get(
                    name,
                    snapshot_builtin_missing,
                ),
            )
            for name in snapshot_postsend_global_names
        )
        snapshot_builtin_namespace = snapshot_getattr(
            _builtins,
            "__dict__",
            None,
        )
        if snapshot_type(snapshot_builtin_namespace) is not snapshot_dict:
            raise RuntimeError(
                "post-send builtin namespace authority is unavailable"
            )
        snapshot_postsend_builtin_state = snapshot_tuple(
            snapshot_dict.items(snapshot_builtin_namespace)
        )
        if any(
            snapshot_type(name) is not snapshot_str
            for name, _value in snapshot_postsend_builtin_state
        ):
            raise RuntimeError(
                "post-send builtin namespace keys are unavailable"
            )
        snapshot_postsend_helper_names = (
            "_canonical_journal_authority_snapshot",
            "_journal_store_call",
            "_envelope",
            "_detach_submission_json",
            "submission_attempt_aggregate_id",
            "_event_id",
            "_identity_digest",
            "_instant",
            "_prepared_lease_state",
            "_validated_authority_result",
            "payload_digest",
            "canonical_json",
            "_canonical_submission_event_instant",
            "_exact_response_terminal_semantics_are_canonical",
            "_has_exact_response_markers",
            "uuid5",
            "NAMESPACE_URL",
            "sha256",
            "datetime",
            "timezone",
            "timedelta",
            "DispatchOutcome",
        )
        snapshot_postsend_helper_authority = []
        for helper_name in snapshot_postsend_helper_names:
            helper = snapshot_module_globals_get(helper_name)
            helper_kwdefaults = snapshot_getattr(
                helper,
                "__kwdefaults__",
                None,
            )
            if (
                helper_kwdefaults is not None
                and snapshot_type(helper_kwdefaults) is not snapshot_dict
            ):
                raise RuntimeError(
                    "post-send helper keyword defaults are unavailable"
                )
            helper_kwdefaults_copy = (
                snapshot_dict(helper_kwdefaults)
                if snapshot_type(helper_kwdefaults) is snapshot_dict
                else None
            )
            helper_kwdefaults_fingerprint = (
                snapshot_tuple(
                    (snapshot_id(key), snapshot_id(value))
                    for key, value in snapshot_dict.items(helper_kwdefaults)
                )
                if snapshot_type(helper_kwdefaults) is snapshot_dict
                else None
            )
            snapshot_postsend_helper_authority.append(
                (
                    helper_name,
                    helper,
                    snapshot_getattr(helper, "__code__", None),
                    snapshot_getattr(helper, "__defaults__", None),
                    helper_kwdefaults,
                    helper_kwdefaults_copy,
                    helper_kwdefaults_fingerprint,
                )
            )
        snapshot_postsend_helper_authority = snapshot_tuple(
            snapshot_postsend_helper_authority
        )
        snapshot_code = snapshot_getattr(
            exact_response_snapshot,
            "__code__",
            None,
        )
        snapshot_defaults = snapshot_getattr(
            exact_response_snapshot,
            "__defaults__",
            None,
        )
        snapshot_kwdefaults = snapshot_getattr(
            exact_response_snapshot,
            "__kwdefaults__",
            None,
        )
        if (
            snapshot_code is None
            or snapshot_type(snapshot_defaults) is not snapshot_tuple
            or snapshot_len(snapshot_defaults) != 3
        ):
            raise RuntimeError(
                "exact transport response snapshot authority is unavailable"
            )
        snapshot_dependency_codes = snapshot_tuple(
            snapshot_getattr(dependency, "__code__", None)
            for dependency in snapshot_defaults
        )
        decoder_authority = snapshot_defaults[0]
        decoder_function = snapshot_defaults[1]
        decoder_digest = snapshot_defaults[2]
        decoder_json_module = json
        decoder_json_module_type = snapshot_type(decoder_json_module)
        decoder_json_module_type_setattr = decoder_json_module_type.__setattr__
        decoder_json_namespace = vars(decoder_json_module)
        decoder_json_namespace_get = decoder_json_namespace.get
        decoder_json_namespace_set = decoder_json_namespace.__setitem__
        decoder_json_loads = decoder_json_namespace_get("loads")
        decoder_json_loads_code = snapshot_getattr(
            decoder_json_loads,
            "__code__",
            None,
        )
        decoder_json_loads_defaults = snapshot_getattr(
            decoder_json_loads,
            "__defaults__",
            None,
        )
        decoder_json_loads_kwdefaults = snapshot_getattr(
            decoder_json_loads,
            "__kwdefaults__",
            None,
        )
        if snapshot_type(decoder_json_loads_kwdefaults) is not snapshot_dict:
            raise RuntimeError(
                "exact JSON decoder default authority is unavailable"
            )
        decoder_json_loads_kwdefault_items = snapshot_tuple(
            decoder_json_loads_kwdefaults.items()
        )
        if any(
            snapshot_type(key) is not snapshot_str
            for key, _value in decoder_json_loads_kwdefault_items
        ):
            raise RuntimeError(
                "exact JSON decoder default keys are unavailable"
            )
        decoder_json_loads_kwdefault_missing = object()
        decoder_json_decoder = decoder_json_namespace_get("JSONDecoder")
        decoder_json_decoder_namespace = vars(decoder_json_decoder)
        decoder_json_decoder_namespace_get = (
            decoder_json_decoder_namespace.get
        )
        decoder_json_decoder_surface = snapshot_tuple(
            decoder_json_decoder_namespace.items()
        )
        decoder_json_decoder_expected_names = snapshot_tuple(
            name for name, _member in decoder_json_decoder_surface
        )
        decoder_json_decoder_methods = snapshot_tuple(
            (
                method_name,
                decoder_json_decoder_namespace_get(method_name),
            )
            for method_name in ("__init__", "decode", "raw_decode")
        )
        decoder_json_decoder_method_codes = snapshot_tuple(
            snapshot_getattr(method, "__code__", None)
            for _method_name, method in decoder_json_decoder_methods
        )
        if (
            any(method is None for _name, method in decoder_json_decoder_methods)
            or any(code is None for code in decoder_json_decoder_method_codes)
        ):
            raise RuntimeError(
                "exact JSON decoder method authority is unavailable"
            )
        decoder_init = decoder_json_decoder_methods[0][1]
        decoder_runtime_globals = snapshot_getattr(
            decoder_init,
            "__globals__",
            None,
        )
        if snapshot_type(decoder_runtime_globals) is not snapshot_dict:
            raise RuntimeError(
                "exact JSON decoder runtime globals are unavailable"
            )
        decoder_runtime_globals_get = decoder_runtime_globals.get
        decoder_runtime_globals_set = decoder_runtime_globals.__setitem__
        decoder_runtime_bindings = snapshot_tuple(
            (
                name,
                decoder_runtime_globals_get(name),
            )
            for name in (
                "JSONObject",
                "JSONArray",
                "scanstring",
                "scanner",
                "JSONDecodeError",
            )
        )
        if any(value is None for _name, value in decoder_runtime_bindings):
            raise RuntimeError(
                "exact JSON decoder runtime authority is unavailable"
            )
        decoder_runtime_code_bindings = snapshot_tuple(
            (
                value,
                snapshot_getattr(value, "__code__", None),
            )
            for name, value in decoder_runtime_bindings
            if name in {"JSONObject", "JSONArray", "scanstring"}
        )
        decoder_scanner_module = decoder_runtime_globals_get("scanner")
        decoder_scanner_module_type = snapshot_type(decoder_scanner_module)
        decoder_scanner_module_type_setattr = (
            decoder_scanner_module_type.__setattr__
        )
        decoder_scanner_namespace = vars(decoder_scanner_module)
        decoder_scanner_namespace_get = decoder_scanner_namespace.get
        decoder_scanner_namespace_set = decoder_scanner_namespace.__setitem__
        decoder_make_scanner = decoder_scanner_namespace_get("make_scanner")
        if decoder_make_scanner is None:
            raise RuntimeError(
                "exact JSON scanner authority is unavailable"
            )
        decoder_make_scanner_code = snapshot_getattr(
            decoder_make_scanner,
            "__code__",
            None,
        )
        decoder_json_decode_error = decoder_json_namespace_get(
            "JSONDecodeError"
        )
        decoder_depth_guard = require_provider_json_depth
        decoder_depth_guard_code = snapshot_getattr(
            decoder_depth_guard,
            "__code__",
            None,
        )
        decoder_number_parser = parse_bounded_json_number_token
        decoder_number_parser_code = snapshot_getattr(
            decoder_number_parser,
            "__code__",
            None,
        )
        decoder_integer_parser = parse_bounded_json_integer_token
        decoder_integer_parser_code = snapshot_getattr(
            decoder_integer_parser,
            "__code__",
            None,
        )
        decoder_exact_decimal_error = ExactDecimalError

        # Direct parser code/default authority is already retained above.
        # Complete the bounded dependency cut by retaining the global/builtin
        # bindings used by those roots and executable state of same-module
        # helper functions reached from them.
        decoder_dependency_function_type = snapshot_type(decoder_number_parser)
        decoder_dependency_modules = snapshot_frozenset(
            (
                snapshot_getattr(decoder_depth_guard, "__module__", None),
                snapshot_getattr(decoder_number_parser, "__module__", None),
                snapshot_getattr(decoder_integer_parser, "__module__", None),
            )
        )
        decoder_dependency_root_ids = snapshot_frozenset(
            snapshot_id(root)
            for root in (
                decoder_depth_guard,
                decoder_number_parser,
                decoder_integer_parser,
            )
        )
        decoder_dependency_missing = object()
        decoder_dependency_function_states = []
        decoder_dependency_global_bindings = []
        decoder_dependency_builtin_bindings = []
        decoder_dependency_seen_functions = set()
        decoder_dependency_seen_globals = set()
        decoder_dependency_seen_builtins = set()

        def capture_decoder_dependency(function) -> None:
            if (
                snapshot_type(function)
                is not decoder_dependency_function_type
            ):
                return
            function_identity = snapshot_id(function)
            if function_identity in decoder_dependency_seen_functions:
                return
            decoder_dependency_seen_functions.add(function_identity)

            function_code = snapshot_getattr(function, "__code__", None)
            if function_code is None:
                raise RuntimeError(
                    "exact response decoder dependency code is unavailable"
                )
            if function_identity not in decoder_dependency_root_ids:
                function_defaults = snapshot_getattr(
                    function,
                    "__defaults__",
                    None,
                )
                if (
                    function_defaults is not None
                    and snapshot_type(function_defaults) is not snapshot_tuple
                ):
                    raise RuntimeError(
                        "exact response decoder dependency defaults are unavailable"
                    )
                function_kwdefaults = snapshot_getattr(
                    function,
                    "__kwdefaults__",
                    None,
                )
                if function_kwdefaults is None:
                    function_kwdefault_items = None
                else:
                    if (
                        snapshot_type(function_kwdefaults)
                        is not snapshot_dict
                    ):
                        raise RuntimeError(
                            "exact response decoder dependency keyword defaults "
                            "are unavailable"
                        )
                    function_kwdefault_items = snapshot_tuple(
                        function_kwdefaults.items()
                    )
                    if any(
                        snapshot_type(key) is not snapshot_str
                        for key, _value in function_kwdefault_items
                    ):
                        raise RuntimeError(
                            "exact response decoder dependency keyword default "
                            "keys are unavailable"
                        )
                decoder_dependency_function_states.append(
                    (
                        function,
                        function_code,
                        function_defaults,
                        function_kwdefault_items,
                    )
                )

            function_globals = snapshot_getattr(
                function,
                "__globals__",
                None,
            )
            function_builtins = snapshot_getattr(
                function,
                "__builtins__",
                None,
            )
            if (
                snapshot_type(function_globals) is not snapshot_dict
                or snapshot_type(function_builtins) is not snapshot_dict
            ):
                raise RuntimeError(
                    "exact response decoder dependency namespace is unavailable"
                )

            for dependency_name in function_code.co_names:
                if dependency_name in function_globals:
                    binding_key = (
                        snapshot_id(function_globals),
                        dependency_name,
                    )
                    dependency = snapshot_dict.get(
                        function_globals,
                        dependency_name,
                    )
                    if (
                        binding_key
                        not in decoder_dependency_seen_globals
                    ):
                        decoder_dependency_seen_globals.add(binding_key)
                        decoder_dependency_global_bindings.append(
                            (
                                function_globals,
                                dependency_name,
                                dependency,
                            )
                        )
                    if (
                        snapshot_type(dependency)
                        is decoder_dependency_function_type
                        and snapshot_getattr(
                            dependency,
                            "__module__",
                            None,
                        )
                        in decoder_dependency_modules
                    ):
                        capture_decoder_dependency(dependency)
                    continue

                if dependency_name in function_builtins:
                    binding_key = (
                        snapshot_id(function_builtins),
                        dependency_name,
                    )
                    dependency = snapshot_dict.get(
                        function_builtins,
                        dependency_name,
                    )
                    if (
                        binding_key
                        not in decoder_dependency_seen_builtins
                    ):
                        decoder_dependency_seen_builtins.add(binding_key)
                        decoder_dependency_builtin_bindings.append(
                            (
                                function_builtins,
                                dependency_name,
                                dependency,
                            )
                        )

        for decoder_dependency_root in (
            decoder_depth_guard,
            decoder_number_parser,
            decoder_integer_parser,
        ):
            capture_decoder_dependency(decoder_dependency_root)

        decoder_dependency_function_states = snapshot_tuple(
            decoder_dependency_function_states
        )
        decoder_dependency_global_bindings = snapshot_tuple(
            decoder_dependency_global_bindings
        )
        decoder_dependency_builtin_bindings = snapshot_tuple(
            decoder_dependency_builtin_bindings
        )

        exact_response_module_bindings = (
            ("ExactJsonTransportResponse", exact_response_type),
            ("_snapshot_exact_transport_response", exact_response_snapshot),
            (
                "_require_canonical_exact_transport_response",
                decoder_authority,
            ),
            ("_decode_exact_json_bytes", decoder_function),
            ("sha256", decoder_digest),
            ("json", decoder_json_module),
            ("require_provider_json_depth", decoder_depth_guard),
            (
                "parse_bounded_json_number_token",
                decoder_number_parser,
            ),
            (
                "parse_bounded_json_integer_token",
                decoder_integer_parser,
            ),
            ("ExactDecimalError", decoder_exact_decimal_error),
        )
        exact_response_code_bindings = (
            (exact_response_snapshot, snapshot_code),
            (decoder_json_loads, decoder_json_loads_code),
            *snapshot_tuple(
                (method, code)
                for (_name, method), code in zip(
                    decoder_json_decoder_methods,
                    decoder_json_decoder_method_codes,
                )
            ),
            *snapshot_tuple(
                (dependency, code)
                for dependency, code in zip(
                    snapshot_defaults,
                    snapshot_dependency_codes,
                )
                if code is not None
            ),
            *snapshot_tuple(
                (dependency, code)
                for dependency, code in decoder_runtime_code_bindings
                if code is not None
            ),
            *(
                ((decoder_make_scanner, decoder_make_scanner_code),)
                if decoder_make_scanner_code is not None
                else ()
            ),
            (decoder_depth_guard, decoder_depth_guard_code),
            (decoder_number_parser, decoder_number_parser_code),
            (decoder_integer_parser, decoder_integer_parser_code),
        )
        exact_response_function_defaults = []
        for dependency, expected_code in exact_response_code_bindings:
            if expected_code is None or dependency is decoder_json_loads:
                continue
            dependency_defaults = snapshot_getattr(
                dependency,
                "__defaults__",
                None,
            )
            if (
                dependency_defaults is not None
                and snapshot_type(dependency_defaults) is not snapshot_tuple
            ):
                raise RuntimeError(
                    "exact response callable defaults are unavailable"
                )
            dependency_kwdefaults = snapshot_getattr(
                dependency,
                "__kwdefaults__",
                None,
            )
            if dependency_kwdefaults is None:
                dependency_kwdefault_items = None
            else:
                if snapshot_type(dependency_kwdefaults) is not snapshot_dict:
                    raise RuntimeError(
                        "exact response callable keyword defaults are unavailable"
                    )
                dependency_kwdefault_items = snapshot_tuple(
                    dependency_kwdefaults.items()
                )
                if any(
                    snapshot_type(key) is not snapshot_str
                    for key, _value in dependency_kwdefault_items
                ):
                    raise RuntimeError(
                        "exact response callable keyword default keys are unavailable"
                    )
            exact_response_function_defaults.append(
                (
                    dependency,
                    dependency_defaults,
                    dependency_kwdefault_items,
                )
            )
        exact_response_function_defaults = snapshot_tuple(
            exact_response_function_defaults
        )
        function_kwdefault_missing = object()

        def restore_exact_response_authority() -> bool:
            changed = False
            if snapshot_type(decoder_json_module) is not decoder_json_module_type:
                decoder_json_module_type_setattr(
                    decoder_json_module,
                    "__class__",
                    decoder_json_module_type,
                )
                changed = True
            if (
                snapshot_type(decoder_scanner_module)
                is not decoder_scanner_module_type
            ):
                decoder_scanner_module_type_setattr(
                    decoder_scanner_module,
                    "__class__",
                    decoder_scanner_module_type,
                )
                changed = True
            for name, expected in exact_response_module_bindings:
                if snapshot_module_globals_get(name) is not expected:
                    snapshot_module_globals_set(name, expected)
                    changed = True

            if (
                snapshot_getattr(
                    exact_response_snapshot,
                    "__defaults__",
                    None,
                )
                is not snapshot_defaults
            ):
                snapshot_setattr(
                    exact_response_snapshot,
                    "__defaults__",
                    snapshot_defaults,
                )
                changed = True
            if (
                snapshot_getattr(
                    exact_response_snapshot,
                    "__kwdefaults__",
                    None,
                )
                is not snapshot_kwdefaults
            ):
                snapshot_setattr(
                    exact_response_snapshot,
                    "__kwdefaults__",
                    snapshot_kwdefaults,
                )
                changed = True
            for dependency, expected_code in exact_response_code_bindings:
                if expected_code is None:
                    continue
                if (
                    snapshot_getattr(dependency, "__code__", None)
                    is not expected_code
                ):
                    snapshot_setattr(
                        dependency,
                        "__code__",
                        expected_code,
                    )
                    changed = True
            for (
                dependency,
                expected_defaults,
                expected_kwdefault_items,
            ) in exact_response_function_defaults:
                if (
                    snapshot_getattr(
                        dependency,
                        "__defaults__",
                        None,
                    )
                    is not expected_defaults
                ):
                    snapshot_setattr(
                        dependency,
                        "__defaults__",
                        expected_defaults,
                    )
                    changed = True
                current_kwdefaults = snapshot_getattr(
                    dependency,
                    "__kwdefaults__",
                    None,
                )
                if expected_kwdefault_items is None:
                    if current_kwdefaults is not None:
                        snapshot_setattr(
                            dependency,
                            "__kwdefaults__",
                            None,
                        )
                        changed = True
                    continue
                callable_kwdefaults_changed = (
                    snapshot_type(current_kwdefaults) is not snapshot_dict
                    or snapshot_len(current_kwdefaults)
                    != snapshot_len(expected_kwdefault_items)
                )
                if not callable_kwdefaults_changed:
                    for current_key in current_kwdefaults:
                        if snapshot_type(current_key) is not snapshot_str:
                            callable_kwdefaults_changed = True
                            break
                if not callable_kwdefaults_changed:
                    current_kwdefault_get = current_kwdefaults.get
                    for key, expected_value in expected_kwdefault_items:
                        if (
                            current_kwdefault_get(
                                key,
                                function_kwdefault_missing,
                            )
                            is not expected_value
                        ):
                            callable_kwdefaults_changed = True
                            break
                if callable_kwdefaults_changed:
                    snapshot_setattr(
                        dependency,
                        "__kwdefaults__",
                        snapshot_dict(expected_kwdefault_items),
                    )
                    changed = True

            for (
                dependency,
                expected_code,
                expected_defaults,
                expected_kwdefault_items,
            ) in decoder_dependency_function_states:
                if (
                    snapshot_getattr(dependency, "__code__", None)
                    is not expected_code
                ):
                    snapshot_setattr(
                        dependency,
                        "__code__",
                        expected_code,
                    )
                    changed = True
                if (
                    snapshot_getattr(dependency, "__defaults__", None)
                    is not expected_defaults
                ):
                    snapshot_setattr(
                        dependency,
                        "__defaults__",
                        expected_defaults,
                    )
                    changed = True
                current_kwdefaults = snapshot_getattr(
                    dependency,
                    "__kwdefaults__",
                    None,
                )
                if expected_kwdefault_items is None:
                    if current_kwdefaults is not None:
                        snapshot_setattr(
                            dependency,
                            "__kwdefaults__",
                            None,
                        )
                        changed = True
                else:
                    kwdefaults_changed = (
                        snapshot_type(current_kwdefaults) is not snapshot_dict
                        or snapshot_len(current_kwdefaults)
                        != snapshot_len(expected_kwdefault_items)
                    )
                    if not kwdefaults_changed:
                        for current_key in current_kwdefaults:
                            if snapshot_type(current_key) is not snapshot_str:
                                kwdefaults_changed = True
                                break
                    if not kwdefaults_changed:
                        current_kwdefault_get = current_kwdefaults.get
                        for key, expected_value in expected_kwdefault_items:
                            if (
                                current_kwdefault_get(
                                    key,
                                    decoder_dependency_missing,
                                )
                                is not expected_value
                            ):
                                kwdefaults_changed = True
                                break
                    if kwdefaults_changed:
                        snapshot_setattr(
                            dependency,
                            "__kwdefaults__",
                            snapshot_dict(expected_kwdefault_items),
                        )
                        changed = True

            for namespace, name, expected in (
                decoder_dependency_global_bindings
            ):
                if (
                    snapshot_dict.get(
                        namespace,
                        name,
                        decoder_dependency_missing,
                    )
                    is not expected
                ):
                    snapshot_dict.__setitem__(namespace, name, expected)
                    changed = True
            for namespace, name, expected in (
                decoder_dependency_builtin_bindings
            ):
                if (
                    snapshot_dict.get(
                        namespace,
                        name,
                        decoder_dependency_missing,
                    )
                    is not expected
                ):
                    snapshot_dict.__setitem__(namespace, name, expected)
                    changed = True

            if decoder_json_namespace_get("loads") is not decoder_json_loads:
                decoder_json_namespace_set("loads", decoder_json_loads)
                changed = True
            if (
                snapshot_getattr(
                    decoder_json_loads,
                    "__defaults__",
                    None,
                )
                is not decoder_json_loads_defaults
            ):
                snapshot_setattr(
                    decoder_json_loads,
                    "__defaults__",
                    decoder_json_loads_defaults,
                )
                changed = True
            current_json_loads_kwdefaults = snapshot_getattr(
                decoder_json_loads,
                "__kwdefaults__",
                None,
            )
            kwdefaults_changed = (
                snapshot_type(current_json_loads_kwdefaults) is not snapshot_dict
                or snapshot_len(current_json_loads_kwdefaults)
                != snapshot_len(decoder_json_loads_kwdefault_items)
            )
            if not kwdefaults_changed:
                for current_key in current_json_loads_kwdefaults:
                    if snapshot_type(current_key) is not snapshot_str:
                        kwdefaults_changed = True
                        break
            if not kwdefaults_changed:
                current_kwdefault_get = current_json_loads_kwdefaults.get
                for key, expected_value in decoder_json_loads_kwdefault_items:
                    if (
                        current_kwdefault_get(
                            key,
                            decoder_json_loads_kwdefault_missing,
                        )
                        is not expected_value
                    ):
                        kwdefaults_changed = True
                        break
            if kwdefaults_changed:
                snapshot_setattr(
                    decoder_json_loads,
                    "__kwdefaults__",
                    snapshot_dict(decoder_json_loads_kwdefault_items),
                )
                changed = True
            if decoder_json_namespace_get("JSONDecoder") is not decoder_json_decoder:
                decoder_json_namespace_set("JSONDecoder", decoder_json_decoder)
                changed = True
            for name, expected in decoder_runtime_bindings:
                if decoder_runtime_globals_get(name) is not expected:
                    decoder_runtime_globals_set(name, expected)
                    changed = True
            if (
                decoder_scanner_namespace_get("make_scanner")
                is not decoder_make_scanner
            ):
                decoder_scanner_namespace_set(
                    "make_scanner",
                    decoder_make_scanner,
                )
                changed = True
            for method_name, expected_method in decoder_json_decoder_methods:
                if (
                    decoder_json_decoder_namespace_get(method_name)
                    is not expected_method
                ):
                    snapshot_setattr(
                        decoder_json_decoder,
                        method_name,
                        expected_method,
                    )
                    changed = True
            current_decoder_names = snapshot_tuple(
                decoder_json_decoder.__dict__
            )
            if current_decoder_names != decoder_json_decoder_expected_names:
                for name in current_decoder_names:
                    if name not in decoder_json_decoder_expected_names:
                        snapshot_delattr(decoder_json_decoder, name)
                        changed = True
            for name, expected_member in decoder_json_decoder_surface:
                if (
                    decoder_json_decoder.__dict__.get(name)
                    is not expected_member
                ):
                    snapshot_setattr(
                        decoder_json_decoder,
                        name,
                        expected_member,
                    )
                    changed = True
            if (
                decoder_json_namespace_get("JSONDecodeError")
                is not decoder_json_decode_error
            ):
                decoder_json_namespace_set(
                    "JSONDecodeError",
                    decoder_json_decode_error,
                )
                changed = True
            return changed

        def restore_postsend_helper_authority() -> bool:
            changed = False
            for (
                name,
                expected,
                expected_code,
                expected_defaults,
                expected_kwdefaults,
                expected_kwdefaults_copy,
                expected_kwdefaults_fingerprint,
            ) in snapshot_postsend_helper_authority:
                if snapshot_module_globals_get(name) is not expected:
                    snapshot_module_globals_set(name, expected)
                    changed = True
                if (
                    expected_code is not None
                    and snapshot_getattr(expected, "__code__", None)
                    is not expected_code
                ):
                    snapshot_setattr(expected, "__code__", expected_code)
                    changed = True
                if (
                    snapshot_getattr(expected, "__defaults__", None)
                    is not expected_defaults
                ):
                    snapshot_setattr(
                        expected,
                        "__defaults__",
                        expected_defaults,
                    )
                    changed = True
                if (
                    snapshot_getattr(expected, "__kwdefaults__", None)
                    is not expected_kwdefaults
                ):
                    snapshot_setattr(
                        expected,
                        "__kwdefaults__",
                        expected_kwdefaults,
                    )
                    changed = True
                if (
                    expected_kwdefaults_fingerprint is not None
                    and snapshot_type(expected_kwdefaults) is snapshot_dict
                ):
                    current_fingerprint = snapshot_tuple(
                        (snapshot_id(key), snapshot_id(value))
                        for key, value in snapshot_dict.items(
                            expected_kwdefaults
                        )
                    )
                    if current_fingerprint != expected_kwdefaults_fingerprint:
                        snapshot_dict.clear(expected_kwdefaults)
                        snapshot_dict.update(
                            expected_kwdefaults,
                            expected_kwdefaults_copy,
                        )
                        changed = True
            return changed

        def restore_postsend_builtin_namespace() -> bool:
            changed = False
            for name, expected in snapshot_postsend_builtin_state:
                current = snapshot_dict.get(
                    snapshot_builtin_namespace,
                    name,
                    snapshot_builtin_missing,
                )
                if current is not expected:
                    snapshot_dict.__setitem__(
                        snapshot_builtin_namespace,
                        name,
                        expected,
                    )
                    changed = True
            return changed

        def restore_postsend_builtin_globals() -> bool:
            changed = False
            for name, expected in snapshot_postsend_global_state:
                current = snapshot_module_globals_get(
                    name,
                    snapshot_builtin_missing,
                )
                if expected is snapshot_builtin_missing:
                    if current is not snapshot_builtin_missing:
                        snapshot_module_globals_pop(name, None)
                        changed = True
                elif current is not expected:
                    snapshot_module_globals_set(name, expected)
                    changed = True
            return changed

        def require_transport_module_authority() -> None:
            builtin_namespace_changed = restore_postsend_builtin_namespace()
            helper_changed = restore_postsend_helper_authority()
            restore_postsend_builtin_globals()
            if builtin_namespace_changed or helper_changed:
                raise snapshot_dispatch_authority_changed(
                    "dispatcher module authority changed before final send barrier"
                )

        postsend_builtin_authority_changed = False
        postsend_helper_authority_changed = False
        exact_response_authority_changed = False
        try:
            try:
                response = transport_send(
                    client_order_id,
                    request_frozen,
                    final_guard,
                )
            finally:
                postsend_builtin_authority_changed = (
                    restore_postsend_builtin_namespace()
                    or postsend_builtin_authority_changed
                )
                postsend_helper_authority_changed = (
                    restore_postsend_helper_authority()
                    or postsend_helper_authority_changed
                )
                exact_response_authority_changed = (
                    restore_exact_response_authority()
                    or exact_response_authority_changed
                )
                restore_postsend_builtin_globals()
            if (
                postsend_builtin_authority_changed
                or postsend_helper_authority_changed
            ):
                if barrier_passed:
                    return authority_change_after_send_outcome()
                raise snapshot_dispatch_authority_changed(
                    "dispatcher module authority changed during dispatch"
                )
            try:
                require_dispatch_call_authority()
            except snapshot_dispatch_authority_changed:
                if barrier_passed:
                    return authority_change_after_send_outcome()
                raise
        except snapshot_dispatch_authority_changed:
            # The authority helper restores the invocation-selected cut before
            # raising. Once Sending is durable, preserve worst-case exposure as
            # UNKNOWN on that original journal rather than leaking a retryable
            # callback/transport error.
            if barrier_passed:
                return authority_change_after_send_outcome()
            raise
        except snapshot_dispatch_blocked as error:
            if (
                postsend_builtin_authority_changed
                or postsend_helper_authority_changed
            ):
                if barrier_passed:
                    return authority_change_after_send_outcome()
                raise snapshot_dispatch_authority_changed(
                    "dispatcher module authority changed during dispatch"
                ) from error
            try:
                require_dispatch_call_authority()
            except snapshot_dispatch_authority_changed:
                if barrier_passed:
                    return authority_change_after_send_outcome()
                raise
            events = self._events(attempt_id)
            if events:
                last = events[-1]
                if last["event_type"] == "SubmissionSending":
                    return DispatchOutcome(
                        "UNKNOWN",
                        client_order_id,
                        None,
                        "concurrent_send_barrier_already_committed",
                    )
                if last["event_type"] in {
                    "SubmissionSent",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                }:
                    return self._terminal_outcome_from_existing_history(
                        events=events,
                        attempt_id=attempt_id,
                        client_order_id=client_order_id,
                        expected_prepared=expected_prepared,
                    )
            return DispatchOutcome("BLOCKED", client_order_id, None, snapshot_str(error))
        except snapshot_exception as error:
            if (
                postsend_builtin_authority_changed
                or postsend_helper_authority_changed
            ):
                if barrier_passed:
                    return authority_change_after_send_outcome()
                raise snapshot_dispatch_authority_changed(
                    "dispatcher module authority changed during dispatch"
                ) from error
            try:
                require_dispatch_call_authority()
            except snapshot_dispatch_authority_changed:
                if barrier_passed:
                    return authority_change_after_send_outcome()
                raise
            events = self._events(attempt_id)
            if not events:
                return DispatchOutcome(
                    "UNKNOWN",
                    client_order_id,
                    None,
                    "durable_submission_history_invalid",
                )
            last = events[-1]
            if last["event_type"] == "SubmissionSending":
                if not self._existing_history_is_canonical(
                    events=events,
                    attempt_id=attempt_id,
                    client_order_id=client_order_id,
                    expected_prepared=expected_prepared,
                ):
                    return DispatchOutcome(
                        "UNKNOWN",
                        client_order_id,
                        None,
                        "durable_submission_history_invalid",
                    )
                try:
                    self._append(
                        attempt_id=attempt_id,
                        event_type="SubmissionUnknown",
                        version=3,
                        payload={
                            "client_order_id": client_order_id,
                            "reason": f"transport_exception_after_send_barrier:{snapshot_type(error).__name__}",
                        },
                        now=barrier_now,
                    )
                except snapshot_value_error:
                    current = self._events(attempt_id)
                    return self._terminal_outcome_from_existing_history(
                        events=current,
                        attempt_id=attempt_id,
                        client_order_id=client_order_id,
                        expected_prepared=expected_prepared,
                    )
                return DispatchOutcome("UNKNOWN", client_order_id, None, "transport_result_ambiguous")
            if (
                barrier_passed
                and last["event_type"] in {
                    "SubmissionSent",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                }
            ):
                return self._terminal_outcome_from_existing_history(
                    events=events,
                    attempt_id=attempt_id,
                    client_order_id=client_order_id,
                    expected_prepared=expected_prepared,
                )
            if barrier_passed:
                return DispatchOutcome(
                    "UNKNOWN",
                    client_order_id,
                    None,
                    "durable_submission_history_invalid",
                )
            if not guard_called:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionBlocked",
                    version=2,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": f"transport_failed_before_final_guard:{snapshot_type(error).__name__}",
                    },
                    now=now,
                )
                return DispatchOutcome("BLOCKED", client_order_id, None, "transport_failed_before_send")
            if not barrier_passed:
                # The provider wrapper invoked a guard that rejected, but did
                # not propagate DispatchBlocked. Once it masks that rejection
                # and raises something else, we can no longer prove that it
                # refrained from an outbound side effect after the guard.
                # Preserve worst-case exposure and force reconciliation.
                next_version = snapshot_int(last["aggregate_version"]) + 1
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=next_version,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": (
                            "provider_wrapper_masked_final_guard_failure:"
                            + snapshot_type(error).__name__
                        ),
                    },
                    now=barrier_now,
                )
                return DispatchOutcome(
                    "UNKNOWN",
                    client_order_id,
                    None,
                    "provider_guard_contract_violation",
                )
            raise

        if not guard_called:
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionUnknown",
                version=2,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "provider_wrapper_returned_without_final_guard",
                },
                now=now,
            )
            return DispatchOutcome("UNKNOWN", client_order_id, None, "provider_guard_contract_violation")

        if not barrier_passed:
            # A wrapper that catches DispatchBlocked (or any final-guard
            # failure) and then returns has violated the only safe outbound
            # contract. We cannot prove that it refrained from sending after
            # swallowing the barrier, so preserve worst-case exposure and force
            # reconciliation instead of fabricating SENT or safe-to-retry.
            events = self._events(attempt_id)
            last = events[-1]
            next_version = snapshot_int(last["aggregate_version"]) + 1
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionUnknown",
                version=next_version,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "provider_wrapper_swallowed_final_guard_failure",
                },
                now=barrier_now,
            )
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "provider_guard_contract_violation",
            )

        post_transport_events = self._events(attempt_id)
        if not self._existing_history_is_canonical(
            events=post_transport_events,
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            expected_prepared=expected_prepared,
        ):
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "durable_submission_history_invalid",
            )
        post_transport_last = post_transport_events[-1]
        if post_transport_last["event_type"] in {
            "SubmissionSent",
            "SubmissionBlocked",
            "SubmissionUnknown",
        }:
            return self._terminal_outcome_from_existing_history(
                events=post_transport_events,
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                expected_prepared=expected_prepared,
            )
        if post_transport_last["event_type"] != "SubmissionSending":
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "durable_submission_history_invalid",
            )

        terminal_requires_reconciliation = False
        terminal_reason = "sent_confirmed"
        verified_exact_sent_payload = None
        try:
            if snapshot_type(response) is exact_response_type:
                # Revalidate raw exact state now. Frozen dataclass construction
                # is not sufficient authority because object.__setattr__ can
                # alter fields after __post_init__ and before transport returns.
                if (
                    exact_response_authority_changed
                    or restore_exact_response_authority()
                ):
                    raise snapshot_value_error(
                        "exact transport response authority changed after send"
                    )
                (
                    response_text,
                    response_encoding,
                    response_sha256,
                    outcome_response,
                    response_http_status,
                    terminal_requires_reconciliation,
                    response_ambiguity_reason,
                ) = exact_response_snapshot(response)
                sent_payload = {
                    "client_order_id": client_order_id,
                    "response_text": response_text,
                    "response_sha256": response_sha256,
                    "response_encoding": response_encoding,
                }
                if response_http_status is not None:
                    sent_payload["http_status"] = response_http_status
                if terminal_requires_reconciliation:
                    terminal_reason = (
                        response_ambiguity_reason
                        or "provider_response_ambiguous"
                    )
                    sent_payload["reason"] = terminal_reason
                    sent_payload["retry_disposition"] = "RECONCILE_FIRST"
                # From this point forward the exact response has crossed every
                # response-authority/decoder fence. If only the terminal journal
                # append fails, preserve these already-verified bytes as
                # reconciliation evidence instead of degrading to marker-free
                # UNKNOWN. This variable is deliberately assigned only after
                # exact snapshot construction succeeds.
                verified_exact_sent_payload = sent_payload
            elif snapshot_isinstance(response, exact_response_type):
                # Caller-polymorphic post-SEND response getters are not evidence.
                # A durable UNKNOWN retains the no-blind-retry property.
                raise snapshot_type_error("exact provider response subtype is forbidden")
            else:
                # Legacy provider wrappers may still return decoded JSON rather
                # than ExactJsonTransportResponse. Detach that graph before
                # payload hashing/persistence so dict/list subclasses or nested
                # executable objects cannot run callbacks after the wire send.
                legacy_response = _detach_submission_json(response)
                sent_payload = {
                    "client_order_id": client_order_id,
                    "response": legacy_response,
                }
                outcome_response = legacy_response
            self._append(
                attempt_id=attempt_id,
                event_type=(
                    "SubmissionUnknown"
                    if terminal_requires_reconciliation
                    else "SubmissionSent"
                ),
                version=3,
                payload=sent_payload,
                now=barrier_now,
            )
        except snapshot_exception as persistence_error:
            # The outbound request has already crossed the final barrier.
            # Never make this state safe to retry merely because the provider
            # response could not be journaled.
            try:
                fallback_reason = (
                    "sent_response_persistence_failed:"
                    + snapshot_type(persistence_error).__name__
                )
                if verified_exact_sent_payload is not None:
                    fallback_payload = snapshot_dict(
                        verified_exact_sent_payload
                    )
                    if not terminal_requires_reconciliation:
                        fallback_payload["reason"] = fallback_reason
                        fallback_payload["retry_disposition"] = (
                            "RECONCILE_FIRST"
                        )
                else:
                    fallback_payload = {
                        "client_order_id": client_order_id,
                        "reason": fallback_reason,
                    }
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=3,
                    payload=fallback_payload,
                    now=barrier_now,
                )
            except snapshot_exception:
                current = self._events(attempt_id)
                if current and current[-1]["event_type"] in {
                    "SubmissionSent",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                }:
                    return self._terminal_outcome_from_existing_history(
                        events=current,
                        attempt_id=attempt_id,
                        client_order_id=client_order_id,
                        expected_prepared=expected_prepared,
                    )
                # A durable SubmissionSending row already exists. Recovery will
                # convert that state to UNKNOWN without another outbound send.
                raise persistence_error
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "sent_response_persistence_failed",
            )
        terminal_events = self._events(attempt_id)
        if (
            not self._existing_history_is_canonical(
                events=terminal_events,
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                expected_prepared=expected_prepared,
            )
            or terminal_events[-1]["event_type"]
            not in {"SubmissionSent", "SubmissionUnknown"}
        ):
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "durable_submission_history_invalid",
            )
        return self._terminal_outcome_from_existing_history(
            events=terminal_events,
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            expected_prepared=expected_prepared,
        )
