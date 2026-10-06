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
import weakref
from types import MappingProxyType
from typing import Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from autotrade_runtime.artifacts import ArtifactIntegrityError, ArtifactStore

from .durable_capabilities import DurableCapabilityRegistry
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import JournalStore, payload_digest
from .provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
    ProviderAccountAcquisitionError,
    SerializedProviderAccountAcquisition,
)
from .provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderReadQueryBinding,
    QualifiedProviderResponseObservation,
    _require_qualified_provider_read_binding_authority,
    issue_terminal_qualified_provider_read_authority,
    observe_qualified_provider_json_response,
    terminal_qualified_provider_read_authority_snapshot,
)
from .provider_transport import (
    BinanceSpotAuthenticatedReadTransport,
    BybitV5AuthenticatedReadTransport,
    KrakenSpotAuthenticatedReadTransport,
    ProviderTransportError,
    UrllibJsonWireClient,
    direct_authenticated_read_execution_receipt_snapshot,
    direct_authenticated_read_network_policy_identity,
    direct_authenticated_read_transport_identity,
    provider_observation_direct_execution_material,
    qualified_authenticated_read_expected_wire_semantics_digest,
    require_direct_authenticated_read_client,
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
_WIRE_EXECUTION_AGGREGATE_TYPE = "qualified_authenticated_provider_wire_execution"
_WIRE_EXECUTION_EVENT = "AuthenticatedReadWireExecutionClaimed"
_ACCOUNT_ACQUISITION_BINDING_AGGREGATE_TYPE = (
    "provider_origin_account_acquisition_binding"
)
_ACCOUNT_ACQUISITION_BINDING_EVENT = "ProviderOriginAccountAcquisitionBound.v1"
_PREPARED_EVENT = "AuthenticatedReadPrepared"
_RETAINED_EVENT = "AuthenticatedReadRetained"
_OBSERVED_EVENT = "AuthenticatedReadObserved"
_ORIGIN_KIND = "PROVIDER_ORIGIN"
_DIRECT_EXECUTION_CLASS = "DIRECT_PROVIDER_WIRE"
_TEST_EXECUTION_CLASS = "TEST_INJECTED"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_ORIGIN_REF_RE = re.compile(r"^provider-origin:sha256:[0-9a-f]{64}$")
_ACCOUNT_ACQUISITION_ID_RE = re.compile(
    r"^provider-account-acquisition:sha256:[0-9a-f]{64}$"
)
_PROVIDER_SCOPE_DIGEST_RE = re.compile(
    r"^provider-financial-scope:sha256:[0-9a-f]{64}$"
)

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
_ACCOUNT_ACQUISITION_BINDING_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "qualified_query_digest",
        "qualification_id",
        "account_acquisition",
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
_WIRE_EXECUTION_PAYLOAD_KEYS = frozenset(
    {
        "attempt_id",
        "qualified_query_digest",
        "qualification_id",
        "http_status",
        "response_sha256",
        "observed_at",
        "wire_request_sha256",
        "wire_request_semantics_sha256",
        "terminal_authority_journal_sequence_cut",
        "terminal_authority_verified_at",
    }
)


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderOriginError(f"{name} must be exact canonical non-empty text")
    return value


def _utc_text(value: object, *, name: str) -> str:
    # Exact datetime is not enough: a caller-controlled tzinfo subclass can
    # execute code through utcoffset()/astimezone() before provider-origin
    # authority has been established.  stdlib timezone (including fixed
    # offsets) is inert at this trust ingress.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderOriginError(
            f"{name} must be exact datetime with exact datetime.timezone tzinfo"
        )
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


def _account_acquisition_snapshot(
    *,
    authority: object | None,
    acquisition: object | None,
    store: JournalStore,
    query_binding: QualifiedProviderReadQueryBinding,
) -> dict[str, object] | None:
    """Bind one exact current serialized account acquisition to a provider read."""

    if authority is None and acquisition is None:
        return None
    if type(authority) is not DurableProviderAccountAcquisitionAuthority:
        raise ProviderOriginError(
            "account provider read requires exact DurableProviderAccountAcquisitionAuthority"
        )
    if type(acquisition) is not SerializedProviderAccountAcquisition:
        raise ProviderOriginError(
            "account provider read requires exact SerializedProviderAccountAcquisition"
        )
    if authority.store is not store:
        raise ProviderOriginError(
            "provider-origin and account acquisition must share one JournalStore instance"
        )
    try:
        current = authority.require_current(acquisition)
    except ProviderAccountAcquisitionError as error:
        raise ProviderOriginError(
            "provider-origin account acquisition is not exact current authority"
        ) from error
    base = query_binding.query_binding
    scope = current.provider_scope
    if (
        current.account_id != base.account_id
        or scope.provider_id != base.provider_id
        or scope.runtime_environment != base.environment
        or scope.provider_environment != query_binding.provider_environment
    ):
        raise ProviderOriginError(
            "provider-origin account acquisition scope differs from qualified read"
        )
    return {
        "acquisition_id": current.acquisition_id,
        "acquisition_generation": current.acquisition_generation,
        "acquisition_journal_sequence_cut": current.acquisition_journal_sequence_cut,
        "issued_journal_sequence": current.issued_journal_sequence,
        "provider_scope_digest": current.provider_scope.content_digest,
        "account_id": current.account_id,
    }


def _require_account_acquisition_snapshot(
    value: object,
    *,
    query_binding: QualifiedProviderReadQueryBinding,
) -> dict[str, object] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {
        "acquisition_id",
        "acquisition_generation",
        "acquisition_journal_sequence_cut",
        "issued_journal_sequence",
        "provider_scope_digest",
        "account_id",
    }:
        raise ProviderOriginError(
            "durable provider-origin account acquisition binding is non-canonical"
        )
    acquisition_id = value["acquisition_id"]
    scope_digest = value["provider_scope_digest"]
    account_id = value["account_id"]
    generation = value["acquisition_generation"]
    cut = value["acquisition_journal_sequence_cut"]
    issued = value["issued_journal_sequence"]
    if (
        type(acquisition_id) is not str
        or _ACCOUNT_ACQUISITION_ID_RE.fullmatch(acquisition_id) is None
        or type(scope_digest) is not str
        or _PROVIDER_SCOPE_DIGEST_RE.fullmatch(scope_digest) is None
        or type(account_id) is not str
        or not account_id
        or account_id != query_binding.query_binding.account_id
        or type(generation) is not int
        or generation < 1
        or type(cut) is not int
        or cut < 0
        or type(issued) is not int
        or issued != cut + 1
    ):
        raise ProviderOriginError(
            "durable provider-origin account acquisition binding is invalid"
        )
    return dict(value)


def _account_acquisition_binding_payload(
    *,
    attempt_id: str,
    query_binding: QualifiedProviderReadQueryBinding,
    account_acquisition: dict[str, object],
) -> dict[str, object]:
    attempt = _exact_text(attempt_id, name="account acquisition attempt_id")
    snapshot = _qualified_query_snapshot(query_binding)
    acquisition = _require_account_acquisition_snapshot(
        account_acquisition,
        query_binding=query_binding,
    )
    if acquisition is None:
        raise ProviderOriginError(
            "provider-origin account acquisition binding cannot be empty"
        )
    return {
        "schema_version": "1.0.0",
        "attempt_id": attempt,
        "qualified_query_digest": snapshot["qualified_query_digest"],
        "qualification_id": snapshot["qualification_id"],
        "account_acquisition": acquisition,
    }


