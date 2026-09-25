"""Durable guarded submission attempts for the AutoTrade MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5, uuid4

from .persistence import JournalStore, canonical_json, payload_digest


AuthorityCheck = Callable[[str, str], tuple[bool, str]]
SenderCheck = Callable[[str, int], None]
TransportSend = Callable[[str, Mapping[str, Any], Callable[[], None]], Any]

_SUBMISSION_RESPONSE_BINDING_TOKEN = object()


def _decode_exact_json_bytes(raw: bytes) -> Any:
    if type(raw) is not bytes or not raw:
        raise ValueError("provider response bytes must be non-empty bytes")

    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(
                    f"provider response contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=no_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(
                    f"provider response contains non-finite JSON constant: {value}"
                )
            ),
        )
    except ValueError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "provider response must be exact UTF-8 JSON bytes"
        ) from error


@dataclass(frozen=True)
class ExactJsonTransportResponse:
    """Exact provider wire bytes returned after the guarded send barrier."""

    response_bytes: bytes
    http_status: int | None = None

    def __post_init__(self) -> None:
        raw = self.response_bytes
        _decode_exact_json_bytes(raw)
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ValueError("http_status must be an integer 100..599 when provided")

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
        if type(self.response_bytes) is not bytes or not self.response_bytes:
            raise ValueError("response_bytes must be non-empty bytes")
        if (
            "sha256:" + sha256(self.response_bytes).hexdigest()
            != self.response_sha256
        ):
            raise ValueError("durable provider response digest mismatch")
        _decode_exact_json_bytes(self.response_bytes)
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
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


def load_submission_response_binding(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    attempt_id: str,
) -> SubmissionResponseBinding:
    """Load exact provider response provenance from the canonical submission journal."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    aggregate_id = submission_attempt_aggregate_id(
        environment=environment,
        account_id=account_id,
        attempt_id=attempt_id,
    )
    events = store.load_events("submission_attempt", aggregate_id)
    if not events:
        raise ValueError("durable submission attempt was not found")
    event_types = [event.get("event_type") for event in events]
    if event_types != ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"]:
        raise ValueError(
            "durable exact response requires Prepared -> Sending -> Sent"
        )
    prepared, sending, sent = events
    payload = prepared.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("durable SubmissionPrepared payload is invalid")
    sent_payload = sent.get("payload")
    if not isinstance(sent_payload, dict):
        raise ValueError("durable SubmissionSent payload is invalid")
    response_text = sent_payload.get("response_text")
    response_sha256 = sent_payload.get("response_sha256")
    if (
        sent_payload.get("response_encoding") != "utf-8-json"
        or not isinstance(response_text, str)
        or not response_text
        or not isinstance(response_sha256, str)
    ):
        raise ValueError(
            "durable exact provider response bytes are unavailable"
        )
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
        self.store = store
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

    def _aggregate_id(self, attempt_id: str) -> str:
        return submission_attempt_aggregate_id(
            environment=self.environment,
            account_id=self.account_id,
            attempt_id=attempt_id,
        )

    def _events(self, attempt_id: str) -> list[dict[str, Any]]:
        return self.store.load_events(
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
        return self.store.append_event(
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
        if event["event_type"] == "SubmissionSent":
            return DispatchOutcome("SENT", client_order_id, payload.get("response"), "sent_confirmed")
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
            barrier_passed = True

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

        try:
            if isinstance(response, ExactJsonTransportResponse):
                sent_payload = {
                    "client_order_id": client_order_id,
                    "response": response.payload,
                    "response_text": response.response_text,
                    "response_sha256": response.response_sha256,
                    "response_encoding": "utf-8-json",
                }
                if response.http_status is not None:
                    sent_payload["http_status"] = response.http_status
                outcome_response = response.payload
            else:
                sent_payload = {
                    "client_order_id": client_order_id,
                    "response": response,
                }
                outcome_response = response
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionSent",
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
        return DispatchOutcome(
            "SENT",
            client_order_id,
            outcome_response,
            "sent_confirmed",
        )
