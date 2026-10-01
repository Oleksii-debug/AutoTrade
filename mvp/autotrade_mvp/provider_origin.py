"""Durable authenticated provider-read origin authority.

This module is deliberately transport-neutral. It records one immutable
Prepared -> Observed journal chain around an authenticated read and reconstructs
exact response bytes after restart. Only the provider transport seam receives
the private record token; ordinary parser/test bytes cannot self-assert
PROVIDER_ORIGIN merely by matching response content.
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

from .persistence import JournalStore, payload_digest
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
)
from .provider_response_limits import (
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_response_bytes,
)


class ProviderOriginError(RuntimeError):
    """Raised when durable provider-origin authority is incomplete or invalid."""


_PROVIDER_ORIGIN_RECORD_TOKEN = object()
_BINDING_TOKEN = object()
_OBSERVATION_TOKEN = object()
_AGGREGATE_TYPE = "authenticated_provider_read"
_PREPARED_EVENT = "AuthenticatedReadPrepared"
_OBSERVED_EVENT = "AuthenticatedReadObserved"
_ORIGIN_KIND = "PROVIDER_ORIGIN"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ORIGIN_REF_RE = re.compile(r"provider-origin:sha256:[0-9a-f]{64}")
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


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value:
        raise ProviderOriginError(f"{name} must be exact non-empty text")
    if value != value.strip():
        raise ProviderOriginError(f"{name} must be canonical text")
    return value


def _utc_text(value: object, *, name: str) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderOriginError(f"{name} must be an exact timezone-aware datetime")
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
) -> dict[str, object]:
    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise ProviderOriginError(
            "query_binding must be exact AuthenticatedReadQueryBinding"
        )
    state = vars(query_binding)
    required = (
        "provider_id",
        "account_id",
        "entity_id",
        "environment",
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


def _provider_origin_ref(
    *,
    attempt_id: str,
    prepared_event_id: str,
    observed_event_id: str,
    query_digest: str,
    endpoint: str,
    http_status: int,
    response_sha256: str,
    observed_at: str,
    transport_identity: str,
    network_policy_identity: str,
    journal_sequence: int,
) -> str:
    material = {
        "attempt_id": attempt_id,
        "prepared_event_id": prepared_event_id,
        "observed_event_id": observed_event_id,
        "query_digest": query_digest,
        "endpoint": endpoint,
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
    return "provider-origin:sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AuthenticatedReadResponseBinding:
    """Journal-derived exact provider response authority."""

    attempt_id: str
    provider_id: str
    account_id: str
    environment: str
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
        if _ORIGIN_REF_RE.fullmatch(self.origin_ref) is None:
            raise ProviderOriginError("origin_ref must be canonical")
        _parse_utc_text(self.observed_at, name="observed_at")
        if type(self.journal_sequence) is not int or self.journal_sequence <= 0:
            raise ProviderOriginError(
                "journal_sequence must be an exact positive integer"
            )
        expected_origin_ref = _provider_origin_ref(
            attempt_id=self.attempt_id,
            prepared_event_id=self.prepared_event_id,
            observed_event_id=self.observed_event_id,
            query_digest=self.query_digest,
            endpoint=self.endpoint,
            http_status=self.http_status,
            response_sha256=self.response_sha256,
            observed_at=self.observed_at,
            transport_identity=self.transport_identity,
            network_policy_identity=self.network_policy_identity,
            journal_sequence=self.journal_sequence,
        )
        if self.origin_ref != expected_origin_ref:
            raise ProviderOriginError(
                "origin_ref conflicts with complete durable provider-origin subject"
            )


@dataclass(frozen=True)
class ProviderOriginObservation:
    """Financially distinguishable observation carrying durable origin authority."""

    response_binding: AuthenticatedReadResponseBinding
    observation: ProviderResponseObservation
    _observation_token: InitVar[object | None] = None

    def __post_init__(self, _observation_token: object | None) -> None:
        if _observation_token is not _OBSERVATION_TOKEN:
            raise ProviderOriginError(
                "provider-origin observation must come from durable response binding"
            )
        if type(self.response_binding) is not AuthenticatedReadResponseBinding:
            raise ProviderOriginError("response_binding must be exact durable binding")
        if type(self.observation) is not ProviderResponseObservation:
            raise ProviderOriginError("observation must be exact ProviderResponseObservation")
        if self.observation.response_sha256 != self.response_binding.response_sha256:
            raise ProviderOriginError("observation response digest mismatch")
        if self.observation.query_binding.query_digest != self.response_binding.query_digest:
            raise ProviderOriginError("observation query digest mismatch")

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


class ProviderOriginJournal:
    """Durable authority for one canonical JournalStore generation."""

    def __init__(self, store: JournalStore) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact canonical JournalStore")
        self._store = store
        self._store_identity = JournalStore.store_identity.__get__(
            store, JournalStore
        )

    def _require_store(self) -> JournalStore:
        store = self._store
        if type(store) is not JournalStore:
            raise ProviderOriginError("provider-origin JournalStore authority changed")
        current = JournalStore.store_identity.__get__(store, JournalStore)
        if current != self._store_identity:
            raise ProviderOriginError("provider-origin JournalStore generation changed")
        return store

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
            "origin_kind": _ORIGIN_KIND,
            "query": snapshot,
            "transport_identity": transport,
            "network_policy_identity": policy,
        }
        store = self._require_store()
        JournalStore.append_event(
            store,
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
        _origin_token: object | None = None,
    ) -> AuthenticatedReadResponseBinding:
        if _origin_token is not _PROVIDER_ORIGIN_RECORD_TOKEN:
            raise ProviderOriginError(
                "PROVIDER_ORIGIN observations may only be issued by provider transport"
            )
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
        store = self._require_store()
        events = JournalStore.load_events(store, _AGGREGATE_TYPE, attempt)
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
            "origin_kind": _ORIGIN_KIND,
            "prepared_event_id": prepared.get("event_id"),
            "query_digest": expected["query_digest"],
            "transport_identity": payload["transport_identity"],
            "network_policy_identity": payload["network_policy_identity"],
            "http_status": http_status,
            "response_sha256": response_digest,
            "response_base64": base64.b64encode(raw).decode("ascii"),
            "observed_at": observed_text,
        }
        JournalStore.append_event(
            store,
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
        store = self._require_store()
        events = JournalStore.load_events(store, _AGGREGATE_TYPE, attempt)
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
        if (
            observed_payload.get("origin_kind") != _ORIGIN_KIND
            or observed_payload.get("prepared_event_id") != prepared.get("event_id")
            or observed_payload.get("query_digest") != expected["query_digest"]
            or observed_payload.get("transport_identity")
            != prepared_payload.get("transport_identity")
            or observed_payload.get("network_policy_identity")
            != prepared_payload.get("network_policy_identity")
        ):
            raise ProviderOriginError(
                "provider-origin Observed event conflicts with durable Prepared scope"
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
        origin_ref = _provider_origin_ref(
            attempt_id=attempt,
            prepared_event_id=str(prepared.get("event_id")),
            observed_event_id=str(observed.get("event_id")),
            query_digest=expected["query_digest"],
            endpoint=expected["endpoint"],
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
    if type(response_binding) is not AuthenticatedReadResponseBinding:
        raise ProviderOriginError(
            "response_binding must be exact AuthenticatedReadResponseBinding"
        )
    expected = _query_snapshot(query_binding)
    if (
        response_binding.query_digest != expected["query_digest"]
        or response_binding.provider_id != expected["provider_id"]
        or response_binding.account_id != expected["account_id"]
        or response_binding.environment != expected["environment"]
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
            "provider-origin HTTP status is outside the accepted endpoint policy"
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
    return ProviderOriginObservation(
        response_binding=response_binding,
        observation=observation,
        _observation_token=_OBSERVATION_TOKEN,
    )