def _account_acquisition_binding_event(
    *,
    payload: dict[str, object],
    committed_at: str,
) -> dict[str, object]:
    attempt = _exact_text(
        payload.get("attempt_id"),
        name="account acquisition attempt_id",
    )
    committed = _exact_text(
        committed_at,
        name="account acquisition committed_at",
    )
    _parse_utc_text(committed, name="account acquisition committed_at")
    return {
        "event_id": attempt + ":account-acquisition-bound",
        "event_type": _ACCOUNT_ACQUISITION_BINDING_EVENT,
        "aggregate_type": _ACCOUNT_ACQUISITION_BINDING_AGGREGATE_TYPE,
        "aggregate_id": attempt,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": committed,
    }


def _load_provider_origin_account_acquisition_binding(
    store: JournalStore,
    *,
    attempt_id: str,
    query_binding: QualifiedProviderReadQueryBinding,
    prepared_event: dict[str, object] | None = None,
) -> dict[str, object] | None:
    attempt = _exact_text(attempt_id, name="account acquisition attempt_id")
    events = JournalStore.load_events(
        store,
        _ACCOUNT_ACQUISITION_BINDING_AGGREGATE_TYPE,
        attempt,
    )
    if not events:
        return None
    if len(events) != 1:
        raise ProviderOriginError(
            "provider-origin account acquisition binding is ambiguous"
        )
    event = events[0]
    if type(event) is not dict or set(event) != _EVENT_KEYS:
        raise ProviderOriginError(
            "provider-origin account acquisition binding event is invalid"
        )
    payload = event.get("payload")
    if (
        type(payload) is not dict
        or set(payload) != _ACCOUNT_ACQUISITION_BINDING_PAYLOAD_KEYS
        or payload.get("schema_version") != "1.0.0"
    ):
        raise ProviderOriginError(
            "provider-origin account acquisition binding payload is invalid"
        )
    expected = _account_acquisition_binding_payload(
        attempt_id=attempt,
        query_binding=query_binding,
        account_acquisition=payload.get("account_acquisition"),
    )
    if (
        payload != expected
        or event.get("event_id") != attempt + ":account-acquisition-bound"
        or event.get("event_type") != _ACCOUNT_ACQUISITION_BINDING_EVENT
        or event.get("aggregate_type")
        != _ACCOUNT_ACQUISITION_BINDING_AGGREGATE_TYPE
        or event.get("aggregate_id") != attempt
        or event.get("aggregate_version") != 1
        or event.get("payload_hash") != payload_digest(expected)
    ):
        raise ProviderOriginError(
            "provider-origin account acquisition binding is corrupt"
        )
    committed = _exact_text(
        event.get("committed_at"),
        name="account acquisition committed_at",
    )
    _parse_utc_text(committed, name="account acquisition committed_at")
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence < 1:
        raise ProviderOriginError(
            "provider-origin account acquisition binding sequence is invalid"
        )
    acquisition = expected["account_acquisition"]
    if sequence <= acquisition["issued_journal_sequence"]:
        raise ProviderOriginError(
            "provider-origin account acquisition binding does not follow acquisition issuance"
        )
    if prepared_event is not None:
        if type(prepared_event) is not dict:
            raise ProviderOriginError(
                "provider-origin Prepared event is unavailable for acquisition binding"
            )
        prepared_sequence = prepared_event.get("journal_sequence")
        if (
            type(prepared_sequence) is not int
            or prepared_sequence < 1
            or sequence >= prepared_sequence
        ):
            raise ProviderOriginError(
                "provider-origin account acquisition binding must precede Prepared"
            )
        prepared_committed = _exact_text(
            prepared_event.get("committed_at"),
            name="provider-origin Prepared committed_at",
        )
        if _parse_utc_text(
            committed,
            name="account acquisition committed_at",
        ) > _parse_utc_text(
            prepared_committed,
            name="provider-origin Prepared committed_at",
        ):
            raise ProviderOriginError(
                "provider-origin account acquisition binding follows Prepared time"
            )
    return dict(acquisition)


def _append_provider_origin_account_acquisition_binding(
    store: JournalStore,
    *,
    attempt_id: str,
    query_binding: QualifiedProviderReadQueryBinding,
    account_acquisition: dict[str, object],
    committed_at: str,
) -> None:
    payload = _account_acquisition_binding_payload(
        attempt_id=attempt_id,
        query_binding=query_binding,
        account_acquisition=account_acquisition,
    )
    JournalStore.append_event(
        store,
        _account_acquisition_binding_event(
            payload=payload,
            committed_at=committed_at,
        ),
    )
    loaded = _load_provider_origin_account_acquisition_binding(
        store,
        attempt_id=attempt_id,
        query_binding=query_binding,
    )
    if loaded != payload["account_acquisition"]:
        raise ProviderOriginError(
            "provider-origin account acquisition binding readback mismatch"
        )


def _require_provider_origin_causal_chronology(
    *,
    prepared_at: object,
    terminal_verified_at: object,
    observed_at: object,
) -> None:
    prepared = _parse_utc_text(
        _exact_text(prepared_at, name="prepared_at"),
        name="prepared_at",
    )
    terminal = _parse_utc_text(
        _exact_text(
            terminal_verified_at,
            name="terminal_authority_verified_at",
        ),
        name="terminal_authority_verified_at",
    )
    observed = _parse_utc_text(
        _exact_text(observed_at, name="observed_at"),
        name="observed_at",
    )
    if not prepared <= terminal <= observed:
        raise ProviderOriginError(
            "provider-origin causal chronology requires Prepared <= "
            "terminal C/Q verification <= response observation"
        )


def _require_direct_terminal_after_prepared_sequence(
    *,
    prepared_event: object,
    terminal_cut: object,
) -> None:
    if type(prepared_event) is not dict:
        raise ProviderOriginError("durable Prepared event is unavailable")
    prepared_sequence = prepared_event.get("journal_sequence")
    if type(prepared_sequence) is not int or prepared_sequence < 1:
        raise ProviderOriginError("durable Prepared journal sequence is invalid")
    if type(terminal_cut) is not int or terminal_cut < prepared_sequence:
        raise ProviderOriginError(
            "terminal direct-wire authority predates durable Prepared"
        )


def _require_direct_prepared_network_authority(
    prepared_payload: object,
) -> None:
    if type(prepared_payload) is not dict:
        raise ProviderOriginError("durable Prepared payload is unavailable")
    if (
        prepared_payload.get("transport_identity")
        != direct_authenticated_read_transport_identity()
        or prepared_payload.get("network_policy_identity")
        != direct_authenticated_read_network_policy_identity()
    ):
        raise ProviderOriginError(
            "direct provider recovery requires canonical Prepared network authority"
        )


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


def _provider_response_artifact_rights() -> dict[str, object]:
    return {
        "storage": True,
        "export": False,
        "rights_id": "qualified-provider-origin-response:v1",
    }


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


