"""Durable authenticated-read provenance for exact qualified provider routes.

This module is the bounded #652 durable root.  It does not create provider
network authority.  A qualified read prepared by ``provider_route_reads`` is
persisted before I/O; exact response bytes can then be retained in the neutral
ArtifactStore before a terminal Observed event is published.  Restart can
finish Retained -> Observed without a provider re-query.

Authority boundary: deterministic durability tests may still record explicit
TEST_INJECTED responses, but that evidence class can never produce a financial
ProviderOriginObservation.  Production PROVIDER_ORIGIN requires the canonical
direct transport's closure-authorized execution receipt and a terminal exact
C/Q proof captured immediately before the wire send.  The durable journal keeps
those identities across restart without treating storage integrity as provider
origin or provider qualification.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from autotrade_runtime.artifacts import ArtifactIntegrityError, ArtifactStore

from .persistence import JournalStore, payload_digest
from .provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderReadQueryBinding,
    QualifiedProviderResponseObservation,
    _require_qualified_provider_read_binding_authority,
    observe_qualified_provider_json_response,
    terminal_qualified_provider_read_authority_snapshot,
)
from .provider_transport import (
    ProviderTransportError,
    direct_authenticated_read_execution_receipt_snapshot,
    direct_authenticated_read_network_policy_identity,
    direct_authenticated_read_transport_identity,
    provider_observation_direct_execution_material,
    qualified_authenticated_read_expected_wire_semantics_digest,
)
from .provider_response_limits import (
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_response_bytes,
)


class ProviderOriginError(RuntimeError):
    """Raised when durable qualified provider-read provenance is invalid."""


_TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN = object()
_BINDING_TOKEN = object()
_OBSERVATION_TOKEN = object()
_AGGREGATE_TYPE = "qualified_authenticated_provider_read"
_PREPARED_EVENT = "AuthenticatedReadPrepared"
_RETAINED_EVENT = "AuthenticatedReadRetained"
_OBSERVED_EVENT = "AuthenticatedReadObserved"
_ORIGIN_KIND = "PROVIDER_ORIGIN"
_DIRECT_EXECUTION_CLASS = "DIRECT_PROVIDER_WIRE"
_TEST_EXECUTION_CLASS = "TEST_INJECTED"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_ORIGIN_REF_RE = re.compile(r"^provider-origin:sha256:[0-9a-f]{64}$")

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
        "qualified_query",
        "transport_identity",
        "network_policy_identity",
    }
)
_RETAINED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "prepared_event_id",
        "prepared_subject_digest",
        "qualified_query_digest",
        "qualification_id",
        "endpoint_rule_digest",
        "qualified_route_rule_digest",
        "data_entitlement",
        "parser_identity",
        "transport_identity",
        "network_policy_identity",
        "http_status",
        "response_sha256",
        "response_artifact_id",
        "observed_at",
        "execution_class",
        "wire_request_sha256",
        "wire_request_semantics_sha256",
        "terminal_authority_journal_sequence_cut",
        "terminal_authority_verified_at",
    }
)
_OBSERVED_PAYLOAD_KEYS = frozenset(set(_RETAINED_PAYLOAD_KEYS) | {"retained_event_id"})


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderOriginError(f"{name} must be exact canonical non-empty text")
    return value


def _utc_text(value: object, *, name: str) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderOriginError(f"{name} must be exact timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, *, name: str) -> datetime:
    text = _exact_text(value, name=name)
    if not text.endswith("Z"):
        raise ProviderOriginError(f"{name} must be canonical UTC text")
    try:
        point = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderOriginError(f"{name} must be canonical UTC text") from error
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ProviderOriginError(f"{name} must be canonical UTC text")
    return point


def _qualified_query_snapshot(
    binding: QualifiedProviderReadQueryBinding,
) -> dict[str, object]:
    if type(binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("binding must be exact QualifiedProviderReadQueryBinding")
    try:
        _require_qualified_provider_read_binding_authority(binding)
        qualified_digest = binding.query_digest
    except ProviderRouteReadError as error:
        raise ProviderOriginError("qualified provider-read authority is unavailable") from error

    base = binding.query_binding
    query = base.query
    if type(query) is not MappingProxyType:
        raise ProviderOriginError("qualified provider-read query is not immutable")
    exact_query: dict[str, str] = {}
    for key, value in query.items():
        if type(key) is not str or type(value) is not str:
            raise ProviderOriginError("qualified provider-read query is non-canonical")
        exact_query[key] = value

    statuses = binding.accepted_success_statuses
    if (
        type(statuses) is not tuple
        or not statuses
        or tuple(sorted(set(statuses))) != statuses
        or any(type(status) is not int or status < 200 or status > 299 for status in statuses)
    ):
        raise ProviderOriginError("qualified provider-read success statuses are invalid")

    material = {
        "base_query": {
            "provider_id": _exact_text(base.provider_id, name="provider_id"),
            "account_id": _exact_text(base.account_id, name="account_id"),
            "entity_id": _exact_text(base.entity_id, name="entity_id"),
            "environment": _exact_text(base.environment, name="environment"),
            "capability_snapshot_id": _exact_text(
                base.capability_snapshot_id, name="capability_snapshot_id"
            ),
            "instrument_version": _exact_text(
                base.instrument_version, name="instrument_version"
            ),
            "surface": base.surface.value,
            "endpoint": _exact_text(base.endpoint, name="endpoint"),
            "query": exact_query,
            "prepared_at": _exact_text(base.prepared_at, name="prepared_at"),
            "permission_scope": _exact_text(
                base.permission_scope, name="permission_scope"
            ),
            "query_digest": _exact_text(base.query_digest, name="base_query_digest"),
        },
        "qualification_id": _exact_text(binding.qualification_id, name="qualification_id"),
        "route_semantics_digest": _exact_text(
            binding.route_semantics_digest, name="route_semantics_digest"
        ),
        "endpoint_rule_digest": _exact_text(
            binding.endpoint_rule_digest, name="endpoint_rule_digest"
        ),
        "qualified_route_rule_digest": _exact_text(
            binding.qualified_route_rule_digest, name="qualified_route_rule_digest"
        ),
        "data_entitlement": _exact_text(
            binding.data_entitlement, name="data_entitlement"
        ),
        "accepted_success_statuses": list(statuses),
        "parser_identity": _exact_text(binding.parser_identity, name="parser_identity"),
        "authority_journal_sequence_cut": binding.authority_journal_sequence_cut,
        "provider_environment": _exact_text(
            binding.provider_environment, name="provider_environment"
        ),
        "adapter_code_sha": _exact_text(binding.adapter_code_sha, name="adapter_code_sha"),
        "packaged_artifact_digest": _exact_text(
            binding.packaged_artifact_digest, name="packaged_artifact_digest"
        ),
        "qualified_query_digest": qualified_digest,
    }
    if _QID_RE.fullmatch(material["qualification_id"]) is None:
        raise ProviderOriginError("qualification_id is non-canonical")
    for name in (
        "route_semantics_digest",
        "endpoint_rule_digest",
        "qualified_route_rule_digest",
        "packaged_artifact_digest",
        "qualified_query_digest",
    ):
        if _SHA256_RE.fullmatch(material[name]) is None:
            raise ProviderOriginError(f"{name} is non-canonical")
    if _SHA256_RE.fullmatch(material["base_query"]["query_digest"]) is None:
        raise ProviderOriginError("base query digest is non-canonical")
    cut = material["authority_journal_sequence_cut"]
    if type(cut) is not int or cut < 0:
        raise ProviderOriginError("qualified provider-read authority cut is invalid")
    _parse_utc_text(material["base_query"]["prepared_at"], name="prepared_at")
    return material


def _response_artifact_id(
    *,
    attempt_id: str,
    qualified_query_digest: str,
    response_sha256: str,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://autotrade.local/provider-origin/response/"
            + attempt_id
            + "/"
            + qualified_query_digest
            + "/"
            + response_sha256,
        )
    )


def _event(
    *,
    event_id: str,
    event_type: str,
    attempt_id: str,
    version: int,
    payload: dict[str, object],
    committed_at: str,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": attempt_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": committed_at,
    }


def _require_event(
    event: object,
    *,
    attempt_id: str,
    event_type: str,
    version: int,
    payload_keys: frozenset[str],
) -> dict[str, object]:
    if type(event) is not dict or set(event) != _EVENT_KEYS:
        raise ProviderOriginError("provider-origin journal event schema is not exact")
    suffix = {
        _PREPARED_EVENT: "prepared",
        _RETAINED_EVENT: "retained",
        _OBSERVED_EVENT: "observed",
    }[event_type]
    if (
        event.get("event_id") != attempt_id + ":" + suffix
        or event.get("event_type") != event_type
        or event.get("aggregate_type") != _AGGREGATE_TYPE
        or event.get("aggregate_id") != attempt_id
        or event.get("aggregate_version") != version
    ):
        raise ProviderOriginError("provider-origin journal chronology is invalid")
    payload = event.get("payload")
    if type(payload) is not dict or set(payload) != payload_keys:
        raise ProviderOriginError("provider-origin journal payload schema is not exact")
    if event.get("payload_hash") != payload_digest(payload):
        raise ProviderOriginError("provider-origin journal payload hash mismatch")
    _parse_utc_text(event.get("committed_at"), name="committed_at")
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence < 1:
        raise ProviderOriginError("provider-origin journal sequence is invalid")
    return payload


def _origin_ref(
    *,
    attempt_id: str,
    qualified_query_digest: str,
    qualification_id: str,
    qualified_route_rule_digest: str,
    response_sha256: str,
    response_artifact_id: str,
    observed_at: str,
    journal_sequence: int,
    execution_class: str,
    wire_request_sha256: str,
    wire_request_semantics_sha256: str,
    terminal_authority_journal_sequence_cut: int,
    terminal_authority_verified_at: str,
) -> str:
    material = {
        "attempt_id": attempt_id,
        "qualified_query_digest": qualified_query_digest,
        "qualification_id": qualification_id,
        "qualified_route_rule_digest": qualified_route_rule_digest,
        "response_sha256": response_sha256,
        "response_artifact_id": response_artifact_id,
        "observed_at": observed_at,
        "journal_sequence": journal_sequence,
        "execution_class": execution_class,
        "wire_request_sha256": wire_request_sha256,
        "wire_request_semantics_sha256": wire_request_semantics_sha256,
        "terminal_authority_journal_sequence_cut": terminal_authority_journal_sequence_cut,
        "terminal_authority_verified_at": terminal_authority_verified_at,
    }
    return "provider-origin:sha256:" + sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class AuthenticatedReadResponseBinding:
    """Restartable durable binding for one exact qualified provider response."""

    attempt_id: str
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    capability_snapshot_id: str
    qualification_id: str
    endpoint: str
    qualified_query_digest: str
    endpoint_rule_digest: str
    qualified_route_rule_digest: str
    data_entitlement: str
    parser_identity: str
    transport_identity: str
    network_policy_identity: str
    http_status: int
    observed_at: str
    response_sha256: str
    response_artifact_id: str
    response_bytes: bytes
    origin_ref: str
    journal_sequence: int
    execution_class: str
    wire_request_sha256: str
    wire_request_semantics_sha256: str
    terminal_authority_journal_sequence_cut: int
    terminal_authority_verified_at: str
    _binding_token: InitVar[object | None] = None

    def __post_init__(self, _binding_token: object | None) -> None:
        if _binding_token is not _BINDING_TOKEN:
            raise ProviderOriginError(
                "provider-origin response binding must come from durable journal"
            )
        for name in (
            "attempt_id",
            "provider_id",
            "account_id",
            "environment",
            "provider_environment",
            "capability_snapshot_id",
            "qualification_id",
            "endpoint",
            "qualified_query_digest",
            "endpoint_rule_digest",
            "qualified_route_rule_digest",
            "data_entitlement",
            "parser_identity",
            "transport_identity",
            "network_policy_identity",
            "observed_at",
            "response_sha256",
            "response_artifact_id",
            "origin_ref",
            "execution_class",
            "wire_request_sha256",
            "wire_request_semantics_sha256",
            "terminal_authority_verified_at",
        ):
            _exact_text(getattr(self, name), name=name)
        if _QID_RE.fullmatch(self.qualification_id) is None:
            raise ProviderOriginError("qualification_id is non-canonical")
        for digest in (
            self.qualified_query_digest,
            self.endpoint_rule_digest,
            self.qualified_route_rule_digest,
            self.network_policy_identity,
            self.response_sha256,
            self.wire_request_sha256,
            self.wire_request_semantics_sha256,
        ):
            if _SHA256_RE.fullmatch(digest) is None:
                raise ProviderOriginError("provider-origin digest is non-canonical")
        if _ORIGIN_REF_RE.fullmatch(self.origin_ref) is None:
            raise ProviderOriginError("origin_ref is non-canonical")
        if type(self.http_status) is not int or not 200 <= self.http_status <= 299:
            raise ProviderOriginError("provider-origin HTTP status must be exact 2xx")
        if self.execution_class not in {
            _DIRECT_EXECUTION_CLASS,
            _TEST_EXECUTION_CLASS,
        }:
            raise ProviderOriginError("provider-origin execution class is invalid")
        if (
            type(self.terminal_authority_journal_sequence_cut) is not int
            or self.terminal_authority_journal_sequence_cut < 0
        ):
            raise ProviderOriginError(
                "terminal provider-read authority cut is invalid"
            )
        _parse_utc_text(
            self.terminal_authority_verified_at,
            name="terminal_authority_verified_at",
        )
        if type(self.response_bytes) is not bytes or not self.response_bytes:
            raise ProviderOriginError("provider-origin response bytes are missing")
        if "sha256:" + sha256(self.response_bytes).hexdigest() != self.response_sha256:
            raise ProviderOriginError("provider-origin response digest conflicts with bytes")
        _parse_utc_text(self.observed_at, name="observed_at")
        if type(self.journal_sequence) is not int or self.journal_sequence < 1:
            raise ProviderOriginError("provider-origin journal sequence is invalid")


@dataclass(frozen=True)
class ProviderOriginObservation:
    """Qualified parsed response carrying independent durable origin identity."""

    response_binding: AuthenticatedReadResponseBinding
    qualified_observation: QualifiedProviderResponseObservation
    _observation_token: InitVar[object | None] = None

    def __post_init__(self, _observation_token: object | None) -> None:
        if _observation_token is not _OBSERVATION_TOKEN:
            raise ProviderOriginError(
                "provider-origin observation must come from durable response binding"
            )
        if type(self.response_binding) is not AuthenticatedReadResponseBinding:
            raise ProviderOriginError("response_binding is not exact durable binding")
        if type(self.qualified_observation) is not QualifiedProviderResponseObservation:
            raise ProviderOriginError("qualified observation is not canonical")
        if (
            self.qualified_observation.evidence_ref
            == self.response_binding.origin_ref
        ):
            raise ProviderOriginError(
                "qualified content evidence and provider-origin evidence must stay distinct"
            )
        if (
            self.qualified_observation.observation.response_sha256
            != self.response_binding.response_sha256
            or self.qualified_observation.qualification_id
            != self.response_binding.qualification_id
            or self.qualified_observation.qualified_route_rule_digest
            != self.response_binding.qualified_route_rule_digest
        ):
            raise ProviderOriginError("qualified observation differs from durable origin")

    @property
    def payload(self) -> object:
        return self.qualified_observation.observation.payload

    @property
    def origin_ref(self) -> str:
        return self.response_binding.origin_ref

    @property
    def qualified_evidence_ref(self) -> str:
        return self.qualified_observation.evidence_ref


class ProviderOriginJournal:
    """Prepared/Retained/Observed journal root for one exact JournalStore generation."""

    def __init__(self, store: JournalStore, *, response_store: ArtifactStore) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        if type(response_store) is not ArtifactStore:
            raise TypeError("response_store must be exact ArtifactStore")
        self._store = store
        self._store_identity = store.store_identity
        self._response_store = response_store
        self._response_store_root = response_store.root.resolve(strict=False)

    def _require_store(self) -> JournalStore:
        if type(self._store) is not JournalStore:
            raise ProviderOriginError("provider-origin JournalStore authority changed")
        if self._store.store_identity != self._store_identity:
            raise ProviderOriginError("provider-origin JournalStore generation changed")
        if type(self._response_store) is not ArtifactStore:
            raise ProviderOriginError("provider-origin ArtifactStore authority changed")
        if self._response_store.root.resolve(strict=False) != self._response_store_root:
            raise ProviderOriginError("provider-origin ArtifactStore root changed")
        return self._store

    def prepare(
        self,
        query_binding: QualifiedProviderReadQueryBinding,
        *,
        transport_identity: str,
        network_policy_identity: str,
        recorded_at: datetime,
    ) -> str:
        snapshot = _qualified_query_snapshot(query_binding)
        transport = _exact_text(transport_identity, name="transport_identity")
        policy = _exact_text(network_policy_identity, name="network_policy_identity")
        if _SHA256_RE.fullmatch(policy) is None:
            raise ProviderOriginError("network_policy_identity must be canonical SHA-256")
        committed_at = _utc_text(recorded_at, name="recorded_at")
        prepared_at = _parse_utc_text(
            snapshot["base_query"]["prepared_at"], name="prepared_at"
        )
        if _parse_utc_text(committed_at, name="recorded_at") < prepared_at:
            raise ProviderOriginError("provider-origin prepare cannot precede query preparation")
        attempt_id = "provider-read:" + uuid4().hex
        payload = {
            "origin_kind": _ORIGIN_KIND,
            "qualified_query": snapshot,
            "transport_identity": transport,
            "network_policy_identity": policy,
        }
        JournalStore.append_event(
            self._require_store(),
            _event(
                event_id=attempt_id + ":prepared",
                event_type=_PREPARED_EVENT,
                attempt_id=attempt_id,
                version=1,
                payload=payload,
                committed_at=committed_at,
            ),
        )
        return attempt_id

    def prepare_direct(
        self,
        query_binding: QualifiedProviderReadQueryBinding,
        *,
        recorded_at: datetime,
    ) -> str:
        """Persist Prepared under the canonical direct-network identity only."""

        return self.prepare(
            query_binding,
            transport_identity=direct_authenticated_read_transport_identity(),
            network_policy_identity=direct_authenticated_read_network_policy_identity(),
            recorded_at=recorded_at,
        )

    def record_direct_provider_origin_observation(
        self,
        attempt_id: str,
        query_binding: QualifiedProviderReadQueryBinding,
        *,
        provider_observation: object,
    ) -> AuthenticatedReadResponseBinding:
        return self._record_provider_origin(
            attempt_id,
            query_binding,
            provider_observation=provider_observation,
        )

    def _record_provider_origin(
        self,
        attempt_id: str,
        query_binding: QualifiedProviderReadQueryBinding,
        *,
        http_status: int | None = None,
        response_bytes: bytes | None = None,
        observed_at: datetime | None = None,
        provider_observation: object | None = None,
        _origin_token: object | None = None,
    ) -> AuthenticatedReadResponseBinding:
        attempt = _exact_text(attempt_id, name="attempt_id")
        snapshot = _qualified_query_snapshot(query_binding)

        if provider_observation is None:
            if _origin_token is not _TEST_ONLY_PROVIDER_ORIGIN_RECORD_TOKEN:
                raise ProviderOriginError(
                    "provider-origin response lacks canonical transport execution receipt"
                )
            if (
                type(http_status) is not int
                or type(response_bytes) is not bytes
                or type(observed_at) is not datetime
            ):
                raise ProviderOriginError(
                    "test-injected provider response material is incomplete"
                )
            execution_class = _TEST_EXECUTION_CLASS
            wire_request_sha256 = "sha256:" + sha256(
                (
                    "TEST_INJECTED|"
                    + attempt
                    + "|"
                    + snapshot["qualified_query_digest"]
                ).encode("utf-8")
            ).hexdigest()
            wire_request_semantics_sha256 = "sha256:" + sha256(
                (
                    "TEST_INJECTED_SEMANTICS|"
                    + snapshot["qualified_query_digest"]
                ).encode("utf-8")
            ).hexdigest()
            terminal_cut = snapshot["authority_journal_sequence_cut"]
            terminal_verified_at = _utc_text(
                observed_at,
                name="terminal_authority_verified_at",
            )
        else:
            if (
                _origin_token is not None
                or http_status is not None
                or response_bytes is not None
                or observed_at is not None
            ):
                raise ProviderOriginError(
                    "direct provider observation cannot be mixed with caller response material"
                )
            try:
                receipt, direct_raw = provider_observation_direct_execution_material(
                    provider_observation
                )
                receipt_snapshot = (
                    direct_authenticated_read_execution_receipt_snapshot(receipt)
                )
                terminal_snapshot = (
                    terminal_qualified_provider_read_authority_snapshot(
                        receipt_snapshot["terminal_authority"]
                    )
                )
            except (ProviderTransportError, ProviderRouteReadError) as error:
                raise ProviderOriginError(
                    "provider observation lacks canonical direct wire authority"
                ) from error
            if getattr(provider_observation, "query_binding", None) is not query_binding.query_binding:
                raise ProviderOriginError(
                    "direct provider observation belongs to another exact read query"
                )
            if (
                terminal_snapshot["qualified_query_digest"]
                != snapshot["qualified_query_digest"]
                or terminal_snapshot["capability_snapshot_id"]
                != snapshot["base_query"]["capability_snapshot_id"]
                or terminal_snapshot["qualification_id"]
                != snapshot["qualification_id"]
            ):
                raise ProviderOriginError(
                    "terminal C/Q proof differs from exact qualified provider read"
                )
            http_status = getattr(provider_observation, "http_status", None)
            response_bytes = direct_raw
            observed_value = getattr(provider_observation, "observed_at", None)
            observed_at = _parse_utc_text(
                observed_value,
                name="direct provider observed_at",
            )
            expected_wire_semantics_sha256 = (
                qualified_authenticated_read_expected_wire_semantics_digest(
                    query_binding.query_binding,
                    provider_environment=query_binding.provider_environment,
                )
            )
            if (
                receipt_snapshot["request_semantics_sha256"]
                != expected_wire_semantics_sha256
            ):
                raise ProviderOriginError(
                    "direct wire request semantics differ from exact qualified read"
                )
            execution_class = _DIRECT_EXECUTION_CLASS
            wire_request_sha256 = receipt_snapshot["request_sha256"]
            wire_request_semantics_sha256 = receipt_snapshot[
                "request_semantics_sha256"
            ]
            terminal_cut = terminal_snapshot["journal_sequence_cut"]
            terminal_verified_at = terminal_snapshot["verified_at"]

        if type(http_status) is not int or http_status not in query_binding.accepted_success_statuses:
            raise ProviderOriginError("provider response status is outside qualified endpoint contract")
        try:
            raw = require_provider_response_bytes(
                response_bytes,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError("provider response exceeds exact byte budget") from error
        observed_text = _utc_text(observed_at, name="observed_at")
        if _SHA256_RE.fullmatch(wire_request_sha256) is None:
            raise ProviderOriginError("wire request digest is non-canonical")
        if _SHA256_RE.fullmatch(wire_request_semantics_sha256) is None:
            raise ProviderOriginError("wire request semantics digest is non-canonical")
        if type(terminal_cut) is not int or terminal_cut < 0:
            raise ProviderOriginError("terminal provider-read authority cut is invalid")
        _parse_utc_text(
            terminal_verified_at,
            name="terminal_authority_verified_at",
        )
        store = self._require_store()
        events = JournalStore.load_events(store, _AGGREGATE_TYPE, attempt)
        if len(events) != 1:
            raise ProviderOriginError(
                "provider-origin response requires one exact durable Prepared event"
            )
        prepared = events[0]
        prepared_payload = _require_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        if prepared_payload.get("qualified_query") != snapshot:
            raise ProviderOriginError("durable Prepared query differs from exact qualified binding")
        if execution_class == _DIRECT_EXECUTION_CLASS:
            try:
                direct_receipt_snapshot = (
                    direct_authenticated_read_execution_receipt_snapshot(receipt)
                )
            except ProviderTransportError as error:
                raise ProviderOriginError(
                    "direct provider receipt lost execution authority"
                ) from error
            if (
                prepared_payload.get("transport_identity")
                != direct_receipt_snapshot["transport_identity"]
                or prepared_payload.get("network_policy_identity")
                != direct_receipt_snapshot["network_policy_identity"]
            ):
                raise ProviderOriginError(
                    "durable Prepared network authority differs from direct wire receipt"
                )
        if _parse_utc_text(observed_text, name="observed_at") < _parse_utc_text(
            prepared.get("committed_at"), name="prepared committed_at"
        ):
            raise ProviderOriginError("provider response cannot precede durable prepare")

        response_digest = "sha256:" + sha256(raw).hexdigest()
        artifact_id = _response_artifact_id(
            attempt_id=attempt,
            qualified_query_digest=snapshot["qualified_query_digest"],
            response_sha256=response_digest,
        )
        prepared_subject_digest = _exact_text(
            prepared.get("payload_hash"), name="prepared_subject_digest"
        )
        metadata = {
            "evidence_kind": "QUALIFIED_PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt,
            "prepared_subject_digest": prepared_subject_digest,
            "qualified_query_digest": snapshot["qualified_query_digest"],
            "qualification_id": snapshot["qualification_id"],
            "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
            "qualified_route_rule_digest": snapshot["qualified_route_rule_digest"],
            "data_entitlement": snapshot["data_entitlement"],
            "parser_identity": snapshot["parser_identity"],
            "provider_environment": snapshot["provider_environment"],
            "execution_class": execution_class,
            "wire_request_sha256": wire_request_sha256,
            "wire_request_semantics_sha256": wire_request_semantics_sha256,
            "terminal_authority_journal_sequence_cut": terminal_cut,
            "terminal_authority_verified_at": terminal_verified_at,
        }
        try:
            manifest = ArtifactStore.publish_bytes(
                self._response_store,
                artifact_id=artifact_id,
                data=raw,
                media_type="application/octet-stream",
                rights={
                    "storage": True,
                    "export": False,
                    "rights_id": "qualified-provider-origin-response:v1",
                },
                source_refs=[],
                metadata=metadata,
            )
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError("provider response bytes could not be retained") from error
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("metadata") != metadata
        ):
            raise ProviderOriginError("provider response artifact conflicts with exact response")

        common = {
            "origin_kind": _ORIGIN_KIND,
            "prepared_event_id": prepared.get("event_id"),
            "prepared_subject_digest": prepared_subject_digest,
            "qualified_query_digest": snapshot["qualified_query_digest"],
            "qualification_id": snapshot["qualification_id"],
            "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
            "qualified_route_rule_digest": snapshot["qualified_route_rule_digest"],
            "data_entitlement": snapshot["data_entitlement"],
            "parser_identity": snapshot["parser_identity"],
            "transport_identity": prepared_payload["transport_identity"],
            "network_policy_identity": prepared_payload["network_policy_identity"],
            "http_status": http_status,
            "response_sha256": response_digest,
            "response_artifact_id": artifact_id,
            "observed_at": observed_text,
            "execution_class": execution_class,
            "wire_request_sha256": wire_request_sha256,
            "wire_request_semantics_sha256": wire_request_semantics_sha256,
            "terminal_authority_journal_sequence_cut": terminal_cut,
            "terminal_authority_verified_at": terminal_verified_at,
        }
        retained_id = attempt + ":retained"
        JournalStore.append_event(
            store,
            _event(
                event_id=retained_id,
                event_type=_RETAINED_EVENT,
                attempt_id=attempt,
                version=2,
                payload=common,
                committed_at=observed_text,
            ),
        )
        observed_payload = {**common, "retained_event_id": retained_id}
        JournalStore.append_event(
            store,
            _event(
                event_id=attempt + ":observed",
                event_type=_OBSERVED_EVENT,
                attempt_id=attempt,
                version=3,
                payload=observed_payload,
                committed_at=observed_text,
            ),
        )
        return self.load_response_binding(attempt, query_binding)

    def recover_response_binding(
        self,
        attempt_id: str,
        query_binding: QualifiedProviderReadQueryBinding,
    ) -> AuthenticatedReadResponseBinding:
        attempt = _exact_text(attempt_id, name="attempt_id")
        events = JournalStore.load_events(self._require_store(), _AGGREGATE_TYPE, attempt)
        if len(events) == 3:
            return self.load_response_binding(attempt, query_binding)
        if len(events) != 2:
            raise ProviderOriginError(
                "provider-origin recovery requires exact Prepared + Retained state"
            )
        prepared, retained = events
        prepared_payload = _require_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        snapshot = _qualified_query_snapshot(query_binding)
        if prepared_payload.get("qualified_query") != snapshot:
            raise ProviderOriginError("recovery query differs from durable Prepared binding")
        retained_payload = _require_event(
            retained,
            attempt_id=attempt,
            event_type=_RETAINED_EVENT,
            version=2,
            payload_keys=_RETAINED_PAYLOAD_KEYS,
        )
        if retained_payload.get("prepared_event_id") != prepared.get("event_id"):
            raise ProviderOriginError("Retained event is not bound to exact Prepared event")
        observed_text = _exact_text(retained_payload.get("observed_at"), name="observed_at")
        JournalStore.append_event(
            self._require_store(),
            _event(
                event_id=attempt + ":observed",
                event_type=_OBSERVED_EVENT,
                attempt_id=attempt,
                version=3,
                payload={**retained_payload, "retained_event_id": retained.get("event_id")},
                committed_at=observed_text,
            ),
        )
        return self.load_response_binding(attempt, query_binding)

    def load_response_binding(
        self,
        attempt_id: str,
        query_binding: QualifiedProviderReadQueryBinding,
    ) -> AuthenticatedReadResponseBinding:
        attempt = _exact_text(attempt_id, name="attempt_id")
        snapshot = _qualified_query_snapshot(query_binding)
        events = JournalStore.load_events(self._require_store(), _AGGREGATE_TYPE, attempt)
        if len(events) != 3:
            raise ProviderOriginError(
                "provider-origin response is incomplete; Prepared, Retained and Observed are required"
            )
        prepared, retained, observed = events
        prepared_payload = _require_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        if prepared_payload.get("qualified_query") != snapshot:
            raise ProviderOriginError("durable provider-origin query does not match exact qualified binding")
        retained_payload = _require_event(
            retained,
            attempt_id=attempt,
            event_type=_RETAINED_EVENT,
            version=2,
            payload_keys=_RETAINED_PAYLOAD_KEYS,
        )
        observed_payload = _require_event(
            observed,
            attempt_id=attempt,
            event_type=_OBSERVED_EVENT,
            version=3,
            payload_keys=_OBSERVED_PAYLOAD_KEYS,
        )
        if observed_payload != {**retained_payload, "retained_event_id": retained.get("event_id")}:
            raise ProviderOriginError("Observed event differs from exact Retained response")
        if retained_payload.get("prepared_event_id") != prepared.get("event_id"):
            raise ProviderOriginError("Retained event differs from exact Prepared response")
        expected_subject_digest = _exact_text(
            prepared.get("payload_hash"), name="prepared_subject_digest"
        )
        if retained_payload.get("prepared_subject_digest") != expected_subject_digest:
            raise ProviderOriginError("Retained response lost Prepared subject binding")
        expected_scope = {
            "qualified_query_digest": snapshot["qualified_query_digest"],
            "qualification_id": snapshot["qualification_id"],
            "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
            "qualified_route_rule_digest": snapshot["qualified_route_rule_digest"],
            "data_entitlement": snapshot["data_entitlement"],
            "parser_identity": snapshot["parser_identity"],
            "transport_identity": prepared_payload["transport_identity"],
            "network_policy_identity": prepared_payload["network_policy_identity"],
        }
        if any(retained_payload.get(name) != value for name, value in expected_scope.items()):
            raise ProviderOriginError("retained provider response scope differs from Prepared authority")
        status = retained_payload.get("http_status")
        if type(status) is not int or status not in query_binding.accepted_success_statuses:
            raise ProviderOriginError("durable provider response status is outside qualified contract")
        response_digest = retained_payload.get("response_sha256")
        if type(response_digest) is not str or _SHA256_RE.fullmatch(response_digest) is None:
            raise ProviderOriginError("durable provider response digest is invalid")
        artifact_id = _exact_text(
            retained_payload.get("response_artifact_id"), name="response_artifact_id"
        )
        try:
            manifest, raw = ArtifactStore.read_authenticated_snapshot(
                self._response_store,
                artifact_id,
            )
        except (ArtifactIntegrityError, FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError("durable provider response artifact is unavailable") from error
        if manifest.get("sha256") != response_digest:
            raise ProviderOriginError("durable provider response manifest digest mismatch")
        if "sha256:" + sha256(raw).hexdigest() != response_digest:
            raise ProviderOriginError("durable provider response bytes digest mismatch")
        execution_class = _exact_text(
            retained_payload.get("execution_class"),
            name="execution_class",
        )
        if execution_class not in {
            _DIRECT_EXECUTION_CLASS,
            _TEST_EXECUTION_CLASS,
        }:
            raise ProviderOriginError("durable provider response execution class is invalid")
        wire_request_sha256 = _exact_text(
            retained_payload.get("wire_request_sha256"),
            name="wire_request_sha256",
        )
        if _SHA256_RE.fullmatch(wire_request_sha256) is None:
            raise ProviderOriginError("durable wire request digest is invalid")
        wire_request_semantics_sha256 = _exact_text(
            retained_payload.get("wire_request_semantics_sha256"),
            name="wire_request_semantics_sha256",
        )
        if _SHA256_RE.fullmatch(wire_request_semantics_sha256) is None:
            raise ProviderOriginError(
                "durable wire request semantics digest is invalid"
            )
        terminal_cut = retained_payload.get(
            "terminal_authority_journal_sequence_cut"
        )
        if type(terminal_cut) is not int or terminal_cut < 0:
            raise ProviderOriginError("durable terminal authority cut is invalid")
        terminal_verified_at = _exact_text(
            retained_payload.get("terminal_authority_verified_at"),
            name="terminal_authority_verified_at",
        )
        _parse_utc_text(
            terminal_verified_at,
            name="terminal_authority_verified_at",
        )
        expected_metadata = {
            "evidence_kind": "QUALIFIED_PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt,
            "prepared_subject_digest": expected_subject_digest,
            "qualified_query_digest": snapshot["qualified_query_digest"],
            "qualification_id": snapshot["qualification_id"],
            "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
            "qualified_route_rule_digest": snapshot["qualified_route_rule_digest"],
            "data_entitlement": snapshot["data_entitlement"],
            "parser_identity": snapshot["parser_identity"],
            "provider_environment": snapshot["provider_environment"],
            "execution_class": execution_class,
            "wire_request_sha256": wire_request_sha256,
            "wire_request_semantics_sha256": wire_request_semantics_sha256,
            "terminal_authority_journal_sequence_cut": terminal_cut,
            "terminal_authority_verified_at": terminal_verified_at,
        }
        if manifest.get("metadata") != expected_metadata:
            raise ProviderOriginError(
                "durable provider response artifact metadata differs from journal authority"
            )
        observed_text = _exact_text(retained_payload.get("observed_at"), name="observed_at")
        _parse_utc_text(observed_text, name="observed_at")
        journal_sequence = observed.get("journal_sequence")
        if type(journal_sequence) is not int or journal_sequence < 1:
            raise ProviderOriginError("Observed journal sequence is invalid")
        base = snapshot["base_query"]
        origin_ref = _origin_ref(
            attempt_id=attempt,
            qualified_query_digest=snapshot["qualified_query_digest"],
            qualification_id=snapshot["qualification_id"],
            qualified_route_rule_digest=snapshot["qualified_route_rule_digest"],
            response_sha256=response_digest,
            response_artifact_id=artifact_id,
            observed_at=observed_text,
            journal_sequence=journal_sequence,
            execution_class=execution_class,
            wire_request_sha256=wire_request_sha256,
            wire_request_semantics_sha256=wire_request_semantics_sha256,
            terminal_authority_journal_sequence_cut=terminal_cut,
            terminal_authority_verified_at=terminal_verified_at,
        )
        return AuthenticatedReadResponseBinding(
            attempt_id=attempt,
            provider_id=base["provider_id"],
            account_id=base["account_id"],
            environment=base["environment"],
            provider_environment=snapshot["provider_environment"],
            capability_snapshot_id=base["capability_snapshot_id"],
            qualification_id=snapshot["qualification_id"],
            endpoint=base["endpoint"],
            qualified_query_digest=snapshot["qualified_query_digest"],
            endpoint_rule_digest=snapshot["endpoint_rule_digest"],
            qualified_route_rule_digest=snapshot["qualified_route_rule_digest"],
            data_entitlement=snapshot["data_entitlement"],
            parser_identity=snapshot["parser_identity"],
            transport_identity=prepared_payload["transport_identity"],
            network_policy_identity=prepared_payload["network_policy_identity"],
            http_status=status,
            observed_at=observed_text,
            response_sha256=response_digest,
            response_artifact_id=artifact_id,
            response_bytes=raw,
            origin_ref=origin_ref,
            journal_sequence=journal_sequence,
            execution_class=execution_class,
            wire_request_sha256=wire_request_sha256,
            wire_request_semantics_sha256=wire_request_semantics_sha256,
            terminal_authority_journal_sequence_cut=terminal_cut,
            terminal_authority_verified_at=terminal_verified_at,
            _binding_token=_BINDING_TOKEN,
        )


def observe_provider_origin_json_response(
    *,
    response_binding: AuthenticatedReadResponseBinding,
    query_binding: QualifiedProviderReadQueryBinding,
) -> ProviderOriginObservation:
    if type(response_binding) is not AuthenticatedReadResponseBinding:
        raise TypeError("response_binding must be exact AuthenticatedReadResponseBinding")
    if response_binding.execution_class != _DIRECT_EXECUTION_CLASS:
        raise ProviderOriginError(
            "provider-origin financial observation requires DIRECT_PROVIDER_WIRE evidence"
        )
    if (
        response_binding.transport_identity
        != direct_authenticated_read_transport_identity()
        or response_binding.network_policy_identity
        != direct_authenticated_read_network_policy_identity()
    ):
        raise ProviderOriginError(
            "provider-origin financial observation requires canonical direct network policy"
        )
    snapshot = _qualified_query_snapshot(query_binding)
    expected_wire_semantics_sha256 = (
        qualified_authenticated_read_expected_wire_semantics_digest(
            query_binding.query_binding,
            provider_environment=query_binding.provider_environment,
        )
    )
    if (
        response_binding.wire_request_semantics_sha256
        != expected_wire_semantics_sha256
    ):
        raise ProviderOriginError(
            "durable direct wire semantics differ from exact qualified read"
        )
    base = snapshot["base_query"]
    if (
        response_binding.provider_id != base["provider_id"]
        or response_binding.account_id != base["account_id"]
        or response_binding.environment != base["environment"]
        or response_binding.provider_environment != snapshot["provider_environment"]
        or response_binding.capability_snapshot_id != base["capability_snapshot_id"]
        or response_binding.qualification_id != snapshot["qualification_id"]
        or response_binding.endpoint != base["endpoint"]
        or response_binding.qualified_query_digest != snapshot["qualified_query_digest"]
        or response_binding.endpoint_rule_digest != snapshot["endpoint_rule_digest"]
        or response_binding.qualified_route_rule_digest != snapshot["qualified_route_rule_digest"]
        or response_binding.data_entitlement != snapshot["data_entitlement"]
        or response_binding.parser_identity != snapshot["parser_identity"]
    ):
        raise ProviderOriginError("durable provider response does not match qualified read")
    qualified = observe_qualified_provider_json_response(
        query_binding=query_binding,
        http_status=response_binding.http_status,
        response_bytes=response_binding.response_bytes,
        observed_at=_parse_utc_text(response_binding.observed_at, name="observed_at"),
    )
    return ProviderOriginObservation(
        response_binding=response_binding,
        qualified_observation=qualified,
        _observation_token=_OBSERVATION_TOKEN,
    )
