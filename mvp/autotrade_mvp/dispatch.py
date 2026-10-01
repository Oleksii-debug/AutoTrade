"""Durable guarded submission attempts for the AutoTrade MVP."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import weakref
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5, uuid4

from autotrade_numeric.exact_decimal import (
    ExactDecimalError,
    parse_bounded_json_integer_token,
    parse_bounded_json_number_token,
)

from .persistence import (
    AggregatePreconditionFailed,
    JournalSequencePreconditionFailed,
    ExpectedAggregateHead,
    JournalStore,
    canonical_json,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_response_limits import (
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_json_depth,
    require_provider_response_bytes,
)
from .sender_gate import journal_sender_gate


AuthorityCheck = Callable[[str, str], tuple[bool, str]]
SenderCheck = Callable[[str, int], None]
TransportSend = Callable[[str, Mapping[str, Any], Callable[[], None]], Any]

_SUBMISSION_RESPONSE_BINDING_TOKEN = object()
_DISPATCHER_SENDER_BINDINGS = weakref.WeakKeyDictionary()
_DISPATCHER_JOURNAL_BINDINGS = weakref.WeakKeyDictionary()
_DISPATCHER_SCOPE_BINDINGS = weakref.WeakKeyDictionary()
_BOUND_SENDER_ISSUANCE_TOKEN = object()
_FINANCIAL_AUTHORITY_ISSUANCE_TOKEN = object()
_FINANCIAL_AUTHORITY_BINDINGS = weakref.WeakKeyDictionary()
_EXACT_RESPONSE_MARKERS = frozenset(
    {"response_encoding", "response_text", "response_base64", "response_sha256"}
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
        _decode_exact_json_bytes(raw)
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

    @property
    def response_text(self) -> str:
        return self.response_bytes.decode("utf-8")

    @property
    def response_sha256(self) -> str:
        return "sha256:" + sha256(self.response_bytes).hexdigest()

    @property
    def payload(self) -> Any:
        return _decode_exact_json_bytes(self.response_bytes)


@dataclass(frozen=True)
class ExactOpaqueTransportResponse:
    """Exact bounded non-JSON provider bytes after an irreversible send.

    Opaque responses are reconciliation evidence only. They never promote to a
    successful JSON provider observation, SENT authority, acknowledgement, or fill.
    """

    response_bytes: bytes
    http_status: int
    ambiguity_reason: str

    def __post_init__(self) -> None:
        try:
            require_provider_response_bytes(
                self.response_bytes,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                allow_empty=True,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("opaque provider response bytes are invalid") from error
        if (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ValueError("opaque http_status must be an integer 100..599")
        if (
            type(self.ambiguity_reason) is not str
            or not self.ambiguity_reason.strip()
        ):
            raise ValueError(
                "opaque provider response requires a non-empty ambiguity_reason"
            )
        object.__setattr__(
            self,
            "ambiguity_reason",
            self.ambiguity_reason.strip(),
        )

    @property
    def response_sha256(self) -> str:
        return "sha256:" + sha256(self.response_bytes).hexdigest()


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

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("SubmissionResponseBinding is sealed")

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
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_hash) is None:
            raise ValueError("request_hash must be a canonical SHA-256 digest")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.submission_scope_hash) is None:
            raise ValueError(
                "submission_scope_hash must be a canonical SHA-256 digest"
            )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.response_sha256) is None:
            raise ValueError("response_sha256 must be a canonical SHA-256 digest")
        if self.response_encoding == "utf-8-json":
            try:
                require_provider_response_bytes(
                    self.response_bytes,
                    max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                )
            except (TypeError, ValueError) as error:
                raise ValueError("durable JSON provider response bytes are invalid") from error
            _decode_exact_json_bytes(self.response_bytes)
        elif self.response_encoding == "base64":
            try:
                require_provider_response_bytes(
                    self.response_bytes,
                    max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                    allow_empty=True,
                )
            except (TypeError, ValueError) as error:
                raise ValueError("durable opaque provider response bytes are invalid") from error
        else:
            raise ValueError("unsupported durable provider response encoding")
        if (
            "sha256:" + sha256(self.response_bytes).hexdigest()
            != self.response_sha256
        ):
            raise ValueError("durable provider response digest mismatch")
        if not isinstance(self.terminal_state, str):
            raise TypeError("terminal_state must be a string")
        terminal_state = self.terminal_state.strip().upper()
        if terminal_state not in {"SENT", "UNKNOWN"}:
            raise ValueError("terminal_state must be SENT or UNKNOWN")
        object.__setattr__(self, "terminal_state", terminal_state)
        if terminal_state == "UNKNOWN":
            if (
                not isinstance(self.ambiguity_reason, str)
                or not self.ambiguity_reason.strip()
            ):
                raise ValueError(
                    "UNKNOWN durable response requires an ambiguity_reason"
                )
            if self.retry_disposition != "RECONCILE_FIRST":
                raise ValueError(
                    "UNKNOWN durable response must remain RECONCILE_FIRST"
                )
            object.__setattr__(
                self,
                "ambiguity_reason",
                self.ambiguity_reason.strip(),
            )
        elif self.ambiguity_reason is not None or self.retry_disposition is not None:
            raise ValueError(
                "SENT durable response cannot carry UNKNOWN retry semantics"
            )
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


class _IssuedFinancialAuthorityCheck:
    """Opaque trusted-process capability issued by AuthorityService only.

    The object intentionally carries no callback or mutable authority state.
    Its executable binding lives only in module-owned weak state. This is a
    trusted-process provenance fence, not a sandbox against arbitrary code
    executing inside this module's trust boundary.
    """

    __slots__ = ("__weakref__",)

    def __new__(cls, issuance_token: object):
        if (
            cls is not _IssuedFinancialAuthorityCheck
            or issuance_token is not _FINANCIAL_AUTHORITY_ISSUANCE_TOKEN
        ):
            raise PermissionError(
                "financial dispatch authority must be issued by AuthorityService"
            )
        return super().__new__(cls)

    def __call__(self, intent_hash: str, now: str) -> tuple[bool, str]:
        if type(self) is not _IssuedFinancialAuthorityCheck:
            raise PermissionError("financial dispatch authority type changed")
        binding = _FINANCIAL_AUTHORITY_BINDINGS.get(self)
        if binding is None:
            raise PermissionError("financial dispatch authority is not issued")
        callback, _store, _environment, _account_id = binding
        return callback(intent_hash, now)


def _issue_financial_authority_check(
    callback: AuthorityCheck,
    *,
    store: JournalStore | None,
    environment: str,
    account_id: str,
) -> AuthorityCheck:
    """Issue one opaque financial authority capability for one exact scope."""

    if not callable(callback):
        raise TypeError("financial authority callback must be callable")
    normalized_environment = (
        environment.strip().upper() if isinstance(environment, str) else ""
    )
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError(
            "financial authority environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("financial authority account_id is required")
    normalized_account_id = account_id.strip()
    if store is not None:
        require_exact_journal_store_authority(store)
    capability = _IssuedFinancialAuthorityCheck(
        _FINANCIAL_AUTHORITY_ISSUANCE_TOKEN
    )
    _FINANCIAL_AUTHORITY_BINDINGS[capability] = (
        callback,
        store,
        normalized_environment,
        normalized_account_id,
    )
    return capability


def _issued_financial_authority_binding(
    value: object,
) -> tuple[AuthorityCheck, JournalStore | None, str, str]:
    if type(value) is not _IssuedFinancialAuthorityCheck:
        raise PermissionError(
            "PAPER/LIVE financial authority must be issued by AuthorityService"
        )
    binding = _FINANCIAL_AUTHORITY_BINDINGS.get(value)
    if binding is None:
        raise PermissionError("financial dispatch authority is not issued")
    callback, store, environment, account_id = binding
    if not callable(callback):
        raise PermissionError("financial dispatch authority binding is invalid")
    if environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise PermissionError("financial dispatch authority scope is invalid")
    if not isinstance(account_id, str) or not account_id:
        raise PermissionError("financial dispatch authority scope is invalid")
    return callback, store, environment, account_id


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
        environment.strip().upper() if isinstance(environment, str) else ""
    )
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id is required")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("attempt_id is required")
    return "submission-attempt:" + _identity_digest(
        normalized_environment,
        account_id.strip(),
        attempt_id.strip(),
    )


def _canonical_journal_authority_snapshot(
    store: JournalStore,
) -> tuple[Path, object]:
    """Compatibility adapter over the persistence-owned JournalStore authority."""

    identity = require_exact_journal_store_authority(
        store,
        subject="canonical JournalStore",
    )
    return Path(identity.canonical_path), identity

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
    versions = [event.get("aggregate_version") for event in events]
    if (
        any(type(version) is not int for version in versions)
        or versions != [1, 2, 3]
    ):
        raise ValueError(
            "durable exact response requires aggregate versions 1 -> 2 -> 3"
        )
    if any(
        event.get("aggregate_type") != "submission_attempt"
        or event.get("aggregate_id") != aggregate_id
        for event in events
    ):
        raise ValueError("durable submission aggregate identity mismatch")
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

    durable_attempt_id = payload.get("attempt_id")
    durable_environment = payload.get("environment")
    durable_account_id = payload.get("account_id")
    if durable_attempt_id != attempt_id:
        raise ValueError("durable submission attempt identity mismatch")
    try:
        prepared_aggregate_id = submission_attempt_aggregate_id(
            environment=durable_environment,
            account_id=durable_account_id,
            attempt_id=attempt_id,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("durable prepared submission identity is invalid") from error
    if prepared_aggregate_id != aggregate_id:
        raise ValueError("durable prepared submission aggregate mismatch")

    client_order_id = payload.get("client_order_id")
    if not isinstance(client_order_id, str) or not client_order_id:
        raise ValueError("durable prepared client_order_id is invalid")
    if (
        sending_payload.get("client_order_id") != client_order_id
        or sent_payload.get("client_order_id") != client_order_id
    ):
        raise ValueError("durable submission client_order_id continuity mismatch")
    owner_token = payload.get("owner_token")
    owner_epoch = payload.get("owner_epoch")
    if (
        not isinstance(owner_token, str)
        or not owner_token
        or type(owner_epoch) is not int
        or owner_epoch < 1
        or sending_payload.get("owner_token") != owner_token
        or sending_payload.get("owner_epoch") != owner_epoch
    ):
        raise ValueError("durable SubmissionSending owner continuity mismatch")
    response_encoding = sent_payload.get("response_encoding")
    response_sha256 = sent_payload.get("response_sha256")
    if not isinstance(response_sha256, str):
        raise ValueError("durable exact provider response digest is unavailable")
    terminal_state = (
        "UNKNOWN" if sent.get("event_type") == "SubmissionUnknown" else "SENT"
    )
    ambiguity_reason = None
    retry_disposition = None
    if terminal_state == "UNKNOWN":
        ambiguity_reason = sent_payload.get("reason")
        retry_disposition = sent_payload.get("retry_disposition")
        if (
            not isinstance(ambiguity_reason, str)
            or not ambiguity_reason.strip()
            or retry_disposition != "RECONCILE_FIRST"
        ):
            raise ValueError(
                "response-bearing SubmissionUnknown must remain RECONCILE_FIRST"
            )
        ambiguity_reason = ambiguity_reason.strip()
    elif (
        sent_payload.get("retry_disposition") is not None
        or sent_payload.get("reason") is not None
    ):
        raise ValueError("SubmissionSent cannot carry UNKNOWN retry semantics")
    if response_encoding == "utf-8-json":
        response_text = sent_payload.get("response_text")
        if not isinstance(response_text, str) or not response_text:
            raise ValueError(
                "durable exact provider JSON response bytes are unavailable"
            )
        response_bytes = response_text.encode("utf-8")
    elif response_encoding == "base64":
        if sent.get("event_type") != "SubmissionUnknown":
            raise ValueError("opaque provider response must remain SubmissionUnknown")
        if sent_payload.get("retry_disposition") != "RECONCILE_FIRST":
            raise ValueError("opaque provider response must require reconciliation")
        opaque_reason = sent_payload.get("reason")
        if type(opaque_reason) is not str or not opaque_reason.strip():
            raise ValueError("opaque provider response ambiguity reason is unavailable")
        response_base64 = sent_payload.get("response_base64")
        if type(response_base64) is not str:
            raise ValueError(
                "durable exact opaque provider response bytes are unavailable"
            )
        try:
            ascii_bytes = response_base64.encode("ascii", errors="strict")
            response_bytes = base64.b64decode(ascii_bytes, validate=True)
        except (UnicodeError, ValueError, binascii.Error):
            raise ValueError("durable opaque provider response base64 is invalid")
        if base64.b64encode(response_bytes).decode("ascii") != response_base64:
            raise ValueError("durable opaque provider response base64 is non-canonical")
    else:
        raise ValueError("durable exact provider response encoding is unavailable")
    if "sha256:" + sha256(response_bytes).hexdigest() != response_sha256:
        raise ValueError("durable provider response digest mismatch")
    http_status = sent_payload.get("http_status")
    if response_encoding == "base64" and http_status is None:
        raise ValueError("opaque provider response HTTP status is unavailable")
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
        client_order_id=client_order_id,
        environment=str(durable_environment or ""),
        account_id=str(durable_account_id or ""),
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


def stable_client_order_id(
    provider: str,
    intent_id: str,
    *,
    environment: str,
    account_id: str,
    max_length: int = 32,
    client_id_format: str = "TOKEN",
) -> str:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider is required")
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise ValueError("intent_id is required")
    normalized_environment = environment.strip().upper() if isinstance(environment, str) else ""
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id is required")
    if not isinstance(client_id_format, str):
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
    if not isinstance(value, str) or not value.strip():
        raise ValueError("now must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("now must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("now must include a timezone")
    return parsed.astimezone(timezone.utc)


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


def _canonical_sender_preconditions(
    value: object,
) -> tuple[ExpectedAggregateHead, ...]:
    if type(value) is not tuple:
        raise TypeError(
            "bound_sender_preconditions must be an exact tuple of "
            "ExpectedAggregateHead values"
        )
    canonical: list[ExpectedAggregateHead] = []
    expected_keys = frozenset(
        {
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "latest_event_id",
            "latest_payload_hash",
        }
    )
    for item in value:
        if type(item) is not ExpectedAggregateHead:
            raise TypeError(
                "bound_sender_preconditions must be an exact tuple of "
                "ExpectedAggregateHead values"
            )
        state = vars(item)
        if (
            any(type(name) is not str for name in state)
            or frozenset(state) != expected_keys
        ):
            raise TypeError("sender aggregate precondition state is non-canonical")
        canonical.append(
            ExpectedAggregateHead(
                aggregate_type=state["aggregate_type"],
                aggregate_id=state["aggregate_id"],
                aggregate_version=state["aggregate_version"],
                latest_event_id=state["latest_event_id"],
                latest_payload_hash=state["latest_payload_hash"],
            )
        )
    return tuple(canonical)


def _issued_sender_binding(
    dispatcher: object,
) -> tuple[SenderCheck | None, tuple[ExpectedAggregateHead, ...]]:
    if type(dispatcher) is not GuardedDispatcher:
        raise TypeError("dispatcher must be exact GuardedDispatcher")
    binding = _DISPATCHER_SENDER_BINDINGS.get(dispatcher)
    if binding is None:
        raise PermissionError("dispatcher lacks issued sender authority")
    issued_check, issued_preconditions = binding
    if dispatcher._bound_sender_check is not issued_check:
        raise PermissionError("dispatcher sender authority changed")
    visible_preconditions = _canonical_sender_preconditions(
        dispatcher._bound_sender_preconditions
    )
    if visible_preconditions != issued_preconditions:
        raise PermissionError("dispatcher sender authority changed")
    return issued_check, issued_preconditions


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
        bound_sender_check: SenderCheck | None = None,
        bound_sender_preconditions: tuple[ExpectedAggregateHead, ...] = (),
        _bound_sender_issuance_token: object | None = None,
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
        _DISPATCHER_JOURNAL_BINDINGS[self] = (
            store,
            self._journal_store_path,
            self._journal_store_identity,
        )
        normalized_environment = (
            environment.strip().upper() if isinstance(environment, str) else ""
        )
        if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required")
        self.environment = normalized_environment
        self.account_id = account_id.strip()
        self.scope_key = _identity_digest(self.environment, self.account_id)
        self.owner_token = owner_token or str(uuid4())
        if not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool) or owner_epoch < 1:
            raise ValueError("owner_epoch must be a positive integer")
        self.owner_epoch = owner_epoch
        if not isinstance(prepared_lease_seconds, int) or isinstance(prepared_lease_seconds, bool) or prepared_lease_seconds < 1:
            raise ValueError("prepared_lease_seconds must be a positive integer")
        self.prepared_lease_seconds = prepared_lease_seconds
        _DISPATCHER_SCOPE_BINDINGS[self] = (
            self.environment,
            self.account_id,
            self.scope_key,
            self.owner_token,
            self.owner_epoch,
            self.prepared_lease_seconds,
        )
        # bound_sender_check/preconditions are financial sender authority.  The
        # public constructor may preserve low-level compatibility, but it must
        # not be an authority minting API.  Only the module-owned recovery
        # issuance path may populate these fields.  This is a trusted-process
        # provenance boundary, not a sandbox against arbitrary in-process code.
        if type(bound_sender_preconditions) is not tuple:
            raise TypeError(
                "bound_sender_preconditions must be an exact tuple of "
                "ExpectedAggregateHead values"
            )
        has_requested_bound_sender = (
            bound_sender_check is not None or len(bound_sender_preconditions) != 0
        )
        if (
            has_requested_bound_sender
            and _bound_sender_issuance_token is not _BOUND_SENDER_ISSUANCE_TOKEN
        ):
            raise PermissionError(
                "bound sender authority must be issued by recovery composition"
            )
        if (
            _bound_sender_issuance_token is not None
            and _bound_sender_issuance_token is not _BOUND_SENDER_ISSUANCE_TOKEN
        ):
            raise PermissionError("invalid bound sender issuance token")
        if (
            _bound_sender_issuance_token is _BOUND_SENDER_ISSUANCE_TOKEN
            and bound_sender_check is None
        ):
            raise ValueError("issued bound sender authority requires a sender check")
        if bound_sender_check is not None and not callable(bound_sender_check):
            raise TypeError("bound_sender_check must be callable when provided")
        bound_sender_preconditions = _canonical_sender_preconditions(
            bound_sender_preconditions
        )
        if bound_sender_check is None and bound_sender_preconditions:
            raise ValueError(
                "sender aggregate preconditions require a bound sender authority"
            )
        if (
            self.environment in {"PAPER", "LIVE"}
            and bound_sender_check is not None
            and not bound_sender_preconditions
        ):
            raise ValueError(
                "bound PAPER/LIVE sender authority requires aggregate preconditions"
            )
        # A RecoveryController-minted dispatcher carries the selected sender
        # authority with the dispatcher. Once present, that authority is the
        # exclusive sender check: legacy per-call sender_check is not invoked,
        # so caller code cannot mutate sender state between the bound check and
        # the durable send barrier.
        self._bound_sender_check = bound_sender_check
        self._bound_sender_preconditions = bound_sender_preconditions
        _DISPATCHER_SENDER_BINDINGS[self] = (
            bound_sender_check,
            _canonical_sender_preconditions(bound_sender_preconditions),
        )

    def _journal_store_authority(self) -> JournalStore:
        if type(self) is not GuardedDispatcher:
            raise TypeError("dispatcher must be exact GuardedDispatcher")
        issued_scope = _DISPATCHER_SCOPE_BINDINGS.get(self)
        if issued_scope is None:
            raise PermissionError("dispatcher lacks issued scope authority")
        visible_scope = (
            self.environment,
            self.account_id,
            self.scope_key,
            self.owner_token,
            self.owner_epoch,
            self.prepared_lease_seconds,
        )
        if visible_scope != issued_scope:
            raise PermissionError("submission dispatcher scope changed")
        binding = _DISPATCHER_JOURNAL_BINDINGS.get(self)
        if binding is None:
            raise PermissionError("dispatcher lacks issued journal authority")
        issued_store, issued_path, issued_identity = binding
        if (
            self.store is not issued_store
            or self._journal_store_path != issued_path
            or self._journal_store_identity != issued_identity
        ):
            raise PermissionError("submission journal authority changed")
        path, identity = _canonical_journal_authority_snapshot(issued_store)
        if path != issued_path or identity != issued_identity:
            raise PermissionError("submission journal authority changed")
        return issued_store

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
        aggregate_preconditions: tuple[ExpectedAggregateHead, ...] = (),
        expected_journal_sequence: int | None = None,
    ):
        store = self._journal_store_authority()
        return JournalStore.append_event(
            store,
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
            aggregate_preconditions=aggregate_preconditions,
            expected_journal_sequence=expected_journal_sequence,
        )

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
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionUnknown",
                version=last["aggregate_version"] + 1,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "recovered_after_send_barrier_without_terminal_result",
                },
                now=now,
            )
            return self._outcome_from_terminal(self._events(attempt_id)[-1], client_order_id)
        if last["event_type"] != "SubmissionPrepared":
            raise RuntimeError(f"unsupported submission attempt state: {last['event_type']}")

        prepared_at = _instant(last["payload"]["prepared_at"])
        age = (_instant(now) - prepared_at).total_seconds()
        if age < 0:
            return DispatchOutcome("IN_PROGRESS", client_order_id, None, "clock_before_prepared_timestamp")
        if age < self.prepared_lease_seconds:
            return DispatchOutcome("IN_PROGRESS", client_order_id, None, "prepared_owner_lease_active")
        self._append(
            attempt_id=attempt_id,
            event_type="SubmissionUnknown",
            version=last["aggregate_version"] + 1,
            payload={
                "client_order_id": client_order_id,
                "reason": "prepared_owner_lease_expired_without_send_evidence",
            },
            now=now,
        )
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
        for value, name in (
            (attempt_id, "attempt_id"),
            (intent_id, "intent_id"),
            (intent_hash, "intent_hash"),
            (provider, "provider"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        if self.environment in {"PAPER", "LIVE"}:
            (
                _issued_callback,
                issued_store,
                issued_environment,
                issued_account_id,
            ) = _issued_financial_authority_binding(authority_check)
            selected_store = self._journal_store_authority()
            if issued_store is None or issued_store is not selected_store:
                raise PermissionError(
                    "PAPER/LIVE financial authority belongs to another journal authority"
                )
            if (
                issued_environment != self.environment
                or issued_account_id != self.account_id
            ):
                raise PermissionError(
                    "PAPER/LIVE financial authority belongs to another dispatcher scope"
                )
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
            try:
                (
                    bound_sender_check,
                    issued_sender_preconditions,
                ) = _issued_sender_binding(self)
            except Exception as error:
                barrier_reason = (
                    "bound_sender_authority_changed:" + type(error).__name__
                )
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
            if (
                self.environment in {"PAPER", "LIVE"}
                and bound_sender_check is None
                and sender_check is None
            ):
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
            # A product/recovery-bound authority is exclusive.  The per-call
            # hook exists only for legacy low-level dispatchers that were not
            # minted with a bound sender authority. Capture the canonical
            # global journal cut before validating sender readiness so any
            # durable mutation racing that readiness check is fenced by the
            # same transaction that acquires SubmissionSending.
            sender_journal_cut = JournalStore.current_journal_sequence(
                self._journal_store_authority()
            )
            effective_sender_check = (
                bound_sender_check
                if bound_sender_check is not None
                else sender_check
            )
            if effective_sender_check is not None:
                try:
                    effective_sender_check(self.owner_token, self.owner_epoch)
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
            try:
                sending = self._append(
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
                    aggregate_preconditions=issued_sender_preconditions,
                    expected_journal_sequence=sender_journal_cut,
                )
            except (AggregatePreconditionFailed, JournalSequencePreconditionFailed) as error:
                barrier_reason = (
                    "sender_head_changed_before_sending"
                    if isinstance(error, AggregatePreconditionFailed)
                    else "journal_cut_changed_before_sending"
                )
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
            if not sending.inserted:
                raise RuntimeError(
                    "fresh SubmissionSending acquisition returned historical replay"
                )
            barrier_passed = True

        with journal_sender_gate(self._journal_store_authority()):
            try:
                response = transport_send(client_order_id, request_frozen, final_guard)
            except DispatchBlocked as error:
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
                    # The exact raw bytes + digest are the durable source.
                    # The prior "response" JSON mirror could silently round
                    # decimals to float; persisting Decimal objects directly is
                    # not JSON-serializable and misclassified valid sends UNKNOWN.
                    # Keep the mirror out of exact response events altogether.
                    sent_payload = {
                        "client_order_id": client_order_id,
                        "response_text": response.response_text,
                        "response_sha256": response.response_sha256,
                        "response_encoding": "utf-8-json",
                    }
                    if response.http_status is not None:
                        sent_payload["http_status"] = response.http_status
                    outcome_response = response.payload
                    terminal_requires_reconciliation = response.requires_reconciliation
                    if terminal_requires_reconciliation:
                        terminal_reason = (
                            response.ambiguity_reason
                            or "provider_response_ambiguous"
                        )
                        sent_payload["reason"] = terminal_reason
                        sent_payload["retry_disposition"] = "RECONCILE_FIRST"
                elif type(response) is ExactOpaqueTransportResponse:
                    sent_payload = {
                        "client_order_id": client_order_id,
                        "response_base64": base64.b64encode(
                            response.response_bytes
                        ).decode("ascii"),
                        "response_sha256": response.response_sha256,
                        "response_encoding": "base64",
                        "http_status": response.http_status,
                        "reason": response.ambiguity_reason,
                        "retry_disposition": "RECONCILE_FIRST",
                    }
                    outcome_response = None
                    terminal_requires_reconciliation = True
                    terminal_reason = response.ambiguity_reason
                elif isinstance(
                    response,
                    (ExactJsonTransportResponse, ExactOpaqueTransportResponse),
                ):
                    # Caller-polymorphic post-SEND response getters are not evidence.
                    # A durable UNKNOWN retains the no-blind-retry property.
                    raise TypeError("exact provider response subtype is forbidden")
                else:
                    if self.environment in {"PAPER", "LIVE"}:
                        # A real financial send may be terminally classified only
                        # from exact bounded provider wire evidence.  A generic
                        # Python object can be a parser/test convenience, but it
                        # cannot prove what bytes/status actually crossed the
                        # provider boundary and must never create a fresh legacy-
                        # shaped SubmissionSent row.
                        sent_payload = {
                            "client_order_id": client_order_id,
                            "reason": "provider_response_missing_exact_wire_evidence",
                            "retry_disposition": "RECONCILE_FIRST",
                        }
                        outcome_response = None
                        terminal_requires_reconciliation = True
                        terminal_reason = (
                            "provider_response_missing_exact_wire_evidence"
                        )
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



def _issue_recovery_guarded_dispatcher(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    owner_token: str,
    owner_epoch: int,
    prepared_lease_seconds: int,
    bound_sender_check: SenderCheck,
    bound_sender_preconditions: tuple[ExpectedAggregateHead, ...],
) -> GuardedDispatcher:
    """Issue one recovery-bound dispatcher inside the trusted product TCB.

    Ordinary callers must not be able to turn the public GuardedDispatcher
    constructor into a PAPER/LIVE sender-authority mint.  Recovery owns the
    canonical owner/currentness checks; dispatch owns the sealed lifetime
    binding consumed at the irreversible send barrier.
    """

    return GuardedDispatcher(
        store,
        environment=environment,
        account_id=account_id,
        owner_token=owner_token,
        owner_epoch=owner_epoch,
        prepared_lease_seconds=prepared_lease_seconds,
        bound_sender_check=bound_sender_check,
        bound_sender_preconditions=bound_sender_preconditions,
        _bound_sender_issuance_token=_BOUND_SENDER_ISSUANCE_TOKEN,
    )