def _direct_wire_execution_claim_payload(
    *,
    attempt_id: str,
    qualified_query_digest: str,
    qualification_id: str,
    http_status: int,
    response_sha256: str,
    observed_at: str,
    wire_request_sha256: str,
    wire_request_semantics_sha256: str,
    terminal_authority_journal_sequence_cut: int,
    terminal_authority_verified_at: str,
) -> dict[str, object]:
    attempt = _exact_text(attempt_id, name="wire execution attempt_id")
    qualified_digest = _exact_text(
        qualified_query_digest,
        name="wire execution qualified_query_digest",
    )
    qualification = _exact_text(
        qualification_id,
        name="wire execution qualification_id",
    )
    if type(http_status) is not int or http_status < 100 or http_status > 599:
        raise ProviderOriginError("wire execution HTTP status is invalid")
    response_digest = _exact_text(
        response_sha256,
        name="wire execution response_sha256",
    )
    request_digest = _exact_text(
        wire_request_sha256,
        name="wire execution request_sha256",
    )
    semantics_digest = _exact_text(
        wire_request_semantics_sha256,
        name="wire execution request semantics sha256",
    )
    for name, value in (
        ("qualified_query_digest", qualified_digest),
        ("response_sha256", response_digest),
        ("wire_request_sha256", request_digest),
        ("wire_request_semantics_sha256", semantics_digest),
    ):
        if _SHA256_RE.fullmatch(value) is None:
            raise ProviderOriginError(f"wire execution {name} is non-canonical")
    if _QID_RE.fullmatch(qualification) is None:
        raise ProviderOriginError("wire execution qualification_id is non-canonical")
    observed = _exact_text(observed_at, name="wire execution observed_at")
    verified = _exact_text(
        terminal_authority_verified_at,
        name="wire execution terminal authority verified_at",
    )
    observed_time = _parse_utc_text(observed, name="wire execution observed_at")
    verified_time = _parse_utc_text(
        verified,
        name="wire execution terminal authority verified_at",
    )
    if verified_time > observed_time:
        raise ProviderOriginError(
            "wire execution terminal authority verification follows response observation"
        )
    cut = terminal_authority_journal_sequence_cut
    if type(cut) is not int or cut < 0:
        raise ProviderOriginError(
            "wire execution terminal authority cut is invalid"
        )
    return {
        "attempt_id": attempt,
        "qualified_query_digest": qualified_digest,
        "qualification_id": qualification,
        "http_status": http_status,
        "response_sha256": response_digest,
        "observed_at": observed,
        "wire_request_sha256": request_digest,
        "wire_request_semantics_sha256": semantics_digest,
        "terminal_authority_journal_sequence_cut": cut,
        "terminal_authority_verified_at": verified,
    }


def _wire_execution_event(
    *,
    wire_request_sha256: str,
    payload: dict[str, object],
    committed_at: str,
) -> dict[str, object]:
    attempt_id = _exact_text(payload.get("attempt_id"), name="wire execution attempt_id")
    return {
        # Global uniqueness is enforced by this deterministic event id.  The
        # aggregate is the attempt so restart can discover the accepted claim
        # without knowing the transmitted request digest in advance.
        "event_id": "provider-wire-execution:" + wire_request_sha256.removeprefix("sha256:"),
        "event_type": _WIRE_EXECUTION_EVENT,
        "aggregate_type": _WIRE_EXECUTION_AGGREGATE_TYPE,
        "aggregate_id": attempt_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": committed_at,
    }


def _load_direct_wire_execution_claim(
    store: JournalStore,
    *,
    attempt_id: str,
) -> dict[str, object]:
    attempt = _exact_text(attempt_id, name="wire execution attempt_id")
    events = JournalStore.load_events(
        store,
        _WIRE_EXECUTION_AGGREGATE_TYPE,
        attempt,
    )
    if len(events) != 1:
        raise ProviderOriginError(
            "direct wire execution claim is missing or ambiguous"
        )
    event = events[0]
    if type(event) is not dict or set(event) != _EVENT_KEYS:
        raise ProviderOriginError("direct wire execution claim schema is invalid")
    payload = event.get("payload")
    if type(payload) is not dict or set(payload) != _WIRE_EXECUTION_PAYLOAD_KEYS:
        raise ProviderOriginError("direct wire execution claim payload is invalid")
    expected = _direct_wire_execution_claim_payload(
        attempt_id=attempt,
        qualified_query_digest=payload.get("qualified_query_digest"),
        qualification_id=payload.get("qualification_id"),
        http_status=payload.get("http_status"),
        response_sha256=payload.get("response_sha256"),
        observed_at=payload.get("observed_at"),
        wire_request_sha256=payload.get("wire_request_sha256"),
        wire_request_semantics_sha256=payload.get(
            "wire_request_semantics_sha256"
        ),
        terminal_authority_journal_sequence_cut=payload.get(
            "terminal_authority_journal_sequence_cut"
        ),
        terminal_authority_verified_at=payload.get(
            "terminal_authority_verified_at"
        ),
    )
    request_digest = expected["wire_request_sha256"]
    if (
        event.get("event_id")
        != "provider-wire-execution:" + request_digest.removeprefix("sha256:")
        or event.get("event_type") != _WIRE_EXECUTION_EVENT
        or event.get("aggregate_type") != _WIRE_EXECUTION_AGGREGATE_TYPE
        or event.get("aggregate_id") != attempt
        or event.get("aggregate_version") != 1
        or payload != expected
        or event.get("payload_hash") != payload_digest(expected)
        or event.get("committed_at") != expected["observed_at"]
    ):
        raise ProviderOriginError(
            "direct wire execution claim is corrupt"
        )
    _parse_utc_text(event.get("committed_at"), name="wire execution committed_at")
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence < 1:
        raise ProviderOriginError("direct wire execution claim sequence is invalid")
    if sequence <= expected["terminal_authority_journal_sequence_cut"]:
        raise ProviderOriginError(
            "direct wire execution claim does not follow terminal authority cut"
        )
    return expected


def _require_direct_wire_execution_claim(
    store: JournalStore,
    *,
    attempt_id: str,
    qualified_query_digest: str,
    qualification_id: str,
    http_status: int,
    response_sha256: str,
    observed_at: str,
    wire_request_sha256: str,
    wire_request_semantics_sha256: str,
    terminal_authority_journal_sequence_cut: int,
    terminal_authority_verified_at: str,
) -> None:
    expected = _direct_wire_execution_claim_payload(
        attempt_id=attempt_id,
        qualified_query_digest=qualified_query_digest,
        qualification_id=qualification_id,
        http_status=http_status,
        response_sha256=response_sha256,
        observed_at=observed_at,
        wire_request_sha256=wire_request_sha256,
        wire_request_semantics_sha256=wire_request_semantics_sha256,
        terminal_authority_journal_sequence_cut=terminal_authority_journal_sequence_cut,
        terminal_authority_verified_at=terminal_authority_verified_at,
    )
    actual = _load_direct_wire_execution_claim(
        store,
        attempt_id=attempt_id,
    )
    if actual != expected:
        raise ProviderOriginError(
            "direct wire execution is already claimed by another attempt or is corrupt"
        )


