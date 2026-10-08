"""Durable guarded submission attempts for the AutoTrade MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
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

# Journal CAS primitives are the sole atomic send+intent binding authority.
_CANONICAL_JOURNAL_CURRENT_SEQUENCE = JournalStore.current_journal_sequence
_CANONICAL_JOURNAL_COMMIT_COMMAND = JournalStore.commit_command


AuthorityCheck = Callable[[str, str], tuple[bool, str]]
SenderCheck = Callable[[str, int], None]
TransportSend = Callable[[str, Mapping[str, Any], Callable[[], None]], Any]

_SUBMISSION_RESPONSE_BINDING_TOKEN = object()
_EXACT_RESPONSE_MARKERS = frozenset(
    {"response_encoding", "response_text", "response_sha256"}
)


def _has_exact_response_markers(payload: Mapping[str, Any]) -> bool:
    return any(marker in payload for marker in _EXACT_RESPONSE_MARKERS)


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
        if self.http_status is not None and (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ValueError("http_status must be an integer 100..599 when provided")
        if type(self.requires_reconciliation) is not bool:
            raise TypeError("requires_reconciliation must be boolean")
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
        elif self.ambiguity_reason is not None:
            raise ValueError(
                "ambiguity_reason is only valid when reconciliation is required"
            )
        # Post-SEND ambiguous HTTP can be HTML, binary or empty. Preserve the
        # bounded *exact bytes* for durable UNKNOWN, never mint JSON authority.
        # 2xx/definitive responses retain the strict JSON contract.
        if self.requires_reconciliation and (
            self.http_status is None or not 200 <= self.http_status <= 299
        ):
            require_provider_response_bytes(
                raw, max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES, allow_empty=True,
            )
        else:
            _decode_exact_json_bytes(raw)
        _seal_exact_transport_response(self)

    @property
    def response_encoding(self) -> str:
        raw, status, requires_reconciliation, _ = (
            _require_canonical_exact_transport_response(self)
        )
        if requires_reconciliation and (status is None or not 200 <= status <= 299):
            try:
                _decode_exact_json_bytes(raw)
            except ValueError:
                if not raw:
                    return "hex"
                try:
                    raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    return "hex"
                return "utf-8-opaque"
        return "utf-8-json"

    @property
    def response_text(self) -> str:
        raw, _, _, _ = _require_canonical_exact_transport_response(self)
        return raw.hex() if self.response_encoding == "hex" else raw.decode("utf-8")

    @property
    def response_sha256(self) -> str:
        raw, _, _, _ = _require_canonical_exact_transport_response(self)
        return "sha256:" + sha256(raw).hexdigest()

    @property
    def payload(self) -> Any:
        raw, _, _, _ = _require_canonical_exact_transport_response(self)
        if self.response_encoding != "utf-8-json":
            raise ValueError("no JSON payload is available for opaque provider response")
        return _decode_exact_json_bytes(raw)


# Issued transport response state is held separately from the mutable instance.
# Frozen dataclasses alone do not resist object.__setattr__ after the wire send.
_EXACT_RESPONSE_ORIGINAL_SHA256 = sha256
_EXACT_RESPONSE_ORIGINAL_DECODE = _decode_exact_json_bytes
_exact_response_issued: dict[int, tuple[object, tuple[object, ...]]] = {}


def _seal_exact_transport_response(value: ExactJsonTransportResponse) -> None:
    if type(value) is not ExactJsonTransportResponse:
        raise ValueError("exact transport response authority is unavailable")
    record = vars(value)
    if frozenset(record) != frozenset(
        ("response_bytes", "http_status", "requires_reconciliation", "ambiguity_reason")
    ):
        raise ValueError("exact transport response authority is unavailable")
    for key, (ref, _) in tuple(_exact_response_issued.items()):
        if ref() is None:
            _exact_response_issued.pop(key, None)
    identity = id(value)
    if identity in _exact_response_issued and _exact_response_issued[identity][0]() is not None:
        raise ValueError("exact transport response authority is unavailable")
    _exact_response_issued[identity] = (
        weakref_ref(value),
        (record["response_bytes"], record["http_status"],
         record["requires_reconciliation"], record["ambiguity_reason"]),
    )


def _require_canonical_exact_transport_response(
    value: ExactJsonTransportResponse,
) -> tuple[bytes, int | None, bool, str | None]:
    if (sha256 is not _EXACT_RESPONSE_ORIGINAL_SHA256
            or _decode_exact_json_bytes is not _EXACT_RESPONSE_ORIGINAL_DECODE
            or type(value) is not ExactJsonTransportResponse):
        raise ValueError("exact transport response authority is unavailable")
    registered = _exact_response_issued.get(id(value))
    if registered is None or registered[0]() is not value:
        raise ValueError("exact transport response authority is unavailable")
    state = vars(value)
    if frozenset(state) != frozenset(
        ("response_bytes", "http_status", "requires_reconciliation", "ambiguity_reason")
    ):
        raise ValueError("exact transport response authority is unavailable")
    current = (state["response_bytes"], state["http_status"],
               state["requires_reconciliation"], state["ambiguity_reason"])
    expected = registered[1]
    if any(type(actual) is not type(wanted) or actual != wanted
           for actual, wanted in zip(current, expected)):
        raise ValueError("exact transport response authority is unavailable")
    return expected


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
    response_encoding: str
    terminal_state: str
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
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_hash) is None:
            raise ValueError("request_hash must be a canonical SHA-256 digest")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.submission_scope_hash) is None:
            raise ValueError(
                "submission_scope_hash must be a canonical SHA-256 digest"
            )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.response_sha256) is None:
            raise ValueError("response_sha256 must be a canonical SHA-256 digest")
        if type(self.response_encoding) is not str or self.response_encoding not in {
            "utf-8-json", "hex", "utf-8-opaque",
        }:
            raise ValueError("durable provider response encoding is invalid")
        if type(self.response_bytes) is not bytes:
            raise ValueError("response_bytes must be exact bytes")
        if self.response_encoding == "utf-8-json":
            _decode_exact_json_bytes(self.response_bytes)
        else:
            if type(self.terminal_state) is not str or self.terminal_state != "UNKNOWN":
                raise ValueError("opaque response cannot establish definitive SENT")
            if self.http_status is not None and (
                type(self.http_status) is not int
                or 200 <= self.http_status <= 299
            ):
                raise ValueError("opaque HTTP success cannot establish provider state")
            require_provider_response_bytes(
                self.response_bytes,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                allow_empty=True,
            )
        if (
            "sha256:" + sha256(self.response_bytes).hexdigest()
            != self.response_sha256
        ):
            raise ValueError("durable provider response digest mismatch")
        if type(self.terminal_state) is not str or self.terminal_state not in {"SENT", "UNKNOWN"}:
            raise ValueError("durable submission terminal state is invalid")
        if self.terminal_state == "UNKNOWN":
            if (type(self.ambiguity_reason) is not str or not self.ambiguity_reason.strip()
                    or self.retry_disposition != "RECONCILE_FIRST"):
                raise ValueError("durable UNKNOWN requires reconciliation-first reason")
        elif self.ambiguity_reason is not None or self.retry_disposition is not None:
            raise ValueError("definitive SENT cannot carry UNKNOWN disposition")
        if self.http_status is not None and (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ValueError("durable provider HTTP status must be an integer 100..599")
        environment = self.environment.upper()
        if environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("invalid durable submission environment")
        object.__setattr__(self, "environment", environment)
        if not isinstance(self.submission_scope, Mapping):
            raise TypeError("submission_scope must be a mapping")
        canonical_scope = json.loads(canonical_json(dict(self.submission_scope)))
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
        for value, name in (
            (self.prepared_at, "prepared_at"),
            (self.sent_at, "sent_at"),
        ):
            point = _instant(value)
            canonical = point.isoformat().replace("+00:00", "Z")
            if canonical != value:
                raise ValueError(f"{name} must be canonical UTC text")

    @property
    def payload(self) -> Any:
        if self.response_encoding != "utf-8-json":
            raise ValueError("no JSON payload is available for opaque provider response")
        return _freeze_json(_decode_exact_json_bytes(self.response_bytes))


def _freeze_json(value: Any) -> Any:
    """Recursively freeze a canonical JSON value before it reaches transport."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


