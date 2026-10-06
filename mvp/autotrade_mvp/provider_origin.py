"""Durable authenticated provider-read evidence ledger.

This module records exact Prepared/read-response chronology and reconstructs
response bytes after restart. Same-process Python objects, private names, journal
hashes and local chronology are not external provider-origin authority.

Until the independently authenticated provider-wire issuer is integrated,
local response recording is explicitly TEST_INJECTED and the financial
PROVIDER_ORIGIN promotion path fails closed.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderCoreError,
    ProviderResponseObservation,
    Surface,
    _require_authenticated_read_query_binding_authority,
    observe_authenticated_json_response,
)
from .provider_response_limits import (
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_response_bytes,
)


class ProviderOriginError(RuntimeError):
    """Raised when durable provider-origin authority is incomplete or invalid."""


_BINDING_TOKEN = object()
_OBSERVATION_TOKEN = object()
_AGGREGATE_TYPE = "authenticated_provider_read"
_PREPARED_EVENT = "AuthenticatedReadPrepared"
_OBSERVED_EVENT = "AuthenticatedReadObserved"
_PENDING_KIND = "PROVIDER_WIRE_PENDING"
_PROVIDER_ORIGIN_KIND = "PROVIDER_ORIGIN"
_TEST_INJECTED_KIND = "TEST_INJECTED"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PROVIDER_ORIGIN_REF_RE = re.compile(r"provider-origin:sha256:[0-9a-f]{64}")
_TEST_INJECTED_REF_RE = re.compile(r"provider-test:sha256:[0-9a-f]{64}")
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "payload",
        "payload_hash",
        "committed_at",
        "journal_sequence",
    }
)
_PREPARED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "query",
        "transport_identity",
        "network_policy_identity",
    }
)
_OBSERVED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "prepared_event_id",
        "query_digest",
        "transport_identity",
        "network_policy_identity",
        "http_status",
        "response_sha256",
        "response_base64",
        "observed_at",
    }
)


def _require_origin_journal_authority(
    store: object,
    _require=require_exact_journal_store_authority,
):
    try:
        return _require(store, subject="provider-origin JournalStore")
    except (TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginError(
            "provider-origin JournalStore authority is unavailable"
        ) from error


def _load_origin_events(
    store: JournalStore,
    identity: object,
    attempt_id: str,
    _scope=journal_store_authority_scope,
    _load=JournalStore.load_events,
):
    try:
        with _scope(store, identity):
            return _load(store, _AGGREGATE_TYPE, attempt_id)
    except (TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginError(
            "provider-origin JournalStore authority changed during read"
        ) from error


def _append_origin_event(
    store: JournalStore,
    identity: object,
    event: dict[str, object],
    _scope=journal_store_authority_scope,
    _append=JournalStore.append_event,
) -> None:
    try:
        with _scope(store, identity):
            _append(store, event)
    except (TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginError(
            "provider-origin JournalStore authority changed during append"
        ) from error


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value:
        raise ProviderOriginError(f"{name} must be exact non-empty text")
    if value != value.strip():
        raise ProviderOriginError(f"{name} must be canonical text")
    return value


def _utc_text(value: object, *, name: str) -> str:
    # An exact datetime can still carry a caller-defined tzinfo subclass.
    # Calling utcoffset()/astimezone() on that object would execute arbitrary
    # caller code on an authority-bearing persistence ingress.  Keep this
    # bounded root deterministic by admitting only the exact stdlib timezone
    # implementation (UTC or fixed offsets) before invoking timezone methods.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderOriginError(
            f"{name} must use exact datetime with exact datetime.timezone tzinfo"
        )
    if value.utcoffset() is None:
        raise ProviderOriginError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, *, name: str) -> datetime:
    text = _exact_text(value, name=name)
    if not text.endswith("Z"):
        raise ProviderOriginError(f"{name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderOriginError(f"{name} must be canonical UTC text") from error
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ProviderOriginError(f"{name} must be canonical UTC text")
    return parsed


def _query_snapshot(
    query_binding: AuthenticatedReadQueryBinding,
    _require_query_authority=_require_authenticated_read_query_binding_authority,
) -> dict[str, object]:
    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise ProviderOriginError(
            "query_binding must be exact AuthenticatedReadQueryBinding"
        )
    try:
        _require_query_authority(query_binding)
    except ProviderCoreError as error:
        raise ProviderOriginError(
            "authenticated-read binding lacks canonical preparation authority"
        ) from error
    state = vars(query_binding)
    required = (
        "provider_id",
        "account_id",
        "entity_id",
        "environment",
        "provider_environment",
        "capability_snapshot_id",
        "instrument_version",
        "surface",
        "endpoint",
        "query",
        "prepared_at",
        "permission_scope",
        "query_digest",
    )
    if any(name not in state for name in required):
        raise ProviderOriginError("authenticated-read binding state is incomplete")
    for name in (
        "provider_id",
        "account_id",
        "entity_id",
        "environment",
        "provider_environment",
        "capability_snapshot_id",
        "instrument_version",
        "endpoint",
        "prepared_at",
        "permission_scope",
        "query_digest",
    ):
        if type(state[name]) is not str or not state[name]:
            raise ProviderOriginError(
                f"authenticated-read {name} must be exact non-empty text"
            )
    surface = state["surface"]
    if type(surface) is not Surface:
        raise ProviderOriginError("authenticated-read surface must be exact Surface")
    query = state["query"]
    if type(query) is not MappingProxyType:
        raise ProviderOriginError(
            "authenticated-read query must be immutable canonical mapping"
        )
    query_values: dict[str, str] = {}
    for key, value in query.items():
        if type(key) is not str or type(value) is not str:
            raise ProviderOriginError(
                "authenticated-read query keys and values must be exact strings"
            )
        query_values[key] = value
    _parse_utc_text(state["prepared_at"], name="prepared_at")
    material = {
        "provider_id": state["provider_id"],
        "account_id": state["account_id"],
        "entity_id": state["entity_id"],
        "environment": state["environment"],
        "provider_environment": state["provider_environment"],
        "capability_snapshot_id": state["capability_snapshot_id"],
        "instrument_version": state["instrument_version"],
        "surface": surface.value,
        "endpoint": state["endpoint"],
        "query": query_values,
        "prepared_at": state["prepared_at"],
        "permission_scope": state["permission_scope"],
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    expected_digest = "sha256:" + sha256(encoded).hexdigest()
    if state["query_digest"] != expected_digest:
        raise ProviderOriginError(
            "authenticated-read query digest conflicts with exact binding state"
        )
    if _SHA256_RE.fullmatch(state["query_digest"]) is None:
        raise ProviderOriginError("authenticated-read query digest is non-canonical")
    return {**material, "query_digest": state["query_digest"]}


def _require_origin_event(
    event: object,
    *,
    attempt_id: str,
    event_type: str,
    aggregate_version: int,
    payload_keys: frozenset[str],
) -> dict[str, object]:
    if type(event) is not dict or set(event) != _EVENT_KEYS:
        raise ProviderOriginError(
            "provider-origin journal event schema is not exact"
        )
    expected_event_id = (
        attempt_id
        + (":prepared" if event_type == _PREPARED_EVENT else ":observed")
    )
    if (
        event.get("event_id") != expected_event_id
        or event.get("event_type") != event_type
        or event.get("aggregate_type") != _AGGREGATE_TYPE
        or event.get("aggregate_id") != attempt_id
        or event.get("aggregate_version") != aggregate_version
    ):
        raise ProviderOriginError(
            "provider-origin journal event identity or chronology is invalid"
        )
    payload = event.get("payload")
    if type(payload) is not dict or set(payload) != payload_keys:
        raise ProviderOriginError(
            "provider-origin journal payload schema is not exact"
        )
    if event.get("payload_hash") != payload_digest(payload):
        raise ProviderOriginError(
            "provider-origin payload digest conflicts with durable payload"
        )
    _parse_utc_text(event.get("committed_at"), name="committed_at")
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise ProviderOriginError(
            "provider-origin journal sequence must be an exact positive integer"
        )
    return payload


def _require_snapshot(
    payload: Mapping[str, object],
    query_binding: AuthenticatedReadQueryBinding,
) -> dict[str, object]:
    if type(payload) is not dict:
        raise ProviderOriginError("provider-origin journal payload must be exact object")
    expected = _query_snapshot(query_binding)
    stored = payload.get("query")
    if type(stored) is not dict or stored != expected:
        raise ProviderOriginError(
            "durable provider-origin query does not match exact query binding"
        )
    return expected


def _evidence_ref(
    *,
    evidence_class: str,
    attempt_id: str,
    prepared_event_id: str,
    observed_event_id: str,
    query_digest: str,
    endpoint: str,
    provider_environment: str,
    http_status: int,
    response_sha256: str,
    observed_at: str,
    transport_identity: str,
    network_policy_identity: str,
    journal_sequence: int,
) -> str:
    if evidence_class == _PROVIDER_ORIGIN_KIND:
        prefix = "provider-origin:sha256:"
    elif evidence_class == _TEST_INJECTED_KIND:
        prefix = "provider-test:sha256:"
    else:
        raise ProviderOriginError("unsupported provider response evidence class")
    material = {
        "evidence_class": evidence_class,
        "attempt_id": attempt_id,
        "prepared_event_id": prepared_event_id,
        "observed_event_id": observed_event_id,
        "query_digest": query_digest,
        "endpoint": endpoint,
        "provider_environment": provider_environment,
        "http_status": http_status,
        "response_sha256": response_sha256,
        "observed_at": observed_at,
        "transport_identity": transport_identity,
        "network_policy_identity": network_policy_identity,
        "journal_sequence": journal_sequence,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return prefix + sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AuthenticatedReadResponseBinding:
    """Journal-derived exact provider response authority."""

    attempt_id: str
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    capability_snapshot_id: str
    query_digest: str
    endpoint: str
    prepared_event_id: str
    observed_event_id: str
    observed_at: str
    http_status: int
    response_sha256: str
    response_bytes: bytes
    transport_identity: str
    network_policy_identity: str
    evidence_class: str
    origin_ref: str
    journal_sequence: int
    _binding_token: InitVar[object | None] = None

    def __post_init__(self, _binding_token: object | None) -> None:
        if _binding_token is not _BINDING_TOKEN:
            raise ProviderOriginError(
                "authenticated read response binding must come from durable origin journal"
            )
        for name in (
            "attempt_id",
            "provider_id",
            "account_id",
            "environment",
            "provider_environment",
            "capability_snapshot_id",
            "query_digest",
            "endpoint",
            "prepared_event_id",
            "observed_event_id",
            "observed_at",
            "response_sha256",
            "transport_identity",
            "network_policy_identity",
            "origin_ref",
        ):
            _exact_text(getattr(self, name), name=name)
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            raise ProviderOriginError("http_status must be exact integer 100..599")
        if type(self.response_bytes) is not bytes or not self.response_bytes:
            raise ProviderOriginError("response_bytes must be exact non-empty bytes")
        try:
            require_provider_response_bytes(
                self.response_bytes,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "response_bytes exceed provider response authority budget"
            ) from error
        digest = "sha256:" + sha256(self.response_bytes).hexdigest()
        if self.response_sha256 != digest or _SHA256_RE.fullmatch(digest) is None:
            raise ProviderOriginError("response_sha256 conflicts with exact response bytes")
        if _SHA256_RE.fullmatch(self.query_digest) is None:
            raise ProviderOriginError("query_digest must be canonical")
        if _SHA256_RE.fullmatch(self.network_policy_identity) is None:
            raise ProviderOriginError("network_policy_identity must be canonical SHA-256")
        if self.evidence_class not in {
            _PROVIDER_ORIGIN_KIND,
            _TEST_INJECTED_KIND,
        }:
            raise ProviderOriginError("response evidence_class is unsupported")
        expected_ref_pattern = (
            _PROVIDER_ORIGIN_REF_RE
            if self.evidence_class == _PROVIDER_ORIGIN_KIND
            else _TEST_INJECTED_REF_RE
        )
        if expected_ref_pattern.fullmatch(self.origin_ref) is None:
            raise ProviderOriginError(
                "origin_ref prefix does not match response evidence class"
            )
        _parse_utc_text(self.observed_at, name="observed_at")
        if type(self.journal_sequence) is not int or self.journal_sequence <= 0:
            raise ProviderOriginError(
                "journal_sequence must be an exact positive integer"
            )
        expected_origin_ref = _evidence_ref(
            evidence_class=self.evidence_class,
            attempt_id=self.attempt_id,
            prepared_event_id=self.prepared_event_id,
            observed_event_id=self.observed_event_id,
            query_digest=self.query_digest,
            endpoint=self.endpoint,
            provider_environment=self.provider_environment,
            http_status=self.http_status,
            response_sha256=self.response_sha256,
            observed_at=self.observed_at,
            transport_identity=self.transport_identity,
            network_policy_identity=self.network_policy_identity,
            journal_sequence=self.journal_sequence,
        )
        if self.origin_ref != expected_origin_ref:
            raise ProviderOriginError(
                "origin_ref conflicts with complete durable provider response subject"
            )

    def require_provider_origin(self) -> None:
        # No same-process Python binding is an independently authenticated
        # provider-wire issuer. Do not weaken this to a field/token predicate:
        # frozen dataclasses/private names are mutable/importable in-process.
        raise ProviderOriginError(
            "independently authenticated provider-wire origin is not established "
            "by any in-process response binding"
        )


@dataclass(frozen=True)
class ProviderOriginObservation:
    """Financially distinguishable observation carrying durable origin authority."""

    response_binding: AuthenticatedReadResponseBinding
    observation: ProviderResponseObservation
    _observation_token: InitVar[object | None] = None

    def __post_init__(self, _observation_token: object | None) -> None:
        # This bounded root has no independently authenticated external issuer.
        # Do not let a private Python token, a rebound method, or a durable local
        # row become financial PROVIDER_ORIGIN authority.
        raise ProviderOriginError(
            "independently authenticated provider-wire issuer is not integrated"
        )

    @property
    def payload(self) -> object:
        return self.observation.payload

    @property
    def origin_ref(self) -> str:
        return self.response_binding.origin_ref

    @property
    def provider_id(self) -> str:
        return self.response_binding.provider_id

    @property
    def account_id(self) -> str:
        return self.response_binding.account_id

    @property
    def environment(self) -> str:
        return self.response_binding.environment

    @property
    def provider_environment(self) -> str:
        return self.response_binding.provider_environment


class ProviderOriginJournal:
    """Durable authority for one canonical JournalStore generation."""

    def __init__(self, store: JournalStore) -> None:
        self._store = store
        self._store_identity = _require_origin_journal_authority(store)

    def _require_store(self) -> tuple[JournalStore, object]:
        store = self._store
        current = _require_origin_journal_authority(store)
        if current != self._store_identity:
            raise ProviderOriginError("provider-origin JournalStore generation changed")
        return store, current

    @staticmethod
    def _event(
        *,
        event_id: str,
        event_type: str,
        attempt_id: str,
        aggregate_version: int,
        payload: dict[str, object],
        committed_at: str,
    ) -> dict[str, object]:
        return {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": attempt_id,
            "aggregate_version": str(aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }

    def prepare(
        self,
        query_binding: AuthenticatedReadQueryBinding,
        *,
        transport_identity: str,
        network_policy_identity: str,
        recorded_at: datetime,
    ) -> str:
        snapshot = _query_snapshot(query_binding)
        transport = _exact_text(transport_identity, name="transport_identity")
        policy = _exact_text(network_policy_identity, name="network_policy_identity")
        if _SHA256_RE.fullmatch(policy) is None:
            raise ProviderOriginError(
                "network_policy_identity must be canonical SHA-256"
            )
        committed_at = _utc_text(recorded_at, name="recorded_at")
        if _parse_utc_text(committed_at, name="recorded_at") < _parse_utc_text(
            snapshot["prepared_at"], name="prepared_at"
        ):
            raise ProviderOriginError(
                "provider-origin prepare cannot precede query preparation"
            )
        attempt_id = "provider-read:" + uuid4().hex
        event_id = attempt_id + ":prepared"
        payload = {
            "origin_kind": _PENDING_KIND,
            "query": snapshot,
            "transport_identity": transport,
            "network_policy_identity": policy,
        }
        store, identity = self._require_store()
        _append_origin_event(
            store,
            identity,
            self._event(
                event_id=event_id,
                event_type=_PREPARED_EVENT,
                attempt_id=attempt_id,
                aggregate_version=1,
                payload=payload,
                committed_at=committed_at,
            ),
        )
        return attempt_id

    def _record_provider_origin(
        self,
        attempt_id: str,
        query_binding: AuthenticatedReadQueryBinding,
        *,
        http_status: int,
        response_bytes: bytes,
        observed_at: datetime,
    ) -> AuthenticatedReadResponseBinding:
        """Fail closed until an independently authenticated wire issuer exists."""

        raise ProviderOriginError(
            "independent external provider-wire issuer is not integrated"
        )

    def _record_test_injected_response(
        self,
        attempt_id: str,
        query_binding: AuthenticatedReadQueryBinding,
        *,
        http_status: int,
        response_bytes: bytes,
        observed_at: datetime,
    ) -> AuthenticatedReadResponseBinding:
        """Record exact local test bytes without claiming provider origin."""

        attempt = _exact_text(attempt_id, name="attempt_id")
        if type(http_status) is not int or not 100 <= http_status <= 599:
            raise ProviderOriginError("http_status must be exact integer 100..599")
        if type(response_bytes) is not bytes or not response_bytes:
            raise ProviderOriginError("response_bytes must be exact non-empty bytes")
        try:
            raw = require_provider_response_bytes(
                response_bytes,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "response_bytes exceed provider response authority budget"
            ) from error
        observed_text = _utc_text(observed_at, name="observed_at")
        store, identity = self._require_store()
        events = _load_origin_events(store, identity, attempt)
        if len(events) != 1:
            raise ProviderOriginError(
                "provider-origin response requires one exact durable Prepared event"
            )
        prepared = events[0]
        payload = _require_origin_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        expected = _require_snapshot(payload, query_binding)
        prepared_at = _parse_utc_text(
            prepared.get("committed_at"), name="prepared committed_at"
        )
        observed_point = _parse_utc_text(observed_text, name="observed_at")
        if observed_point < prepared_at:
            raise ProviderOriginError(
                "provider response observation cannot precede durable prepare"
            )
        response_digest = "sha256:" + sha256(raw).hexdigest()
        observed_event_id = attempt + ":observed"
        observed_payload = {
            "origin_kind": _TEST_INJECTED_KIND,
            "prepared_event_id": prepared.get("event_id"),
            "query_digest": expected["query_digest"],
            "transport_identity": payload["transport_identity"],
            "network_policy_identity": payload["network_policy_identity"],
            "http_status": http_status,
            "response_sha256": response_digest,
            "response_base64": base64.b64encode(raw).decode("ascii"),
            "observed_at": observed_text,
        }
        _append_origin_event(
            store,
            identity,
            self._event(
                event_id=observed_event_id,
                event_type=_OBSERVED_EVENT,
                attempt_id=attempt,
                aggregate_version=2,
                payload=observed_payload,
                committed_at=observed_text,
            ),
        )
        return self.load_response_binding(attempt, query_binding)

    def load_response_binding(
        self,
        attempt_id: str,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> AuthenticatedReadResponseBinding:
        attempt = _exact_text(attempt_id, name="attempt_id")
        expected = _query_snapshot(query_binding)
        store, identity = self._require_store()
        events = _load_origin_events(store, identity, attempt)
        if len(events) != 2:
            raise ProviderOriginError(
                "provider-origin response is incomplete; Prepared and Observed are required"
            )
        prepared, observed = events
        prepared_payload = _require_origin_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        _require_snapshot(prepared_payload, query_binding)
        observed_payload = _require_origin_event(
            observed,
            attempt_id=attempt,
            event_type=_OBSERVED_EVENT,
            aggregate_version=2,
            payload_keys=_OBSERVED_PAYLOAD_KEYS,
        )
        if prepared_payload.get("origin_kind") != _PENDING_KIND:
            raise ProviderOriginError(
                "provider response Prepared event must remain pending wire evidence"
            )
        evidence_class = observed_payload.get("origin_kind")
        if evidence_class == _PROVIDER_ORIGIN_KIND:
            raise ProviderOriginError(
                "independent external provider-wire attestation verifier is not integrated"
            )
        if evidence_class != _TEST_INJECTED_KIND:
            raise ProviderOriginError(
                "provider response evidence class is unsupported"
            )
        if (
            observed_payload.get("prepared_event_id") != prepared.get("event_id")
            or observed_payload.get("query_digest") != expected["query_digest"]
            or observed_payload.get("transport_identity")
            != prepared_payload.get("transport_identity")
            or observed_payload.get("network_policy_identity")
            != prepared_payload.get("network_policy_identity")
        ):
            raise ProviderOriginError(
                "provider response Observed event conflicts with durable Prepared scope"
            )
        status = observed_payload.get("http_status")
        if type(status) is not int or not 100 <= status <= 599:
            raise ProviderOriginError("durable provider HTTP status is invalid")
        encoded = observed_payload.get("response_base64")
        if type(encoded) is not str or not encoded:
            raise ProviderOriginError("durable provider response bytes are missing")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as error:
            raise ProviderOriginError(
                "durable provider response bytes are not canonical base64"
            ) from error
        if type(raw) is not bytes or not raw:
            raise ProviderOriginError("durable provider response bytes are empty")
        try:
            require_provider_response_bytes(
                raw, max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "durable provider response exceeds authority budget"
            ) from error
        response_digest = "sha256:" + sha256(raw).hexdigest()
        if observed_payload.get("response_sha256") != response_digest:
            raise ProviderOriginError(
                "durable provider response digest conflicts with exact bytes"
            )
        observed_text = observed_payload.get("observed_at")
        _parse_utc_text(observed_text, name="observed_at")
        if observed.get("committed_at") != observed_text:
            raise ProviderOriginError(
                "provider-origin observed event timestamp conflicts with payload"
            )
        journal_sequence = observed.get("journal_sequence")
        if type(journal_sequence) is not int or journal_sequence <= 0:
            raise ProviderOriginError(
                "provider-origin observed journal sequence is invalid"
            )
        transport_identity = _exact_text(
            observed_payload.get("transport_identity"),
            name="transport_identity",
        )
        network_policy_identity = _exact_text(
            observed_payload.get("network_policy_identity"),
            name="network_policy_identity",
        )
        if _SHA256_RE.fullmatch(network_policy_identity) is None:
            raise ProviderOriginError(
                "durable network_policy_identity must be canonical SHA-256"
            )
        origin_ref = _evidence_ref(
            evidence_class=evidence_class,
            attempt_id=attempt,
            prepared_event_id=str(prepared.get("event_id")),
            observed_event_id=str(observed.get("event_id")),
            query_digest=expected["query_digest"],
            endpoint=expected["endpoint"],
            provider_environment=expected["provider_environment"],
            http_status=status,
            response_sha256=response_digest,
            observed_at=observed_text,
            transport_identity=transport_identity,
            network_policy_identity=network_policy_identity,
            journal_sequence=journal_sequence,
        )
        return AuthenticatedReadResponseBinding(
            attempt_id=attempt,
            provider_id=expected["provider_id"],
            account_id=expected["account_id"],
            environment=expected["environment"],
            provider_environment=expected["provider_environment"],
            capability_snapshot_id=expected["capability_snapshot_id"],
            query_digest=expected["query_digest"],
            endpoint=expected["endpoint"],
            prepared_event_id=prepared["event_id"],
            observed_event_id=observed["event_id"],
            observed_at=observed_text,
            http_status=status,
            response_sha256=response_digest,
            response_bytes=raw,
            transport_identity=transport_identity,
            network_policy_identity=network_policy_identity,
            evidence_class=evidence_class,
            origin_ref=origin_ref,
            journal_sequence=journal_sequence,
            _binding_token=_BINDING_TOKEN,
        )


def observe_provider_origin_json_response(
    *,
    response_binding: AuthenticatedReadResponseBinding,
    query_binding: AuthenticatedReadQueryBinding,
    accepted_success_statuses: frozenset[int],
) -> ProviderOriginObservation:
    """Fail closed until an independently authenticated external issuer exists."""

    # The local binding can prove deterministic bytes/scope/restart integrity,
    # but no ordinary in-process Python predicate can establish provider origin.
    # In particular, do not delegate this terminal decision to a mutable class
    # method such as AuthenticatedReadResponseBinding.require_provider_origin.
    raise ProviderOriginError(
        "independently authenticated provider-wire issuer is not integrated"
    )

def observe_test_injected_json_response(
    *,
    response_binding: AuthenticatedReadResponseBinding,
    query_binding: AuthenticatedReadQueryBinding,
    accepted_success_statuses: frozenset[int],
) -> ProviderResponseObservation:
    """Parse exact TEST_INJECTED replay bytes without financial origin authority."""

    if type(response_binding) is not AuthenticatedReadResponseBinding:
        raise ProviderOriginError(
            "response_binding must be exact AuthenticatedReadResponseBinding"
        )
    if response_binding.evidence_class != _TEST_INJECTED_KIND:
        raise ProviderOriginError(
            "test-injected observer requires TEST_INJECTED response evidence"
        )
    expected = _query_snapshot(query_binding)
    if (
        response_binding.query_digest != expected["query_digest"]
        or response_binding.provider_id != expected["provider_id"]
        or response_binding.account_id != expected["account_id"]
        or response_binding.environment != expected["environment"]
        or response_binding.provider_environment != expected["provider_environment"]
        or response_binding.capability_snapshot_id
        != expected["capability_snapshot_id"]
        or response_binding.endpoint != expected["endpoint"]
    ):
        raise ProviderOriginError(
            "durable provider response binding does not match exact query"
        )
    if (
        type(accepted_success_statuses) is not frozenset
        or not accepted_success_statuses
        or any(
            type(status) is not int or not 200 <= status <= 299
            for status in accepted_success_statuses
        )
    ):
        raise ProviderOriginError(
            "accepted_success_statuses must be an exact non-empty frozenset of 2xx integers"
        )
    if response_binding.http_status not in accepted_success_statuses:
        raise ProviderOriginError(
            "test-injected HTTP status is outside the accepted endpoint policy"
        )
    observed_at = _parse_utc_text(
        response_binding.observed_at, name="observed_at"
    )
    observation = observe_authenticated_json_response(
        query_binding=query_binding,
        http_status=response_binding.http_status,
        response_bytes=response_binding.response_bytes,
        observed_at=observed_at,
    )
    if type(observation) is not ProviderResponseObservation:
        raise ProviderOriginError(
            "provider core returned non-canonical response observation"
        )
    return observation


# Host-attested durability bridge.
#
# This intentionally shares the canonical authenticated_provider_read aggregate
# and the selected JournalStore backing object. It does not mint PROVIDER_ORIGIN:
# a Host signature authenticates the Host issuer, not the provider wire path.
# Positive financial promotion remains fail-closed until the qualified direct
# provider transport is cryptographically/compositionally bound to this issuer.
_HOST_ATTESTED_PENDING_KIND = "HOST_ATTESTED_PENDING"
_HOST_ATTESTED_OBSERVED_KIND = "HOST_ATTESTED_OBSERVED"
_HOST_JOURNAL_IDENTITY_SCHEMA = "autotrade-provider-origin-journal-identity:v1"
_HOST_PREPARED_DURABILITY_SCHEMA = "autotrade-provider-read-durable-prepared:v1"
_HOST_OBSERVED_DURABILITY_SCHEMA = "autotrade-provider-read-durable-observed:v1"
_HOST_PREPARED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "query",
        "transport_identity",
        "network_policy_identity",
        "host_prepared_attestation",
    }
)
_HOST_OBSERVED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "prepared_event_id",
        "query_digest",
        "transport_identity",
        "network_policy_identity",
        "http_status",
        "response_sha256",
        "response_base64",
        "observed_at",
        "host_provider_receipt",
        "prepared_receipt_identity",
    }
)


@dataclass(frozen=True, slots=True)
class HostAuthenticatedReadExpectedScope:
    """Independent exact pins not carried by AuthenticatedReadQueryBinding.

    Construction of this DTO is not authority. The bridge only cross-binds these
    caller-supplied expectations to cryptographically verified Host material and
    never promotes them to PROVIDER_ORIGIN on their own.
    """

    data_entitlement: str
    endpoint_rule_identity: str
    credential_handle_id: str
    credential_generation: int
    qualification_id: str
    qualification_build_id: str
    adapter_build_identity: str
    network_policy_identity: str
    transport_identity: str


def _require_host_expected_scope(
    value: object,
) -> HostAuthenticatedReadExpectedScope:
    if type(value) is not HostAuthenticatedReadExpectedScope:
        raise ProviderOriginError(
            "Host expected scope must be exact HostAuthenticatedReadExpectedScope"
        )
    for name in (
        "data_entitlement",
        "credential_handle_id",
        "qualification_id",
        "qualification_build_id",
        "adapter_build_identity",
        "transport_identity",
    ):
        _exact_text(object.__getattribute__(value, name), name=name)
    endpoint_rule = _exact_text(
        object.__getattribute__(value, "endpoint_rule_identity"),
        name="endpoint_rule_identity",
    )
    network_policy = _exact_text(
        object.__getattribute__(value, "network_policy_identity"),
        name="network_policy_identity",
    )
    if _SHA256_RE.fullmatch(endpoint_rule) is None:
        raise ProviderOriginError(
            "endpoint_rule_identity must be canonical SHA-256"
        )
    if _SHA256_RE.fullmatch(network_policy) is None:
        raise ProviderOriginError(
            "network_policy_identity must be canonical SHA-256"
        )
    generation = object.__getattribute__(value, "credential_generation")
    if type(generation) is not int or generation <= 0:
        raise ProviderOriginError(
            "credential_generation must be an exact positive integer"
        )
    return value


def _host_canonical_utc(value: object, *, name: str) -> str:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderOriginError(
            f"{name} must use exact datetime with exact datetime.timezone tzinfo"
        )
    if value.utcoffset() is None:
        raise ProviderOriginError(f"{name} must be timezone-aware")
    point = value.astimezone(timezone.utc)
    return (
        f"{point.year:04d}-{point.month:02d}-{point.day:02d}"
        f"T{point.hour:02d}:{point.minute:02d}:{point.second:02d}."
        f"{point.microsecond:06d}0Z"
    )


def _host_journal_identity(
    identity: object,
) -> str:
    from .provider_host_attestation import canonical_host_material
    from .store_identity import require_exact_journal_store_identity

    exact = require_exact_journal_store_identity(
        identity,
        subject="Host bridge journal identity",
    )

    def number(value: int | None) -> str:
        if value is None:
            return ""
        if type(value) is not int:
            raise ProviderOriginError(
                "Host bridge journal identity contains non-integer identity material"
            )
        return str(value)

    material = canonical_host_material(
        _HOST_JOURNAL_IDENTITY_SCHEMA,
        exact.identity_source,
        exact.canonical_path,
        number(exact.filesystem_device),
        number(exact.filesystem_inode),
        number(exact.windows_volume_serial),
        number(exact.windows_file_index_high),
        number(exact.windows_file_index_low),
    )
    return "sha256:" + sha256(material).hexdigest()


def _host_event(
    *,
    event_id: str,
    event_type: str,
    attempt_id: str,
    aggregate_version: int,
    payload: dict[str, object],
    committed_at: str,
    _payload_digest=payload_digest,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": attempt_id,
        "aggregate_version": str(aggregate_version),
        "payload": payload,
        "payload_hash": _payload_digest(payload),
        "committed_at": committed_at,
    }


def _require_host_event(
    event: object,
    *,
    attempt_id: str,
    event_type: str,
    aggregate_version: int,
    payload_keys: frozenset[str],
) -> dict[str, object]:
    from .provider_host_attestation import _host_utc_key

    if type(event) is not dict or set(event) != _EVENT_KEYS:
        raise ProviderOriginError(
            "Host-attested provider journal event schema is not exact"
        )
    expected_event_id = (
        attempt_id
        + (":prepared" if event_type == _PREPARED_EVENT else ":observed")
    )
    if (
        event.get("event_id") != expected_event_id
        or event.get("event_type") != event_type
        or event.get("aggregate_type") != _AGGREGATE_TYPE
        or event.get("aggregate_id") != attempt_id
        or event.get("aggregate_version") != aggregate_version
    ):
        raise ProviderOriginError(
            "Host-attested provider journal event identity or chronology is invalid"
        )
    payload = event.get("payload")
    if type(payload) is not dict or set(payload) != payload_keys:
        raise ProviderOriginError(
            "Host-attested provider journal payload schema is not exact"
        )
    if event.get("payload_hash") != payload_digest(payload):
        raise ProviderOriginError(
            "Host-attested provider payload digest conflicts with durable payload"
        )
    committed = event.get("committed_at")
    _host_utc_key(committed, name="Host-attested committed_at")
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise ProviderOriginError(
            "Host-attested journal sequence must be an exact positive integer"
        )
    return payload


def _host_prepared_receipt_payload(receipt: object) -> dict[str, object]:
    from .provider_host_attestation import HostPreparedDurabilityReceipt

    if type(receipt) is not HostPreparedDurabilityReceipt:
        raise ProviderOriginError(
            "prepared durability receipt must be exact HostPreparedDurabilityReceipt"
        )
    return {
        "schema": _HOST_PREPARED_DURABILITY_SCHEMA,
        "issuer_session_identity": receipt.issuer_session_identity,
        "read_attempt_id": receipt.read_attempt_id,
        "read_attempt_binding_sha256": receipt.read_attempt_binding_sha256,
        "query_digest": receipt.query_digest,
        "journal_identity": receipt.journal_identity,
        "prepared_event_id": receipt.prepared_event_id,
        "journal_sequence": receipt.journal_sequence,
        "committed_at_utc": receipt.committed_at_utc,
        "receipt_identity": receipt.receipt_identity,
    }


def _host_observed_receipt_payload(receipt: object) -> dict[str, object]:
    from .provider_host_attestation import HostObservedDurabilityReceipt

    if type(receipt) is not HostObservedDurabilityReceipt:
        raise ProviderOriginError(
            "observed durability receipt must be exact HostObservedDurabilityReceipt"
        )
    return {
        "schema": _HOST_OBSERVED_DURABILITY_SCHEMA,
        "issuer_session_identity": receipt.issuer_session_identity,
        "read_attempt_id": receipt.read_attempt_id,
        "read_attempt_binding_sha256": receipt.read_attempt_binding_sha256,
        "provider_receipt_sha256": receipt.provider_receipt_sha256,
        "response_sha256": receipt.response_sha256,
        "http_status": receipt.http_status,
        "observed_at_utc": receipt.observed_at_utc,
        "journal_identity": receipt.journal_identity,
        "prepared_receipt_identity": receipt.prepared_receipt_identity,
        "prepared_event_id": receipt.prepared_event_id,
        "prepared_journal_sequence": receipt.prepared_journal_sequence,
        "observed_event_id": receipt.observed_event_id,
        "observed_journal_sequence": receipt.observed_journal_sequence,
        "committed_at_utc": receipt.committed_at_utc,
        "receipt_identity": receipt.receipt_identity,
    }


class HostAuthenticatedReadJournalBridge:
    """Durability callbacks for signed Host authenticated-read evidence.

    The bridge writes only to the already-selected canonical JournalStore. It
    returns the exact durability receipt material expected by AutoTrade.Host and
    can reconstruct/verify the full Observed envelope after process restart.

    This is deliberately not the provider-origin promotion boundary. A Host
    signature proves which Host issuer produced the read evidence; real provider
    wire provenance still has to be composed and qualified separately.
    """

    def __init__(self, store: JournalStore) -> None:
        self._store = store
        self._store_identity = _require_origin_journal_authority(store)
        self._journal_identity = _host_journal_identity(self._store_identity)

    @property
    def journal_identity(self) -> str:
        store, identity = self._require_store()
        current = _host_journal_identity(identity)
        if current != self._journal_identity:
            raise ProviderOriginError(
                "Host bridge canonical journal identity changed"
            )
        return current

    def _require_store(self) -> tuple[JournalStore, object]:
        store = self._store
        current = _require_origin_journal_authority(store)
        if current != self._store_identity:
            raise ProviderOriginError(
                "Host bridge JournalStore generation changed"
            )
        return store, current

    @staticmethod
    def _require_subject_scope(
        verified: object,
        query_binding: AuthenticatedReadQueryBinding,
        expected_scope: HostAuthenticatedReadExpectedScope,
    ) -> dict[str, object]:
        from .provider_host_attestation import (
            VerifiedHostPreparedAttestation,
            _host_utc_key,
        )

        if type(verified) is not VerifiedHostPreparedAttestation:
            raise ProviderOriginError(
                "Host Prepared verifier returned non-canonical evidence"
            )
        expected = _query_snapshot(query_binding)
        pins = _require_host_expected_scope(expected_scope)
        subject = verified.attempt.subject
        surface = expected["surface"]
        if type(surface) is not Surface:
            raise ProviderOriginError(
                "authenticated-read surface lost canonical authority"
            )
        if (
            subject.provider_id != expected["provider_id"]
            or subject.account_id != expected["account_id"]
            or subject.entity_id != expected["entity_id"]
            or subject.runtime_environment != expected["environment"]
            or subject.provider_environment != expected["provider_environment"]
            or subject.endpoint != expected["endpoint"]
            or subject.surface != surface.value
            or subject.permission_scope != expected["permission_scope"]
            or subject.instrument_version != expected["instrument_version"]
            or subject.query_digest != expected["query_digest"]
            or subject.capability_id != expected["capability_snapshot_id"]
            or subject.data_entitlement != pins.data_entitlement
            or subject.endpoint_rule_identity != pins.endpoint_rule_identity
            or subject.credential_handle_id != pins.credential_handle_id
            or subject.credential_generation != pins.credential_generation
            or subject.qualification_id != pins.qualification_id
            or subject.qualification_build_id != pins.qualification_build_id
            or subject.adapter_build_identity != pins.adapter_build_identity
            or subject.network_policy_identity != pins.network_policy_identity
            or subject.transport_identity != pins.transport_identity
        ):
            raise ProviderOriginError(
                "signed Host authenticated-read subject conflicts with "
                "independently selected scope"
            )
        prepared = _parse_utc_text(
            expected["prepared_at"],
            name="query prepared_at",
        )
        query_prepared_key = (
            prepared.year,
            prepared.month,
            prepared.day,
            prepared.hour,
            prepared.minute,
            prepared.second,
            prepared.microsecond * 10,
        )
        host_prepared_key = _host_utc_key(
            verified.attempt.prepared_at_utc,
            name="Host prepared_at_utc",
        )
        if host_prepared_key < query_prepared_key:
            raise ProviderOriginError(
                "signed Host read attempt predates canonical query preparation"
            )
        return expected

    @staticmethod
    def _prepared_receipt(
        *,
        verified: object,
        journal_identity: str,
        prepared_event: dict[str, object],
    ):
        from .provider_host_attestation import (
            HostPreparedDurabilityReceipt,
            VerifiedHostPreparedAttestation,
            canonical_host_material,
        )

        if type(verified) is not VerifiedHostPreparedAttestation:
            raise ProviderOriginError(
                "Host Prepared verifier returned non-canonical evidence"
            )
        attempt = verified.attempt
        sequence = prepared_event.get("journal_sequence")
        committed = prepared_event.get("committed_at")
        if type(sequence) is not int or sequence <= 0 or type(committed) is not str:
            raise ProviderOriginError(
                "durable Host Prepared event lacks canonical journal cut"
            )
        material = canonical_host_material(
            _HOST_PREPARED_DURABILITY_SCHEMA,
            verified.issuer_session.session_identity,
            attempt.read_attempt_id,
            attempt.binding_sha256,
            attempt.subject.query_digest,
            journal_identity,
            attempt.read_attempt_id + ":prepared",
            str(sequence),
            committed,
        )
        return HostPreparedDurabilityReceipt(
            issuer_session_identity=verified.issuer_session.session_identity,
            read_attempt_id=attempt.read_attempt_id,
            read_attempt_binding_sha256=attempt.binding_sha256,
            query_digest=attempt.subject.query_digest,
            journal_identity=journal_identity,
            prepared_event_id=attempt.read_attempt_id + ":prepared",
            journal_sequence=sequence,
            committed_at_utc=committed,
            receipt_identity=(
                "provider-read-durable-prepared:sha256:"
                + sha256(material).hexdigest()
            ),
        )

    def commit_prepared(
        self,
        prepared_envelope: object,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        expected_session_identity: str,
        expected_public_key_sha256: str,
        expected_scope: HostAuthenticatedReadExpectedScope,
        committed_at: datetime,
    ):
        from .provider_host_attestation import (
            HostProviderAttestationError,
            HostProviderAttestationUnavailable,
            _host_utc_key,
            verify_host_prepared_attestation,
        )

        expected = _query_snapshot(query_binding)
        _require_host_expected_scope(expected_scope)
        try:
            verified = verify_host_prepared_attestation(
                prepared_envelope,
                expected_session_identity=expected_session_identity,
                expected_public_key_sha256=expected_public_key_sha256,
                expected_query=expected["query"],
            )
        except (
            HostProviderAttestationError,
            HostProviderAttestationUnavailable,
        ) as error:
            raise ProviderOriginError(
                "Host Prepared attestation verification failed"
            ) from error
        expected = self._require_subject_scope(
            verified,
            query_binding,
            expected_scope,
        )
        committed = _host_canonical_utc(
            committed_at,
            name="Host Prepared committed_at",
        )
        if _host_utc_key(
            committed,
            name="Host Prepared committed_at",
        ) < _host_utc_key(
            verified.attempt.prepared_at_utc,
            name="Host prepared_at_utc",
        ):
            raise ProviderOriginError(
                "durable Host Prepared commit cannot predate signed attempt"
            )

        attempt_id = verified.attempt.read_attempt_id
        store, identity = self._require_store()
        events = _load_origin_events(store, identity, attempt_id)
        if len(events) > 1:
            raise ProviderOriginError(
                "Host Prepared replay found an already Observed read attempt"
            )
        if events:
            payload = _require_host_event(
                events[0],
                attempt_id=attempt_id,
                event_type=_PREPARED_EVENT,
                aggregate_version=1,
                payload_keys=_HOST_PREPARED_PAYLOAD_KEYS,
            )
            if (
                payload.get("origin_kind") != _HOST_ATTESTED_PENDING_KIND
                or payload.get("query") != expected
                or payload.get("transport_identity")
                != verified.attempt.subject.transport_identity
                or payload.get("network_policy_identity")
                != verified.attempt.subject.network_policy_identity
                or payload.get("host_prepared_attestation")
                != prepared_envelope
            ):
                raise ProviderOriginError(
                    "Host Prepared replay conflicts with durable signed scope"
                )
            return self._prepared_receipt(
                verified=verified,
                journal_identity=self.journal_identity,
                prepared_event=events[0],
            )

        payload = {
            "origin_kind": _HOST_ATTESTED_PENDING_KIND,
            "query": expected,
            "transport_identity": verified.attempt.subject.transport_identity,
            "network_policy_identity": verified.attempt.subject.network_policy_identity,
            "host_prepared_attestation": prepared_envelope,
        }
        _append_origin_event(
            store,
            identity,
            _host_event(
                event_id=attempt_id + ":prepared",
                event_type=_PREPARED_EVENT,
                attempt_id=attempt_id,
                aggregate_version=1,
                payload=payload,
                committed_at=committed,
            ),
        )
        events = _load_origin_events(store, identity, attempt_id)
        if len(events) != 1:
            raise ProviderOriginError(
                "Host Prepared commit did not produce one exact durable event"
            )
        _require_host_event(
            events[0],
            attempt_id=attempt_id,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_HOST_PREPARED_PAYLOAD_KEYS,
        )
        return self._prepared_receipt(
            verified=verified,
            journal_identity=self.journal_identity,
            prepared_event=events[0],
        )

    @staticmethod
    def _observed_receipt(
        *,
        verified_prepared: object,
        provider_receipt: object,
        prepared_receipt: object,
        journal_identity: str,
        observed_event: dict[str, object],
    ):
        from .provider_host_attestation import (
            HostAuthenticatedReadReceipt,
            HostObservedDurabilityReceipt,
            HostPreparedDurabilityReceipt,
            VerifiedHostPreparedAttestation,
            canonical_host_material,
        )

        if (
            type(verified_prepared) is not VerifiedHostPreparedAttestation
            or type(provider_receipt) is not HostAuthenticatedReadReceipt
            or type(prepared_receipt) is not HostPreparedDurabilityReceipt
        ):
            raise ProviderOriginError(
                "Host Observed durability inputs are non-canonical"
            )
        sequence = observed_event.get("journal_sequence")
        committed = observed_event.get("committed_at")
        if type(sequence) is not int or sequence <= 0 or type(committed) is not str:
            raise ProviderOriginError(
                "durable Host Observed event lacks canonical journal cut"
            )
        attempt = verified_prepared.attempt
        material = canonical_host_material(
            _HOST_OBSERVED_DURABILITY_SCHEMA,
            verified_prepared.issuer_session.session_identity,
            attempt.read_attempt_id,
            attempt.binding_sha256,
            provider_receipt.receipt_sha256,
            provider_receipt.response_sha256,
            str(provider_receipt.http_status),
            provider_receipt.observed_at_utc,
            journal_identity,
            prepared_receipt.receipt_identity,
            prepared_receipt.prepared_event_id,
            str(prepared_receipt.journal_sequence),
            attempt.read_attempt_id + ":observed",
            str(sequence),
            committed,
        )
        return HostObservedDurabilityReceipt(
            issuer_session_identity=verified_prepared.issuer_session.session_identity,
            read_attempt_id=attempt.read_attempt_id,
            read_attempt_binding_sha256=attempt.binding_sha256,
            provider_receipt_sha256=provider_receipt.receipt_sha256,
            response_sha256=provider_receipt.response_sha256,
            http_status=provider_receipt.http_status,
            observed_at_utc=provider_receipt.observed_at_utc,
            journal_identity=journal_identity,
            prepared_receipt_identity=prepared_receipt.receipt_identity,
            prepared_event_id=prepared_receipt.prepared_event_id,
            prepared_journal_sequence=prepared_receipt.journal_sequence,
            observed_event_id=attempt.read_attempt_id + ":observed",
            observed_journal_sequence=sequence,
            committed_at_utc=committed,
            receipt_identity=(
                "provider-read-durable-observed:sha256:"
                + sha256(material).hexdigest()
            ),
        )

    def commit_observed(
        self,
        prepared_envelope: object,
        provider_receipt_payload: object,
        prepared_durability_payload: object,
        response_bytes: bytes,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        expected_session_identity: str,
        expected_public_key_sha256: str,
        expected_scope: HostAuthenticatedReadExpectedScope,
        committed_at: datetime,
    ):
        from .provider_host_attestation import (
            HostProviderAttestationError,
            HostProviderAttestationUnavailable,
            _host_utc_key,
            _parse_prepared_durability,
            _parse_receipt,
            verify_host_prepared_attestation,
        )

        expected = _query_snapshot(query_binding)
        _require_host_expected_scope(expected_scope)
        try:
            verified = verify_host_prepared_attestation(
                prepared_envelope,
                expected_session_identity=expected_session_identity,
                expected_public_key_sha256=expected_public_key_sha256,
                expected_query=expected["query"],
            )
            provider_receipt = _parse_receipt(
                provider_receipt_payload,
                prepared=verified,
                response_bytes=response_bytes,
            )
            prepared_receipt = _parse_prepared_durability(
                prepared_durability_payload,
                prepared=verified,
                expected_journal_identity=self.journal_identity,
            )
        except (
            HostProviderAttestationError,
            HostProviderAttestationUnavailable,
        ) as error:
            raise ProviderOriginError(
                "Host Observed attestation component verification failed"
            ) from error
        expected = self._require_subject_scope(
            verified,
            query_binding,
            expected_scope,
        )

        attempt_id = verified.attempt.read_attempt_id
        store, identity = self._require_store()
        events = _load_origin_events(store, identity, attempt_id)
        if len(events) not in {1, 2}:
            raise ProviderOriginError(
                "Host Observed commit requires one durable Prepared event"
            )
        prepared_event = events[0]
        prepared_payload = _require_host_event(
            prepared_event,
            attempt_id=attempt_id,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_HOST_PREPARED_PAYLOAD_KEYS,
        )
        if (
            prepared_payload.get("origin_kind") != _HOST_ATTESTED_PENDING_KIND
            or prepared_payload.get("query") != expected
            or prepared_payload.get("host_prepared_attestation")
            != prepared_envelope
            or prepared_payload.get("transport_identity")
            != verified.attempt.subject.transport_identity
            or prepared_payload.get("network_policy_identity")
            != verified.attempt.subject.network_policy_identity
            or prepared_receipt.prepared_event_id
            != prepared_event.get("event_id")
            or prepared_receipt.journal_sequence
            != prepared_event.get("journal_sequence")
            or prepared_receipt.committed_at_utc
            != prepared_event.get("committed_at")
        ):
            raise ProviderOriginError(
                "Host Observed commit conflicts with durable Prepared cut"
            )

        committed = _host_canonical_utc(
            committed_at,
            name="Host Observed committed_at",
        )
        if _host_utc_key(
            committed,
            name="Host Observed committed_at",
        ) < _host_utc_key(
            provider_receipt.observed_at_utc,
            name="provider observed_at_utc",
        ):
            raise ProviderOriginError(
                "durable Host Observed commit cannot predate signed response"
            )

        encoded = base64.b64encode(response_bytes).decode("ascii")
        payload = {
            "origin_kind": _HOST_ATTESTED_OBSERVED_KIND,
            "prepared_event_id": prepared_event["event_id"],
            "query_digest": expected["query_digest"],
            "transport_identity": verified.attempt.subject.transport_identity,
            "network_policy_identity": verified.attempt.subject.network_policy_identity,
            "http_status": provider_receipt.http_status,
            "response_sha256": provider_receipt.response_sha256,
            "response_base64": encoded,
            "observed_at": provider_receipt.observed_at_utc,
            "host_provider_receipt": provider_receipt_payload,
            "prepared_receipt_identity": prepared_receipt.receipt_identity,
        }

        if len(events) == 2:
            existing_payload = _require_host_event(
                events[1],
                attempt_id=attempt_id,
                event_type=_OBSERVED_EVENT,
                aggregate_version=2,
                payload_keys=_HOST_OBSERVED_PAYLOAD_KEYS,
            )
            if existing_payload != payload:
                raise ProviderOriginError(
                    "Host Observed replay conflicts with durable signed response"
                )
            return self._observed_receipt(
                verified_prepared=verified,
                provider_receipt=provider_receipt,
                prepared_receipt=prepared_receipt,
                journal_identity=self.journal_identity,
                observed_event=events[1],
            )

        _append_origin_event(
            store,
            identity,
            _host_event(
                event_id=attempt_id + ":observed",
                event_type=_OBSERVED_EVENT,
                attempt_id=attempt_id,
                aggregate_version=2,
                payload=payload,
                committed_at=committed,
            ),
        )
        events = _load_origin_events(store, identity, attempt_id)
        if len(events) != 2:
            raise ProviderOriginError(
                "Host Observed commit did not produce exact "
                "Prepared/Observed chronology"
            )
        _require_host_event(
            events[1],
            attempt_id=attempt_id,
            event_type=_OBSERVED_EVENT,
            aggregate_version=2,
            payload_keys=_HOST_OBSERVED_PAYLOAD_KEYS,
        )
        return self._observed_receipt(
            verified_prepared=verified,
            provider_receipt=provider_receipt,
            prepared_receipt=prepared_receipt,
            journal_identity=self.journal_identity,
            observed_event=events[1],
        )

    def load_verified_observed(
        self,
        attempt_id: str,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        expected_session_identity: str,
        expected_public_key_sha256: str,
        expected_scope: HostAuthenticatedReadExpectedScope,
    ):
        from .provider_host_attestation import (
            HostProviderAttestationError,
            HostProviderAttestationUnavailable,
            _parse_prepared_durability,
            _parse_receipt,
            verify_host_observed_attestation,
            verify_host_prepared_attestation,
        )

        attempt = _exact_text(attempt_id, name="attempt_id")
        expected = _query_snapshot(query_binding)
        _require_host_expected_scope(expected_scope)
        store, identity = self._require_store()
        events = _load_origin_events(store, identity, attempt)
        if len(events) != 2:
            raise ProviderOriginError(
                "Host replay requires exact durable Prepared/Observed chronology"
            )
        prepared_event, observed_event = events
        prepared_payload = _require_host_event(
            prepared_event,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_HOST_PREPARED_PAYLOAD_KEYS,
        )
        observed_payload = _require_host_event(
            observed_event,
            attempt_id=attempt,
            event_type=_OBSERVED_EVENT,
            aggregate_version=2,
            payload_keys=_HOST_OBSERVED_PAYLOAD_KEYS,
        )
        if (
            prepared_payload.get("origin_kind") != _HOST_ATTESTED_PENDING_KIND
            or observed_payload.get("origin_kind") != _HOST_ATTESTED_OBSERVED_KIND
            or prepared_payload.get("query") != expected
            or observed_payload.get("prepared_event_id")
            != prepared_event.get("event_id")
            or observed_payload.get("query_digest") != expected["query_digest"]
            or observed_payload.get("transport_identity")
            != prepared_payload.get("transport_identity")
            or observed_payload.get("network_policy_identity")
            != prepared_payload.get("network_policy_identity")
        ):
            raise ProviderOriginError(
                "durable Host replay scope or chronology is inconsistent"
            )

        prepared_envelope = prepared_payload.get("host_prepared_attestation")
        provider_receipt_payload = observed_payload.get("host_provider_receipt")
        encoded = observed_payload.get("response_base64")
        if type(encoded) is not str or not encoded:
            raise ProviderOriginError(
                "durable Host response bytes are missing"
            )
        try:
            response_bytes = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "durable Host response bytes are not canonical base64"
            ) from error
        if (
            not response_bytes
            or base64.b64encode(response_bytes).decode("ascii") != encoded
        ):
            raise ProviderOriginError(
                "durable Host response bytes are not canonical"
            )

        try:
            verified_prepared = verify_host_prepared_attestation(
                prepared_envelope,
                expected_session_identity=expected_session_identity,
                expected_public_key_sha256=expected_public_key_sha256,
                expected_query=expected["query"],
            )
            self._require_subject_scope(
                verified_prepared,
                query_binding,
                expected_scope,
            )
            provider_receipt = _parse_receipt(
                provider_receipt_payload,
                prepared=verified_prepared,
                response_bytes=response_bytes,
            )
        except (
            HostProviderAttestationError,
            HostProviderAttestationUnavailable,
        ) as error:
            raise ProviderOriginError(
                "durable Host replay attestation verification failed"
            ) from error

        prepared_receipt = self._prepared_receipt(
            verified=verified_prepared,
            journal_identity=self.journal_identity,
            prepared_event=prepared_event,
        )
        observed_receipt = self._observed_receipt(
            verified_prepared=verified_prepared,
            provider_receipt=provider_receipt,
            prepared_receipt=prepared_receipt,
            journal_identity=self.journal_identity,
            observed_event=observed_event,
        )

        if (
            observed_payload.get("prepared_receipt_identity")
            != prepared_receipt.receipt_identity
            or observed_payload.get("http_status") != provider_receipt.http_status
            or observed_payload.get("response_sha256")
            != provider_receipt.response_sha256
            or observed_payload.get("observed_at")
            != provider_receipt.observed_at_utc
        ):
            raise ProviderOriginError(
                "durable Host replay response metadata conflicts with signed receipt"
            )

        observed_envelope = {
            "schema": "autotrade-host-authenticated-read-observed:v2",
            "issuer_session": prepared_envelope["issuer_session"],
            "attempt": prepared_envelope["attempt"],
            "receipt": provider_receipt_payload,
            "query": prepared_envelope["query"],
            "response_base64": encoded,
            "durable_prepared": _host_prepared_receipt_payload(prepared_receipt),
            "durable_observed": _host_observed_receipt_payload(observed_receipt),
        }
        try:
            return verify_host_observed_attestation(
                observed_envelope,
                expected_session_identity=expected_session_identity,
                expected_public_key_sha256=expected_public_key_sha256,
                expected_query=expected["query"],
                expected_journal_identity=self.journal_identity,
            )
        except (
            HostProviderAttestationError,
            HostProviderAttestationUnavailable,
        ) as error:
            raise ProviderOriginError(
                "durable Host Observed replay failed complete attestation verification"
            ) from error