def _claim_direct_wire_execution(
    store: JournalStore,
    *,
    attempt_id: str,
    qualified_query_digest: str,
    qualification_id: str,
    http_status: int,
    response_sha256: str,
    observed_at: str,
    wire_request_sha256: str,
    wire_request_semantics_sha256: str,
    terminal_authority_journal_sequence_cut: int,
    terminal_authority_verified_at: str,
) -> None:
    expected = _direct_wire_execution_claim_payload(
        attempt_id=attempt_id,
        qualified_query_digest=qualified_query_digest,
        qualification_id=qualification_id,
        http_status=http_status,
        response_sha256=response_sha256,
        observed_at=observed_at,
        wire_request_sha256=wire_request_sha256,
        wire_request_semantics_sha256=wire_request_semantics_sha256,
        terminal_authority_journal_sequence_cut=terminal_authority_journal_sequence_cut,
        terminal_authority_verified_at=terminal_authority_verified_at,
    )
    current_cut = JournalStore.current_journal_sequence(store)
    if terminal_authority_journal_sequence_cut > current_cut:
        raise ProviderOriginError(
            "direct wire execution terminal authority cut is ahead of durable journal"
        )
    event = _wire_execution_event(
        wire_request_sha256=wire_request_sha256,
        payload=expected,
        committed_at=observed_at,
    )
    try:
        JournalStore.append_event(store, event)
    except ValueError as error:
        try:
            actual = _load_direct_wire_execution_claim(
                store,
                attempt_id=attempt_id,
            )
        except ProviderOriginError:
            raise ProviderOriginError(
                "direct wire execution is already claimed by another attempt "
                "or the claim could not be committed"
            ) from error
        if actual != expected:
            raise ProviderOriginError(
                "direct wire execution is already claimed by another attempt "
                "or is corrupt"
            ) from error
    _require_direct_wire_execution_claim(
        store,
        attempt_id=attempt_id,
        qualified_query_digest=qualified_query_digest,
        qualification_id=qualification_id,
        http_status=http_status,
        response_sha256=response_sha256,
        observed_at=observed_at,
        wire_request_sha256=wire_request_sha256,
        wire_request_semantics_sha256=wire_request_semantics_sha256,
        terminal_authority_journal_sequence_cut=terminal_authority_journal_sequence_cut,
        terminal_authority_verified_at=terminal_authority_verified_at,
    )

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
    account_acquisition_id: str | None,
    account_acquisition_generation: int | None,
    account_acquisition_journal_sequence_cut: int | None,
    account_acquisition_scope_digest: str | None,
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
        "account_acquisition_id": account_acquisition_id,
        "account_acquisition_generation": account_acquisition_generation,
        "account_acquisition_journal_sequence_cut":
            account_acquisition_journal_sequence_cut,
        "account_acquisition_scope_digest": account_acquisition_scope_digest,
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
    account_acquisition_id: str | None = None
    account_acquisition_generation: int | None = None
    account_acquisition_journal_sequence_cut: int | None = None
    account_acquisition_scope_digest: str | None = None
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
        acquisition_values = (
            self.account_acquisition_id,
            self.account_acquisition_generation,
            self.account_acquisition_journal_sequence_cut,
            self.account_acquisition_scope_digest,
        )
        if any(value is not None for value in acquisition_values):
            if (
                type(self.account_acquisition_id) is not str
                or _ACCOUNT_ACQUISITION_ID_RE.fullmatch(
                    self.account_acquisition_id
                ) is None
                or type(self.account_acquisition_generation) is not int
                or self.account_acquisition_generation < 1
                or type(self.account_acquisition_journal_sequence_cut) is not int
                or self.account_acquisition_journal_sequence_cut < 0
                or type(self.account_acquisition_scope_digest) is not str
                or _PROVIDER_SCOPE_DIGEST_RE.fullmatch(
                    self.account_acquisition_scope_digest
                ) is None
            ):
                raise ProviderOriginError(
                    "provider-origin account acquisition binding is partial or invalid"
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
        require_provider_origin_response_binding_authority(self.response_binding)
        if type(self.response_binding) is not AuthenticatedReadResponseBinding:
            raise ProviderOriginError("response_binding is not exact durable binding")
        if self.response_binding.execution_class != _DIRECT_EXECUTION_CLASS:
            raise ProviderOriginError(
                "provider-origin observation requires DIRECT_PROVIDER_WIRE evidence"
            )
        if (
            self.response_binding.transport_identity
            != direct_authenticated_read_transport_identity()
            or self.response_binding.network_policy_identity
            != direct_authenticated_read_network_policy_identity()
        ):
            raise ProviderOriginError(
                "provider-origin observation requires canonical direct network policy"
            )
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
        account_acquisition_authority: object | None = None,
        account_acquisition: object | None = None,
    ) -> str:
        snapshot = _qualified_query_snapshot(query_binding)
        account_acquisition_snapshot = _account_acquisition_snapshot(
            authority=account_acquisition_authority,
            acquisition=account_acquisition,
            store=self._require_store(),
            query_binding=query_binding,
        )
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
        store = self._require_store()
        if account_acquisition_snapshot is not None:
            _append_provider_origin_account_acquisition_binding(
                store,
                attempt_id=attempt_id,
                query_binding=query_binding,
                account_acquisition=account_acquisition_snapshot,
                committed_at=committed_at,
            )
        payload = {
            "origin_kind": _ORIGIN_KIND,
            "qualified_query": snapshot,
            "transport_identity": transport,
            "network_policy_identity": policy,
        }
        JournalStore.append_event(
            store,
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
        account_acquisition_authority: object | None = None,
        account_acquisition: object | None = None,
    ) -> str:
        """Persist Prepared under the canonical direct-network identity only."""

        return self.prepare(
            query_binding,
            transport_identity=direct_authenticated_read_transport_identity(),
            network_policy_identity=direct_authenticated_read_network_policy_identity(),
            recorded_at=recorded_at,
            account_acquisition_authority=account_acquisition_authority,
            account_acquisition=account_acquisition,
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
            _require_direct_terminal_after_prepared_sequence(
                prepared_event=prepared,
                terminal_cut=terminal_cut,
            )
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
        _require_provider_origin_causal_chronology(
            prepared_at=prepared.get("committed_at"),
            terminal_verified_at=terminal_verified_at,
            observed_at=observed_text,
        )

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
                rights=_provider_response_artifact_rights(),
                source_refs=[],
                metadata=metadata,
            )
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError("provider response bytes could not be retained") from error
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("bytes") != len(raw)
            or manifest.get("media_type") != "application/octet-stream"
            or manifest.get("rights") != _provider_response_artifact_rights()
            or manifest.get("source_refs") != []
            or manifest.get("metadata") != metadata
        ):
            raise ProviderOriginError("provider response artifact conflicts with exact response")
        if execution_class == _DIRECT_EXECUTION_CLASS:
            _claim_direct_wire_execution(
                store,
                attempt_id=attempt,
                qualified_query_digest=snapshot["qualified_query_digest"],
                qualification_id=snapshot["qualification_id"],
                http_status=http_status,
                response_sha256=response_digest,
                observed_at=observed_text,
                wire_request_sha256=wire_request_sha256,
                wire_request_semantics_sha256=wire_request_semantics_sha256,
                terminal_authority_journal_sequence_cut=terminal_cut,
                terminal_authority_verified_at=terminal_verified_at,
            )

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
        store = self._require_store()
        events = JournalStore.load_events(store, _AGGREGATE_TYPE, attempt)
        if len(events) == 3:
            return self.load_response_binding(attempt, query_binding)
        if len(events) not in {1, 2}:
            raise ProviderOriginError(
                "provider-origin recovery requires exact Prepared or "
                "Prepared + Retained state"
            )

        prepared = events[0]
        prepared_payload = _require_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        snapshot = _qualified_query_snapshot(query_binding)
        if prepared_payload.get("qualified_query") != snapshot:
            raise ProviderOriginError(
                "recovery query differs from durable Prepared binding"
            )

        if len(events) == 1:
            # A canonical direct response may have been durably retained and
            # globally claimed immediately before a crash that prevented the
            # attempt-local Retained event.  The claim is indexed by attempt_id
            # while its deterministic request-hash event id still prevents the
            # same wire execution from being claimed by another attempt.
            _require_direct_prepared_network_authority(prepared_payload)
            claim = _load_direct_wire_execution_claim(
                store,
                attempt_id=attempt,
            )
            _require_direct_terminal_after_prepared_sequence(
                prepared_event=prepared,
                terminal_cut=claim["terminal_authority_journal_sequence_cut"],
            )
            if (
                claim["qualified_query_digest"]
                != snapshot["qualified_query_digest"]
                or claim["qualification_id"] != snapshot["qualification_id"]
            ):
                raise ProviderOriginError(
                    "recovery wire claim differs from durable Prepared authority"
                )
            status = claim["http_status"]
            if (
                type(status) is not int
                or status not in query_binding.accepted_success_statuses
            ):
                raise ProviderOriginError(
                    "recovery wire claim status is outside qualified contract"
                )
            expected_semantics = (
                qualified_authenticated_read_expected_wire_semantics_digest(
                    query_binding.query_binding,
                    provider_environment=query_binding.provider_environment,
                )
            )
            if claim["wire_request_semantics_sha256"] != expected_semantics:
                raise ProviderOriginError(
                    "recovery wire claim semantics differ from qualified read"
                )
            observed_text = _exact_text(
                claim["observed_at"],
                name="recovery wire observed_at",
            )
            _require_provider_origin_causal_chronology(
                prepared_at=prepared.get("committed_at"),
                terminal_verified_at=claim["terminal_authority_verified_at"],
                observed_at=observed_text,
            )

            response_digest = _exact_text(
                claim["response_sha256"],
                name="recovery response_sha256",
            )
            artifact_id = _response_artifact_id(
                attempt_id=attempt,
                qualified_query_digest=snapshot["qualified_query_digest"],
                response_sha256=response_digest,
            )
            prepared_subject_digest = _exact_text(
                prepared.get("payload_hash"),
                name="prepared_subject_digest",
            )
            expected_metadata = {
                "evidence_kind": "QUALIFIED_PROVIDER_ORIGIN_RESPONSE",
                "attempt_id": attempt,
                "prepared_subject_digest": prepared_subject_digest,
                "qualified_query_digest": snapshot["qualified_query_digest"],
                "qualification_id": snapshot["qualification_id"],
                "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
                "qualified_route_rule_digest": snapshot[
                    "qualified_route_rule_digest"
                ],
                "data_entitlement": snapshot["data_entitlement"],
                "parser_identity": snapshot["parser_identity"],
                "provider_environment": snapshot["provider_environment"],
                "execution_class": _DIRECT_EXECUTION_CLASS,
                "wire_request_sha256": claim["wire_request_sha256"],
                "wire_request_semantics_sha256": claim[
                    "wire_request_semantics_sha256"
                ],
                "terminal_authority_journal_sequence_cut": claim[
                    "terminal_authority_journal_sequence_cut"
                ],
                "terminal_authority_verified_at": claim[
                    "terminal_authority_verified_at"
                ],
            }
            try:
                manifest, raw = ArtifactStore.read_authenticated_snapshot(
                    self._response_store,
                    artifact_id,
                )
            except (
                ArtifactIntegrityError,
                FileNotFoundError,
                OSError,
                TypeError,
                ValueError,
            ) as error:
                raise ProviderOriginError(
                    "recovery provider response artifact is unavailable"
                ) from error
            if (
                type(manifest) is not dict
                or manifest.get("artifact_id") != artifact_id
                or manifest.get("sha256") != response_digest
                or manifest.get("bytes") != len(raw)
                or manifest.get("media_type") != "application/octet-stream"
                or manifest.get("rights") != _provider_response_artifact_rights()
                or manifest.get("source_refs") != []
                or manifest.get("metadata") != expected_metadata
                or "sha256:" + sha256(raw).hexdigest() != response_digest
            ):
                raise ProviderOriginError(
                    "recovery provider response artifact differs from wire claim"
                )

            common = {
                "origin_kind": _ORIGIN_KIND,
                "prepared_event_id": prepared.get("event_id"),
                "prepared_subject_digest": prepared_subject_digest,
                "qualified_query_digest": snapshot["qualified_query_digest"],
                "qualification_id": snapshot["qualification_id"],
                "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
                "qualified_route_rule_digest": snapshot[
                    "qualified_route_rule_digest"
                ],
                "data_entitlement": snapshot["data_entitlement"],
                "parser_identity": snapshot["parser_identity"],
                "transport_identity": prepared_payload["transport_identity"],
                "network_policy_identity": prepared_payload[
                    "network_policy_identity"
                ],
                "http_status": status,
                "response_sha256": response_digest,
                "response_artifact_id": artifact_id,
                "observed_at": observed_text,
                "execution_class": _DIRECT_EXECUTION_CLASS,
                "wire_request_sha256": claim["wire_request_sha256"],
                "wire_request_semantics_sha256": claim[
                    "wire_request_semantics_sha256"
                ],
                "terminal_authority_journal_sequence_cut": claim[
                    "terminal_authority_journal_sequence_cut"
                ],
                "terminal_authority_verified_at": claim[
                    "terminal_authority_verified_at"
                ],
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
            events = JournalStore.load_events(
                store,
                _AGGREGATE_TYPE,
                attempt,
            )
            if len(events) == 3:
                return self.load_response_binding(attempt, query_binding)
            if len(events) != 2:
                raise ProviderOriginError(
                    "provider-origin recovery could not establish Retained state"
                )

        prepared, retained = events
        prepared_payload = _require_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        if prepared_payload.get("qualified_query") != snapshot:
            raise ProviderOriginError(
                "recovery query differs from durable Prepared binding"
            )
        retained_payload = _require_event(
            retained,
            attempt_id=attempt,
            event_type=_RETAINED_EVENT,
            version=2,
            payload_keys=_RETAINED_PAYLOAD_KEYS,
        )
        if retained_payload.get("prepared_event_id") != prepared.get("event_id"):
            raise ProviderOriginError(
                "Retained event is not bound to exact Prepared event"
            )
        _require_provider_origin_causal_chronology(
            prepared_at=prepared.get("committed_at"),
            terminal_verified_at=retained_payload.get(
                "terminal_authority_verified_at"
            ),
            observed_at=retained_payload.get("observed_at"),
        )
        expected_subject_digest = _exact_text(
            prepared.get("payload_hash"),
            name="prepared_subject_digest",
        )
        if retained_payload.get("prepared_subject_digest") != expected_subject_digest:
            raise ProviderOriginError(
                "Retained response lost Prepared subject binding"
            )
        expected_scope = {
            "qualified_query_digest": snapshot["qualified_query_digest"],
            "qualification_id": snapshot["qualification_id"],
            "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
            "qualified_route_rule_digest": snapshot[
                "qualified_route_rule_digest"
            ],
            "data_entitlement": snapshot["data_entitlement"],
            "parser_identity": snapshot["parser_identity"],
            "transport_identity": prepared_payload["transport_identity"],
            "network_policy_identity": prepared_payload[
                "network_policy_identity"
            ],
        }
        if any(
            retained_payload.get(name) != value
            for name, value in expected_scope.items()
        ):
            raise ProviderOriginError(
                "retained provider response scope differs from Prepared authority"
            )
        status = retained_payload.get("http_status")
        if (
            type(status) is not int
            or status not in query_binding.accepted_success_statuses
        ):
            raise ProviderOriginError(
                "durable provider response status is outside qualified contract"
            )
        response_digest = retained_payload.get("response_sha256")
        if (
            type(response_digest) is not str
            or _SHA256_RE.fullmatch(response_digest) is None
        ):
            raise ProviderOriginError(
                "durable provider response digest is invalid"
            )
        artifact_id = _exact_text(
            retained_payload.get("response_artifact_id"),
            name="response_artifact_id",
        )
        execution_class = _exact_text(
            retained_payload.get("execution_class"),
            name="execution_class",
        )
        if execution_class not in {
            _DIRECT_EXECUTION_CLASS,
            _TEST_EXECUTION_CLASS,
        }:
            raise ProviderOriginError(
                "durable provider response execution class is invalid"
            )
        wire_request_sha256 = _exact_text(
            retained_payload.get("wire_request_sha256"),
            name="wire_request_sha256",
        )
        wire_request_semantics_sha256 = _exact_text(
            retained_payload.get("wire_request_semantics_sha256"),
            name="wire_request_semantics_sha256",
        )
        if (
            _SHA256_RE.fullmatch(wire_request_sha256) is None
            or _SHA256_RE.fullmatch(wire_request_semantics_sha256) is None
        ):
            raise ProviderOriginError(
                "durable wire request authority digest is invalid"
            )
        terminal_cut = retained_payload.get(
            "terminal_authority_journal_sequence_cut"
        )
        if type(terminal_cut) is not int or terminal_cut < 0:
            raise ProviderOriginError(
                "durable terminal authority cut is invalid"
            )
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
            "qualified_route_rule_digest": snapshot[
                "qualified_route_rule_digest"
            ],
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
            manifest, raw = ArtifactStore.read_authenticated_snapshot(
                self._response_store,
                artifact_id,
            )
        except (
            ArtifactIntegrityError,
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise ProviderOriginError(
                "retained provider response artifact is unavailable"
            ) from error
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("bytes") != len(raw)
            or manifest.get("media_type") != "application/octet-stream"
            or manifest.get("rights") != _provider_response_artifact_rights()
            or manifest.get("source_refs") != []
            or manifest.get("metadata") != expected_metadata
            or "sha256:" + sha256(raw).hexdigest() != response_digest
        ):
            raise ProviderOriginError(
                "retained provider response artifact differs from journal authority"
            )
        if execution_class == _DIRECT_EXECUTION_CLASS:
            expected_wire_semantics_sha256 = (
                qualified_authenticated_read_expected_wire_semantics_digest(
                    query_binding.query_binding,
                    provider_environment=query_binding.provider_environment,
                )
            )
            if wire_request_semantics_sha256 != expected_wire_semantics_sha256:
                raise ProviderOriginError(
                    "durable direct wire semantics differ from exact qualified read"
                )

        if retained_payload.get("execution_class") == _DIRECT_EXECUTION_CLASS:
            _require_direct_prepared_network_authority(prepared_payload)
            _require_direct_terminal_after_prepared_sequence(
                prepared_event=prepared,
                terminal_cut=retained_payload.get(
                    "terminal_authority_journal_sequence_cut"
                ),
            )
            _require_direct_wire_execution_claim(
                store,
                attempt_id=attempt,
                qualified_query_digest=_exact_text(
                    retained_payload.get("qualified_query_digest"),
                    name="qualified_query_digest",
                ),
                qualification_id=_exact_text(
                    retained_payload.get("qualification_id"),
                    name="qualification_id",
                ),
                http_status=retained_payload.get("http_status"),
                response_sha256=_exact_text(
                    retained_payload.get("response_sha256"),
                    name="response_sha256",
                ),
                observed_at=_exact_text(
                    retained_payload.get("observed_at"),
                    name="observed_at",
                ),
                wire_request_sha256=_exact_text(
                    retained_payload.get("wire_request_sha256"),
                    name="wire_request_sha256",
                ),
                wire_request_semantics_sha256=_exact_text(
                    retained_payload.get("wire_request_semantics_sha256"),
                    name="wire_request_semantics_sha256",
                ),
                terminal_authority_journal_sequence_cut=retained_payload.get(
                    "terminal_authority_journal_sequence_cut"
                ),
                terminal_authority_verified_at=_exact_text(
                    retained_payload.get("terminal_authority_verified_at"),
                    name="terminal_authority_verified_at",
                ),
            )
        observed_text = _exact_text(
            retained_payload.get("observed_at"),
            name="observed_at",
        )
        JournalStore.append_event(
            store,
            _event(
                event_id=attempt + ":observed",
                event_type=_OBSERVED_EVENT,
                attempt_id=attempt,
                version=3,
                payload={
                    **retained_payload,
                    "retained_event_id": retained.get("event_id"),
                },
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
        account_acquisition = _load_provider_origin_account_acquisition_binding(
            self._require_store(),
            attempt_id=attempt,
            query_binding=query_binding,
            prepared_event=prepared,
        )
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
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("bytes") != len(raw)
            or manifest.get("media_type") != "application/octet-stream"
            or manifest.get("rights") != _provider_response_artifact_rights()
            or manifest.get("source_refs") != []
        ):
            raise ProviderOriginError(
                "durable provider response artifact policy differs from canonical retention"
            )
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
        observed_text = _exact_text(
            retained_payload.get("observed_at"),
            name="observed_at",
        )
        _require_provider_origin_causal_chronology(
            prepared_at=prepared.get("committed_at"),
            terminal_verified_at=terminal_verified_at,
            observed_at=observed_text,
        )
        if execution_class == _DIRECT_EXECUTION_CLASS:
            _require_direct_prepared_network_authority(prepared_payload)
            expected_wire_semantics_sha256 = (
                qualified_authenticated_read_expected_wire_semantics_digest(
                    query_binding.query_binding,
                    provider_environment=query_binding.provider_environment,
                )
            )
            if wire_request_semantics_sha256 != expected_wire_semantics_sha256:
                raise ProviderOriginError(
                    "durable direct wire semantics differ from exact qualified read"
                )
            _require_direct_terminal_after_prepared_sequence(
                prepared_event=prepared,
                terminal_cut=terminal_cut,
            )
            _require_direct_wire_execution_claim(
                self._require_store(),
                attempt_id=attempt,
                qualified_query_digest=snapshot["qualified_query_digest"],
                qualification_id=snapshot["qualification_id"],
                http_status=status,
                response_sha256=response_digest,
                observed_at=_exact_text(
                    retained_payload.get("observed_at"),
                    name="observed_at",
                ),
                wire_request_sha256=wire_request_sha256,
                wire_request_semantics_sha256=wire_request_semantics_sha256,
                terminal_authority_journal_sequence_cut=terminal_cut,
                terminal_authority_verified_at=terminal_verified_at,
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
            account_acquisition_id=(
                account_acquisition["acquisition_id"]
                if account_acquisition is not None else None
            ),
            account_acquisition_generation=(
                account_acquisition["acquisition_generation"]
                if account_acquisition is not None else None
            ),
            account_acquisition_journal_sequence_cut=(
                account_acquisition["acquisition_journal_sequence_cut"]
                if account_acquisition is not None else None
            ),
            account_acquisition_scope_digest=(
                account_acquisition["provider_scope_digest"]
                if account_acquisition is not None else None
            ),
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
            account_acquisition_id=(
                account_acquisition["acquisition_id"]
                if account_acquisition is not None else None
            ),
            account_acquisition_generation=(
                account_acquisition["acquisition_generation"]
                if account_acquisition is not None else None
            ),
            account_acquisition_journal_sequence_cut=(
                account_acquisition["acquisition_journal_sequence_cut"]
                if account_acquisition is not None else None
            ),
            account_acquisition_scope_digest=(
                account_acquisition["provider_scope_digest"]
                if account_acquisition is not None else None
            ),
            _binding_token=_BINDING_TOKEN,
        )




def _install_provider_origin_response_binding_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            JournalStore,
            object,
        ],
    ] = {}
    field_names = (
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
        "http_status",
        "observed_at",
        "response_sha256",
        "response_artifact_id",
        "response_bytes",
        "origin_ref",
        "journal_sequence",
        "execution_class",
        "wire_request_sha256",
        "wire_request_semantics_sha256",
        "terminal_authority_journal_sequence_cut",
        "terminal_authority_verified_at",
        "account_acquisition_id",
        "account_acquisition_generation",
        "account_acquisition_journal_sequence_cut",
        "account_acquisition_scope_digest",
    )

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def material(value: AuthenticatedReadResponseBinding) -> tuple[object, ...]:
        if type(value) is not AuthenticatedReadResponseBinding:
            raise ProviderOriginError(
                "exact provider-origin response binding is required"
            )
        return tuple(getattr(value, name) for name in field_names)

    def register(
        value: AuthenticatedReadResponseBinding,
        store: JournalStore,
    ) -> None:
        if type(store) is not JournalStore:
            raise ProviderOriginError(
                "provider-origin response binding requires exact JournalStore"
            )
        snapshot = material(value)
        store_identity = store.store_identity
        prune()
        object_id = id(value)
        current = states.get(object_id)
        if current is not None and current[0]() is not None:
            if (
                current[0]() is value
                and current[1] == snapshot
                and current[2] is store
                and current[3] == store_identity
            ):
                return
            raise ProviderOriginError(
                "provider-origin response binding authority identity collision"
            )
        states[object_id] = (
            weakref.ref(value),
            snapshot,
            store,
            store_identity,
        )

    def require(value: AuthenticatedReadResponseBinding) -> AuthenticatedReadResponseBinding:
        snapshot = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderOriginError(
                "provider-origin response binding construction authority is unavailable"
            )
        if state[1] != snapshot:
            raise ProviderOriginError(
                "provider-origin response binding changed after durable journal load"
            )
        if (
            type(state[2]) is not JournalStore
            or state[2].store_identity != state[3]
        ):
            raise ProviderOriginError(
                "provider-origin response binding JournalStore generation changed"
            )
        return value

    def require_store(
        value: AuthenticatedReadResponseBinding,
        store: JournalStore,
    ) -> AuthenticatedReadResponseBinding:
        require(value)
        if type(store) is not JournalStore:
            raise ProviderOriginError(
                "provider-origin account consumer requires exact JournalStore"
            )
        state = states.get(id(value))
        if (
            state is None
            or state[0]() is not value
            or state[2] is not store
            or store.store_identity != state[3]
        ):
            raise ProviderOriginError(
                "provider-origin response and account authority must share "
                "the same exact JournalStore generation"
            )
        return value

    return register, require, require_store