class DispatchBlocked(RuntimeError):
    """Raised inside a provider wrapper when the final send barrier rejects."""


def _validated_authority_result(result: Any) -> tuple[bool, str]:
    """Fail closed unless authority returns the exact typed decision contract."""
    if not isinstance(result, tuple) or len(result) != 2:
        return False, "authority_check_invalid_result"
    allowed, reason = result
    if not isinstance(allowed, bool):
        return False, "authority_check_invalid_allowed"
    if not isinstance(reason, str) or not reason.strip():
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


def submission_intent_aggregate_id(
    *,
    provider: str,
    environment: str,
    account_id: str,
    intent_id: str,
) -> str:
    """Return one durable financial-send identity for one economic intent."""

    if type(provider) is not str or not provider.strip():
        raise ValueError("provider is required")
    normalized_environment = (
        environment.strip().upper() if type(environment) is str else ""
    )
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    if type(account_id) is not str or not account_id.strip():
        raise ValueError("account_id is required")
    if type(intent_id) is not str or not intent_id.strip():
        raise ValueError("intent_id is required")
    return "submission-intent:" + _identity_digest(
        provider.strip().lower(),
        normalized_environment,
        account_id.strip(),
        intent_id.strip(),
    )


def _legacy_submission_intent_attempt(
    store: JournalStore,
    *,
    provider: str,
    environment: str,
    account_id: str,
    intent_id: str,
    intent_hash: str,
    request_hash: str,
    client_order_id: str,
    submission_scope_hash: str,
) -> str | None:
    """Resolve pre-fence durable intent history without trusting provider dedupe.

    Older journal generations can contain submission attempts created before the
    submission_intent aggregate existed. A fresh attempt for that same economic
    intent must not cross the provider-send boundary merely because the upgrade
    introduced a new local attempt_id.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in JournalStore.load_events_by_aggregate_type(
        store, "submission_attempt"
    ):
        aggregate_id = event.get("aggregate_id")
        if not isinstance(aggregate_id, str) or not aggregate_id:
            raise RuntimeError("durable submission attempt aggregate is invalid")
        grouped.setdefault(aggregate_id, []).append(event)

    expected = {
        "provider": provider,
        "environment": environment,
        "account_id": account_id,
        "intent_id": intent_id,
        "intent_hash": intent_hash,
        "request_hash": request_hash,
        "client_order_id": client_order_id,
        "submission_scope_hash": submission_scope_hash,
    }
    candidates: list[str] = []
    for events in grouped.values():
        first = events[0]
        if first.get("event_type") != "SubmissionPrepared":
            raise RuntimeError("durable submission attempt does not start prepared")
        payload = first.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("durable submission prepared payload is invalid")
        historical_provider = payload.get("provider")
        if (
            not isinstance(historical_provider, str)
            or historical_provider.strip().lower() != provider.strip().lower()
            or payload.get("environment") != environment
            or payload.get("account_id") != account_id
            or payload.get("intent_id") != intent_id
        ):
            continue
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError(
                "intent_id conflicts with historical submission content"
            )
        attempt_id = payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise RuntimeError("historical submission attempt identity is invalid")
        last_type = events[-1].get("event_type")
        if last_type == "SubmissionBlocked":
            continue
        if last_type not in {
            "SubmissionPrepared",
            "SubmissionSending",
            "SubmissionSent",
            "SubmissionUnknown",
        }:
            raise RuntimeError(
                "historical submission attempt has unsupported terminal state"
            )
        candidates.append(attempt_id)

    unique = sorted(set(candidates))
    if len(unique) > 1:
        raise RuntimeError(
            "multiple durable non-blocked submission attempts exist for one economic intent"
        )
    return unique[0] if unique else None


def _canonical_journal_authority_snapshot(
    store: JournalStore,
) -> tuple[object, object]:
    """Validate the narrow canonical JournalStore instance/generation seam.

    JournalStore currently has exactly two initialized instance fields. Any
    additional per-instance attribute can shadow an authority-bearing class
    method (load_events/append_event/_connect/etc.). Reject that entire class
    of caller mutation instead of maintaining an open-ended method blacklist.
    The returned path + physical store identity let long-lived dispatchers
    detect replacement of the selected backing generation.
    """

    if type(store) is not JournalStore:
        raise TypeError("store must be the canonical JournalStore")
    state = vars(store)
    class_owned_names = {
        name
        for base in JournalStore.__mro__
        for name in base.__dict__
    }
    if class_owned_names.intersection(state):
        raise TypeError("canonical JournalStore instance state is shadowed")
    if "path" not in state or "_store_identity" not in state:
        raise TypeError("canonical JournalStore backing state is unavailable")
    path = state["path"]
    identity = JournalStore.store_identity.__get__(store, JournalStore)
    if getattr(identity, "canonical_path", None) != str(path):
        raise PermissionError("canonical JournalStore backing identity changed")
    return path, identity


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
    events = JournalStore.load_events(store, "submission_attempt", aggregate_id)
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
    prepared, sending, sent = events
    payload = prepared.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("durable SubmissionPrepared payload is invalid")
    sending_payload = sending.get("payload")
    if not isinstance(sending_payload, dict):
        raise ValueError("durable SubmissionSending payload is invalid")
    sent_payload = sent.get("payload")
    if not isinstance(sent_payload, dict):
        raise ValueError("durable terminal submission payload is invalid")
    # A response can only be attributed to the economic intent that crossed
    # the durable send barrier. Malformed/mismatched historical rows fail
    # closed; they must not mint a valid exact-response binding.
    client_order_id = payload.get("client_order_id")
    if (
        type(client_order_id) is not str
        or not client_order_id
        or sending_payload.get("client_order_id") != client_order_id
        or sent_payload.get("client_order_id") != client_order_id
    ):
        raise ValueError("client_order_id continuity mismatch")
    response_text = sent_payload.get("response_text")
    response_sha256 = sent_payload.get("response_sha256")
    response_encoding = sent_payload.get("response_encoding")
    if (
        type(response_encoding) is not str
        or response_encoding not in {"utf-8-json", "hex", "utf-8-opaque"}
        or type(response_text) is not str
        or (response_encoding != "hex" and not response_text)
        or type(response_sha256) is not str
    ):
        raise ValueError("durable exact provider response bytes are unavailable")
    if response_encoding == "hex":
        if (
            event_types[2] != "SubmissionUnknown"
            or len(response_text) > 2 * HARD_MAX_PROVIDER_RESPONSE_BYTES
        ):
            raise ValueError("opaque durable response must remain bounded UNKNOWN")
        try:
            response_bytes = bytes.fromhex(response_text)
        except ValueError:
            raise ValueError("durable opaque response encoding is invalid") from None
        if response_bytes.hex() != response_text:
            raise ValueError("durable opaque response encoding is noncanonical")
    else:
        response_bytes = response_text.encode("utf-8")
    if "sha256:" + sha256(response_bytes).hexdigest() != response_sha256:
        raise ValueError("durable provider response digest mismatch")
    http_status = sent_payload.get("http_status")
    if http_status is not None and (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
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
    if sending.get("aggregate_id") != aggregate_id or sent.get("aggregate_id") != aggregate_id:
        raise ValueError("durable submission aggregate identity mismatch")
    return SubmissionResponseBinding(
        attempt_id=attempt_id,
        aggregate_id=aggregate_id,
        provider=str(payload.get("provider", "")),
        request_hash=str(payload.get("request_hash", "")),
        client_order_id=str(payload.get("client_order_id", "")),
        environment=str(payload.get("environment", "")),
        account_id=str(payload.get("account_id", "")),
        prepared_at=prepared_at,
        sent_at=sent_at,
        submission_scope=scope,
        submission_scope_hash=scope_hash,
        response_bytes=response_bytes,
        response_sha256=response_sha256,
        response_encoding=response_encoding,
        terminal_state="SENT" if event_types[2] == "SubmissionSent" else "UNKNOWN",
        ambiguity_reason=sent_payload.get("reason") if event_types[2] == "SubmissionUnknown" else None,
        retry_disposition=sent_payload.get("retry_disposition") if event_types[2] == "SubmissionUnknown" else None,
        http_status=http_status,
        _factory_token=_SUBMISSION_RESPONSE_BINDING_TOKEN,
    )


def _install_submission_response_binding_authority(loader):
    """Retain durable response-binding issuance authority outside caller state."""

    binding_type = SubmissionResponseBinding
    binding_token = _SUBMISSION_RESPONSE_BINDING_TOKEN
    error_type = ValueError
    canonical_type = type
    canonical_id = id
    canonical_tuple = tuple
    canonical_range = range
    canonical_enumerate = enumerate
    canonical_isinstance = isinstance
    canonical_str = str
    canonical_int = int
    canonical_bool = bool
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
    canonical_attempt_id = submission_attempt_aggregate_id
    attempt_id_code = canonical_attempt_id.__code__
    canonical_decode = _decode_exact_json_bytes
    decode_code = canonical_decode.__code__
    canonical_freeze = _freeze_json
    freeze_code = canonical_freeze.__code__
    canonical_instant = _instant
    instant_code = canonical_instant.__code__
    canonical_json_function = canonical_json
    canonical_sha256 = sha256
    canonical_json_module = json
    canonical_re_module = re
    canonical_journal_type = JournalStore

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
            or range is not canonical_range
            or enumerate is not canonical_enumerate
            or isinstance is not canonical_isinstance
            or str is not canonical_str
            or int is not canonical_int
            or bool is not canonical_bool
            or bytes is not canonical_bytes
            or object is not canonical_object
            or getattr is not canonical_getattr
            or MappingProxyType is not mapping_proxy_type
            or weakref_ref is not canonical_weakref_ref
            or JournalStore is not canonical_journal_type
            or json is not canonical_json_module
            or re is not canonical_re_module
            or sha256 is not canonical_sha256
            or canonical_json is not canonical_json_function
            or _canonical_journal_authority_snapshot is not canonical_journal_snapshot
            or canonical_getattr(canonical_journal_snapshot, "__code__", None)
            is not journal_snapshot_code
            or submission_attempt_aggregate_id is not canonical_attempt_id
            or canonical_getattr(canonical_attempt_id, "__code__", None)
            is not attempt_id_code
            or _decode_exact_json_bytes is not canonical_decode
            or canonical_getattr(canonical_decode, "__code__", None)
            is not decode_code
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
        # The instance dictionary, not a mutable class descriptor, is the
        # recorded journal-issued state. Reject injected or missing fields.
        state = object_getattribute(value, "__dict__")
        if canonical_type(state) is not dict or frozenset(state) != frozenset(field_names):
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
        for index in canonical_range(len(field_names)):
            if index in (9, 11, len(field_names) - 1):
                if current[index] is not expected[index]:
                    authority_changed()
            elif (canonical_type(current[index]) is not canonical_type(expected[index])
                  or current[index] != expected[index]):
                authority_changed()
        return value

    def submission_response_binding_projection(value):
        require_canonical_submission_response_binding(value)
        current = raw_snapshot(value)
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
            not isinstance(max_length, int)
            or isinstance(max_length, bool)
            or max_length < 36
        ):
            raise ValueError(
                "UUID client-order identity requires max_length of at least 36"
            )
        return str(uuid5(NAMESPACE_URL, "client-order:" + digest))
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 20:
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
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


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
        self.owner_token = owner_token or str(uuid4())
        if not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool) or owner_epoch < 1:
            raise ValueError("owner_epoch must be a positive integer")
        self.owner_epoch = owner_epoch
        if type(prepared_lease_seconds) is not int or prepared_lease_seconds < 1:
            raise ValueError("prepared_lease_seconds must be a positive exact integer")
        self.prepared_lease_seconds = prepared_lease_seconds

    def _journal_store_authority(self) -> JournalStore:
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
        return JournalStore.load_events(
            store,
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
        expected_journal_sequence: int | None = None,
        _journal_commit_command: Callable[..., object] | None = None,
        co_events: list[tuple[dict[str, Any], str]] | None = None,
    ):
        store = self._journal_store_authority()
        envelope = _envelope(
            scope_key=self.scope_key,
            aggregate_id=self._aggregate_id(attempt_id),
            environment=self.environment,
            attempt_id=attempt_id,
            event_type=event_type,
            version=version,
            payload=payload,
            now=now,
            owner_epoch=self.owner_epoch,
        )
        if expected_journal_sequence is None:
            if co_events:
                raise ValueError("co_events require the journal-cut send barrier")
            return JournalStore.append_event(
                store,
                envelope,
                outbox_topic="autotrade.submission.events",
            )
        commit_command = (
            _CANONICAL_JOURNAL_COMMIT_COMMAND
            if _journal_commit_command is None
            else _journal_commit_command
        )
        _, inserted, appended = commit_command(
            store,
            command_id=envelope["event_id"],
            actor=f"dispatcher:{self.scope_key}",
            environment=self.environment,
            idempotency_key=f"send-barrier:{envelope['event_id']}",
            request={
                "event": envelope,
                "journal_sequence": expected_journal_sequence,
            },
            result={"event_id": envelope["event_id"]},
            state_version=expected_journal_sequence,
            events=[
                (envelope, "autotrade.submission.events"),
                *(co_events or []),
            ],
            expected_journal_sequence=expected_journal_sequence,
        )
        if not inserted:
            raise DispatchBlocked("send_barrier_already_committed")
        return appended[0]

    @staticmethod
    def _outcome_from_terminal(event: dict[str, Any], client_order_id: str) -> DispatchOutcome:
        payload = event["payload"]
        if event["event_type"] == "SubmissionSent":
            if _has_exact_response_markers(payload):
                # Any reserved exact marker commits the row to the SHA-bound
                # evidence contract. Partial/mislabeled exact rows must never
                # fall through to the historical response mirror.
                if payload.get("response_encoding") != "utf-8-json":
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

    def _recover_existing(
        self,
        *,
        attempt_id: str,
        client_order_id: str,
        now: str,
    ) -> DispatchOutcome:
        events = self._events(attempt_id)
        if not events:
            raise RuntimeError("submission attempt disappeared")
        last = events[-1]
        if last["event_type"] in {"SubmissionSent", "SubmissionBlocked", "SubmissionUnknown"}:
            return self._outcome_from_terminal(last, client_order_id)
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
                    return self._outcome_from_terminal(
                        current_last,
                        client_order_id,
                    )
                raise
            return self._outcome_from_terminal(self._events(attempt_id)[-1], client_order_id)
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
                return self._outcome_from_terminal(current_last, client_order_id)
            if current_last["event_type"] == "SubmissionSending":
                return self._recover_existing(
                    attempt_id=attempt_id,
                    client_order_id=client_order_id,
                    now=now,
                )
            raise
        return self._outcome_from_terminal(self._events(attempt_id)[-1], client_order_id)

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
        journal_current_sequence = _CANONICAL_JOURNAL_CURRENT_SEQUENCE
        journal_commit_command = _CANONICAL_JOURNAL_COMMIT_COMMAND
        for value, name in (
            (attempt_id, "attempt_id"),
            (intent_id, "intent_id"),
            (intent_hash, "intent_hash"),
            (provider, "provider"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} is required")
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        _instant(now)
        request_canonical = canonical_json(dict(request))
        request_dict = json.loads(request_canonical)
        request_frozen = _freeze_json(request_dict)
        request_hash = "sha256:" + sha256(request_canonical.encode("utf-8")).hexdigest()
        if submission_scope is None:
            scope_dict: dict[str, Any] = {}
        else:
            if not isinstance(submission_scope, Mapping):
                raise TypeError("submission_scope must be a mapping")
            scope_canonical = canonical_json(dict(submission_scope))
            scope_dict = json.loads(scope_canonical)
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

        intent_aggregate_id = submission_intent_aggregate_id(
            provider=provider,
            environment=self.environment,
            account_id=self.account_id,
            intent_id=intent_id,
        )

        existing = self._events(attempt_id)
        if existing:
            prepared = existing[0]["payload"]
            expected = {
                "attempt_id": attempt_id,
                "intent_id": intent_id,
                "intent_hash": intent_hash,
                "provider": provider,
                "request_hash": request_hash,
                "client_order_id": client_order_id,
                "environment": self.environment,
                "account_id": self.account_id,
                "submission_scope_hash": submission_scope_hash,
            }
            if any(prepared.get(key) != value for key, value in expected.items()):
                raise ValueError("attempt_id conflicts with existing submission content")
            return self._recover_existing(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                now=now,
            )

        # A stable provider client ID is necessary but not sufficient: provider
        # duplicate-ID behavior cannot be the authority for money movement. The
        # canonical intent aggregate makes the first durable send owner unique
        # across process restarts and across fresh attempt_id values.
        store = self._journal_store_authority()
        intent_events = JournalStore.load_events(
            store,
            "submission_intent",
            intent_aggregate_id,
        )
        if intent_events:
            if (
                len(intent_events) != 1
                or intent_events[0].get("event_type") != "SubmissionIntentBound"
            ):
                raise RuntimeError("durable submission intent binding is invalid")
            binding = intent_events[0].get("payload")
            if not isinstance(binding, dict):
                raise RuntimeError("durable submission intent binding payload is invalid")
            expected_binding = {
                "provider": provider,
                "environment": self.environment,
                "account_id": self.account_id,
                "intent_id": intent_id,
                "intent_hash": intent_hash,
                "request_hash": request_hash,
                "client_order_id": client_order_id,
                "submission_scope_hash": submission_scope_hash,
            }
            if any(binding.get(key) != value for key, value in expected_binding.items()):
                raise ValueError("intent_id conflicts with existing submission content")
            canonical_attempt_id = binding.get("attempt_id")
            if not isinstance(canonical_attempt_id, str) or not canonical_attempt_id:
                raise RuntimeError("durable submission intent owner is invalid")
            canonical_events = self._events(canonical_attempt_id)
            if not canonical_events:
                raise RuntimeError("submission intent binding lost its canonical attempt")
            canonical_last = canonical_events[-1]
            if canonical_last["event_type"] in {"SubmissionSent", "SubmissionUnknown"}:
                return self._outcome_from_terminal(canonical_last, client_order_id)
            # SubmissionIntentBound and SubmissionSending are committed in the
            # same SQLite transaction below. Seeing the binding therefore means
            # an irreversible send may already be in flight; never send again.
            if canonical_last["event_type"] == "SubmissionSending":
                return DispatchOutcome(
                    "UNKNOWN",
                    client_order_id,
                    None,
                    "intent_send_already_committed",
                )
            raise RuntimeError(
                "submission intent binding has no irreversible send evidence"
            )

        legacy_attempt_id = _legacy_submission_intent_attempt(
            store,
            provider=provider,
            environment=self.environment,
            account_id=self.account_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            request_hash=request_hash,
            client_order_id=client_order_id,
            submission_scope_hash=submission_scope_hash,
        )
        if legacy_attempt_id is not None:
            return self._recover_existing(
                attempt_id=legacy_attempt_id,
                client_order_id=client_order_id,
                now=now,
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
            )

        try:
            authority_result = authority_check(intent_hash, now)
        except Exception as error:
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

        def final_guard() -> None:
            nonlocal guard_called, barrier_passed, barrier_now
            if guard_called:
                raise RuntimeError("final send guard may be consumed only once")
            guard_called = True
            # request_frozen is a recursively immutable canonical JSON snapshot.
            # Transport cannot pass the barrier for one payload and then mutate
            # the same object before its actual provider call.
            if final_barrier_clock is not None:
                try:
                    barrier_now = final_barrier_clock()
                    parsed_barrier_now = _instant(barrier_now)
                except Exception as error:
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
            # Fixed journal cut before untrusted sender/authority callbacks.
            barrier_journal_sequence = journal_current_sequence(self._journal_store_authority())
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
                except Exception as error:
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
            except Exception as error:
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
            if not durable_before_send or durable_before_send[-1]["event_type"] != "SubmissionPrepared":
                raise DispatchBlocked(
                    "submission_changed_during_final_send_validation"
                )
            prepared_payload = durable_before_send[0].get("payload")
            if type(prepared_payload) is not dict or type(prepared_payload.get("prepared_at")) is not str:
                raise DispatchBlocked("submission_prepared_chronology_invalid")
            lease_state = _prepared_lease_state(
                prepared_at=prepared_payload["prepared_at"],
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
                intent_binding = _envelope(
                    scope_key=self.scope_key,
                    aggregate_id=intent_aggregate_id,
                    environment=self.environment,
                    attempt_id=attempt_id,
                    event_type="SubmissionIntentBound",
                    version=1,
                    payload={
                        "attempt_id": attempt_id,
                        "provider": provider,
                        "environment": self.environment,
                        "account_id": self.account_id,
                        "intent_id": intent_id,
                        "intent_hash": intent_hash,
                        "request_hash": request_hash,
                        "client_order_id": client_order_id,
                        "submission_scope_hash": submission_scope_hash,
                        "bound_at": _instant(barrier_now).isoformat().replace("+00:00", "Z"),
                    },
                    now=barrier_now,
                    owner_epoch=self.owner_epoch,
                )
                intent_binding["aggregate_type"] = "submission_intent"
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
                    expected_journal_sequence=barrier_journal_sequence,
                    _journal_commit_command=journal_commit_command,
                    co_events=[
                        (intent_binding, "autotrade.submission.intent.events"),
                    ],
                )
            except ValueError as error:
                reason = "journal_changed_during_final_send_validation"
                latest = self._events(attempt_id)
                if latest and latest[-1]["event_type"] == "SubmissionPrepared":
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
            barrier_passed = True

        canonical_response_guard = _require_canonical_exact_transport_response
        try:
            response = transport_send(client_order_id, request_frozen, final_guard)
        except DispatchBlocked as error:
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
                    return self._outcome_from_terminal(last, client_order_id)
            return DispatchOutcome("BLOCKED", client_order_id, None, str(error))
        except Exception as error:
            events = self._events(attempt_id)
            last = events[-1]
            if last["event_type"] == "SubmissionSending":
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=3,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": f"transport_exception_after_send_barrier:{type(error).__name__}",
                    },
                    now=barrier_now,
                )
                return DispatchOutcome("UNKNOWN", client_order_id, None, "transport_result_ambiguous")
            if not guard_called:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionBlocked",
                    version=2,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": f"transport_failed_before_final_guard:{type(error).__name__}",
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
                next_version = int(last["aggregate_version"]) + 1
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=next_version,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": (
                            "provider_wrapper_masked_final_guard_failure:"
                            + type(error).__name__
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
            next_version = int(last["aggregate_version"]) + 1
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

        terminal_requires_reconciliation = False
        terminal_reason = "sent_confirmed"
        try:
            if type(response) is ExactJsonTransportResponse:
                # Transport callbacks cannot rebind the response authority
                # after the final guard to relabel UNKNOWN as a safe SENT.
                if _require_canonical_exact_transport_response is not canonical_response_guard:
                    raise ValueError("exact transport response authority is unavailable")
                canonical_response_guard(response)
                # The exact raw bytes + digest are the durable source.
                # The prior "response" JSON mirror could silently round
                # decimals to float; persisting Decimal objects directly is
                # not JSON-serializable and misclassified valid sends UNKNOWN.
                # Keep the mirror out of exact response events altogether.
                sent_payload = {
                    "client_order_id": client_order_id,
                    "response_text": response.response_text,
                    "response_sha256": response.response_sha256,
                    "response_encoding": response.response_encoding,
                }
                if response.http_status is not None:
                    sent_payload["http_status"] = response.http_status
                outcome_response = (
                    response.payload if response.response_encoding == "utf-8-json"
                    else None
                )
                terminal_requires_reconciliation = response.requires_reconciliation
                if terminal_requires_reconciliation:
                    terminal_reason = (
                        response.ambiguity_reason
                        or "provider_response_ambiguous"
                    )
                    sent_payload["reason"] = terminal_reason
                    sent_payload["retry_disposition"] = "RECONCILE_FIRST"
            elif isinstance(response, ExactJsonTransportResponse):
                # Caller-polymorphic post-SEND response getters are not evidence.
                # A durable UNKNOWN retains the no-blind-retry property.
                raise TypeError("exact provider response subtype is forbidden")
            else:
                sent_payload = {
                    "client_order_id": client_order_id,
                    "response": response,
                }
                outcome_response = response
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
        except Exception as persistence_error:
            # The outbound request has already crossed the final barrier.
            # Never make this state safe to retry merely because the provider
            # response could not be journaled.
            try:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=3,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": (
                            "sent_response_persistence_failed:"
                            + type(persistence_error).__name__
                        ),
                    },
                    now=barrier_now,
                )
            except Exception:
                # A durable SubmissionSending row already exists. Recovery will
                # convert that state to UNKNOWN without another outbound send.
                raise persistence_error
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "sent_response_persistence_failed",
            )
        if terminal_requires_reconciliation:
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                terminal_reason,
            )
        return DispatchOutcome(
            "SENT",
            client_order_id,
            outcome_response,
            "sent_confirmed",
        )


# Install journal-owned response binding only after every dependent helper,
# including _instant, has been defined. Imported modules see the final sealed API.
(
    load_submission_response_binding,
    require_canonical_submission_response_binding,
    submission_response_binding_projection,
) = _install_submission_response_binding_authority(
    load_submission_response_binding
)
del _install_submission_response_binding_authority