(
    _register_provider_origin_response_binding_authority,
    require_provider_origin_response_binding_authority,
    require_provider_origin_response_binding_store,
) = _install_provider_origin_response_binding_authority()
del _install_provider_origin_response_binding_authority


def _bind_provider_origin_response_load(load_impl, register_authority):
    def load_response_binding(self, attempt_id, query_binding):
        value = load_impl(self, attempt_id, query_binding)
        register_authority(value, self._require_store())
        return value

    return load_response_binding


ProviderOriginJournal.load_response_binding = _bind_provider_origin_response_load(
    ProviderOriginJournal.load_response_binding,
    _register_provider_origin_response_binding_authority,
)
del _bind_provider_origin_response_load
del _register_provider_origin_response_binding_authority


def _install_direct_provider_origin_record_authority():
    record_impl = ProviderOriginJournal._record_provider_origin
    direct_record_token = object()

    def guarded_record(
        self,
        attempt_id,
        query_binding,
        *,
        http_status=None,
        response_bytes=None,
        observed_at=None,
        provider_observation=None,
        _origin_token=None,
    ):
        if provider_observation is not None:
            if _origin_token is not direct_record_token:
                raise ProviderOriginError(
                    "direct provider origin requires canonical execute authority"
                )
            return record_impl(
                self,
                attempt_id,
                query_binding,
                provider_observation=provider_observation,
            )
        return record_impl(
            self,
            attempt_id,
            query_binding,
            http_status=http_status,
            response_bytes=response_bytes,
            observed_at=observed_at,
            provider_observation=None,
            _origin_token=_origin_token,
        )

    def record_from_execute(
        origin,
        attempt_id,
        query_binding,
        *,
        provider_observation,
    ):
        return guarded_record(
            origin,
            attempt_id,
            query_binding,
            provider_observation=provider_observation,
            _origin_token=direct_record_token,
        )

    return guarded_record, record_from_execute


(
    ProviderOriginJournal._record_provider_origin,
    _record_direct_provider_origin_from_execute,
) = _install_direct_provider_origin_record_authority()
del _install_direct_provider_origin_record_authority


def _execute_direct_provider_origin_read_impl(
    *,
    origin: ProviderOriginJournal,
    route: object,
    capability_registry: object,
    qualification_registry: object,
    query_binding: QualifiedProviderReadQueryBinding,
    transport: object,
    account_acquisition_authority: object | None,
    account_acquisition: object | None,
    _record_direct_provider_origin,
) -> AuthenticatedReadResponseBinding:
    """Execute one qualified read through the canonical direct provider wire.

    Prepared is durable before I/O.  Exact current C/Q is re-resolved inside
    the transport's terminal callback immediately before SEND.  Only an exact
    canonical UrllibJsonWireClient whose original direct-only opener is still
    authoritative may enter this path.
    """

    if type(origin) is not ProviderOriginJournal:
        raise TypeError("origin must be exact ProviderOriginJournal")
    if type(query_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError(
            "query_binding must be exact QualifiedProviderReadQueryBinding"
        )
    _require_qualified_provider_read_binding_authority(query_binding)
    if type(capability_registry) is not DurableCapabilityRegistry:
        raise TypeError("capability_registry must be exact DurableCapabilityRegistry")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    authority_store = capability_registry.store
    if qualification_registry.store is not authority_store:
        raise ProviderOriginError(
            "provider-origin C/Q authorities must share one JournalStore instance"
        )
    if origin._require_store() is not authority_store:
        raise ProviderOriginError(
            "provider-origin authority must share exact C/Q JournalStore instance"
        )
    account_snapshot = _account_acquisition_snapshot(
        authority=account_acquisition_authority,
        acquisition=account_acquisition,
        store=authority_store,
        query_binding=query_binding,
    )
    if account_snapshot is not None:
        qualification = qualification_registry.qualification(
            query_binding.qualification_id,
            journal_sequence_cut=query_binding.authority_journal_sequence_cut,
        )
        if qualification.scope.provider_scope.content_digest != (
            account_snapshot["provider_scope_digest"]
        ):
            raise ProviderOriginError(
                "provider-origin acquisition scope differs from accepted provider Q"
            )
    if type(transport) not in {
        BinanceSpotAuthenticatedReadTransport,
        BybitV5AuthenticatedReadTransport,
        KrakenSpotAuthenticatedReadTransport,
    }:
        raise ProviderOriginError(
            "provider-origin execution requires canonical authenticated-read transport"
        )
    wire_client = getattr(transport, "wire_client", None)
    if type(wire_client) is not UrllibJsonWireClient:
        raise ProviderOriginError(
            "provider-origin execution requires canonical direct wire client"
        )
    try:
        require_direct_authenticated_read_client(wire_client)
    except ProviderTransportError as error:
        raise ProviderOriginError(
            "provider-origin direct network authority is unavailable"
        ) from error

    base = query_binding.query_binding
    policy = getattr(transport, "policy", None)
    if (
        getattr(transport, "account_id", None) != base.account_id
        or getattr(transport, "capability_snapshot_id", None)
        != base.capability_snapshot_id
        or getattr(policy, "provider_id", None) != base.provider_id
        or getattr(policy, "environment", None) != base.environment
    ):
        raise ProviderOriginError(
            "provider-origin transport scope differs from exact qualified read"
        )
    if type(transport) is BybitV5AuthenticatedReadTransport:
        if transport.provider_environment != query_binding.provider_environment:
            raise ProviderOriginError(
                "Bybit provider environment differs from exact qualified read"
            )
    elif type(transport) is BinanceSpotAuthenticatedReadTransport:
        expected_provider_environment = (
            "TESTNET" if base.environment == "PAPER" else "LIVE"
        )
        if query_binding.provider_environment != expected_provider_environment:
            raise ProviderOriginError(
                "Binance provider environment differs from canonical wire policy"
            )
    elif query_binding.provider_environment != "LIVE":
        raise ProviderOriginError(
            "Kraken provider environment differs from canonical wire policy"
        )

    clock_utc = getattr(transport, "clock_utc", None)
    if not callable(clock_utc):
        raise ProviderOriginError(
            "provider-origin transport clock authority is unavailable"
        )
    prepared_at = clock_utc()
    attempt_id = origin.prepare_direct(
        query_binding,
        recorded_at=prepared_at,
        account_acquisition_authority=account_acquisition_authority,
        account_acquisition=account_acquisition,
    )

    def terminal_authority_factory(received_query):
        if received_query is not base:
            raise ProviderOriginError(
                "terminal transport query differs from exact qualified read"
            )
        if account_snapshot is not None:
            _account_acquisition_snapshot(
                authority=account_acquisition_authority,
                acquisition=account_acquisition,
                store=authority_store,
                query_binding=query_binding,
            )
        return issue_terminal_qualified_provider_read_authority(
            route,
            capability_registry,
            qualification_registry,
            query_binding,
            at=clock_utc(),
        )

    provider_observation = transport(
        base,
        terminal_authority_factory=terminal_authority_factory,
    )
    if account_snapshot is not None:
        _account_acquisition_snapshot(
            authority=account_acquisition_authority,
            acquisition=account_acquisition,
            store=authority_store,
            query_binding=query_binding,
        )
    return _record_direct_provider_origin(
        origin,
        attempt_id,
        query_binding,
        provider_observation=provider_observation,
    )


def _bind_execute_direct_provider_origin_read(execute_impl, record_direct):
    def execute_direct_provider_origin_read(
        *,
        origin: ProviderOriginJournal,
        route: object,
        capability_registry: object,
        qualification_registry: object,
        query_binding: QualifiedProviderReadQueryBinding,
        transport: object,
        account_acquisition_authority: object | None = None,
        account_acquisition: object | None = None,
    ) -> AuthenticatedReadResponseBinding:
        return execute_impl(
            origin=origin,
            route=route,
            capability_registry=capability_registry,
            qualification_registry=qualification_registry,
            query_binding=query_binding,
            transport=transport,
            account_acquisition_authority=account_acquisition_authority,
            account_acquisition=account_acquisition,
            _record_direct_provider_origin=record_direct,
        )

    return execute_direct_provider_origin_read


execute_direct_provider_origin_read = _bind_execute_direct_provider_origin_read(
    _execute_direct_provider_origin_read_impl,
    _record_direct_provider_origin_from_execute,
)
del _bind_execute_direct_provider_origin_read
del _execute_direct_provider_origin_read_impl
del _record_direct_provider_origin_from_execute

def require_current_provider_origin_account_acquisition(
    *,
    response_binding: AuthenticatedReadResponseBinding,
    account_acquisition_authority: DurableProviderAccountAcquisitionAuthority,
    account_acquisition: SerializedProviderAccountAcquisition,
) -> SerializedProviderAccountAcquisition:
    """Require one provider-origin response to belong to exact current acquisition."""

    require_provider_origin_response_binding_authority(response_binding)
    if type(account_acquisition_authority) is not DurableProviderAccountAcquisitionAuthority:
        raise TypeError(
            "account_acquisition_authority must be exact DurableProviderAccountAcquisitionAuthority"
        )
    if type(account_acquisition) is not SerializedProviderAccountAcquisition:
        raise TypeError(
            "account_acquisition must be exact SerializedProviderAccountAcquisition"
        )
    require_provider_origin_response_binding_store(
        response_binding,
        account_acquisition_authority.store,
    )
    try:
        current = account_acquisition_authority.require_current(account_acquisition)
    except ProviderAccountAcquisitionError as error:
        raise ProviderOriginError(
            "provider-origin account acquisition is no longer current"
        ) from error
    if (
        response_binding.account_acquisition_id is None
        or response_binding.account_acquisition_generation is None
        or response_binding.account_acquisition_journal_sequence_cut is None
        or response_binding.account_acquisition_scope_digest is None
    ):
        raise ProviderOriginError(
            "provider-origin response is not bound to account acquisition authority"
        )
    if (
        response_binding.account_id != current.account_id
        or response_binding.provider_id != current.provider_scope.provider_id
        or response_binding.environment != current.provider_scope.runtime_environment
        or response_binding.provider_environment
        != current.provider_scope.provider_environment
        or response_binding.account_acquisition_id != current.acquisition_id
        or response_binding.account_acquisition_generation
        != current.acquisition_generation
        or response_binding.account_acquisition_journal_sequence_cut
        != current.acquisition_journal_sequence_cut
        or response_binding.account_acquisition_scope_digest
        != current.provider_scope.content_digest
    ):
        raise ProviderOriginError(
            "provider-origin response belongs to another account acquisition"
        )
    return current


def execute_direct_provider_origin_account_read(
    *,
    origin: ProviderOriginJournal,
    route: object,
    capability_registry: object,
    qualification_registry: object,
    query_binding: QualifiedProviderReadQueryBinding,
    transport: object,
    account_acquisition_authority: DurableProviderAccountAcquisitionAuthority,
    account_acquisition: SerializedProviderAccountAcquisition,
) -> AuthenticatedReadResponseBinding:
    """Canonical direct-wire entry point for acquisition-bound account reads."""

    if type(account_acquisition_authority) is not DurableProviderAccountAcquisitionAuthority:
        raise TypeError(
            "account_acquisition_authority must be exact DurableProviderAccountAcquisitionAuthority"
        )
    if type(account_acquisition) is not SerializedProviderAccountAcquisition:
        raise TypeError(
            "account_acquisition must be exact SerializedProviderAccountAcquisition"
        )
    return execute_direct_provider_origin_read(
        origin=origin,
        route=route,
        capability_registry=capability_registry,
        qualification_registry=qualification_registry,
        query_binding=query_binding,
        transport=transport,
        account_acquisition_authority=account_acquisition_authority,
        account_acquisition=account_acquisition,
    )


def observe_provider_origin_json_response(
    *,
    response_binding: AuthenticatedReadResponseBinding,
    query_binding: QualifiedProviderReadQueryBinding,
) -> ProviderOriginObservation:
    require_provider_origin_response_binding_authority(response_binding)
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
