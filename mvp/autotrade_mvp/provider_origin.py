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
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .capabilities import CapabilityRegistry, CapabilitySnapshot
from .persistence import (
    JournalSequencePreconditionFailed,
    JournalStore,
    ProtectedWriterCapability,
    payload_digest,
)
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderCoreError,
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from .provider_response_limits import (
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_response_bytes,
)
from .provider_qualification_authority import ProviderQualificationCurrentReader
from .provider_selection import (
    ProviderSelectionError,
    SelectedProviderAuthority,
    revalidate_selected_provider_authority,
)
from .reconciliation_journal import (
    ReconciliationScopeGeneration,
    load_reconciliation_scope_generation_payload,
    reconciliation_scope_generation_payload,
    require_current_reconciliation_scope_generation,
    require_current_reconciliation_scope_generation_payload,
)


class ProviderOriginError(RuntimeError):
    """Raised when durable provider-origin authority is incomplete or invalid."""


_BINDING_TOKEN = object()
_OBSERVATION_TOKEN = object()
_HISTORICAL_EVIDENCE_TOKEN = object()
_AGGREGATE_TYPE = "authenticated_provider_read"
_PROTECTED_WRITER_NAMESPACE = "authenticated_provider_read:v1"
_PROTECTED_WRITER_AUTHORITY_ID = "provider-origin-journal:v1"
_PREPARED_EVENT = "AuthenticatedReadPrepared"
_RETAINED_EVENT = "AuthenticatedReadResponseRetained"
_OBSERVED_EVENT = "AuthenticatedReadObserved"
_ORIGIN_KIND = "PROVIDER_ORIGIN"
_ROUTE_SCHEMA_VERSION = "authenticated-read-route:v1"
_PREPARED_SUBJECT_SCHEMA_VERSION = "provider-origin-prepared-subject:v2"
_ORIGIN_SUBJECT_SCHEMA_VERSION = "provider-origin-subject:v2"
_AUTHENTICATED_READ_NETWORK_POLICY_ID = "direct-tls-v1"
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
        "writer_namespace",
        "writer_authority_id",
        "writer_authority_hash",
        "writer_provenance_hash",
    }
)
_PREPARED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "prepared_subject_schema",
        "query",
        "transport_identity",
        "network_policy_identity",
        "route",
        "selection",
        "reconciliation_generation",
    }
)
_RETAINED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "prepared_event_id",
        "prepared_subject_digest",
        "query_digest",
        "transport_identity",
        "network_policy_identity",
        "route_digest",
        "selection_identity",
        "reconciliation_generation",
        "http_status",
        "response_sha256",
        "response_artifact_id",
        "observed_at",
    }
)
_OBSERVED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "prepared_event_id",
        "retained_event_id",
        "prepared_subject_digest",
        "query_digest",
        "transport_identity",
        "network_policy_identity",
        "route_digest",
        "selection_identity",
        "reconciliation_generation",
        "http_status",
        "response_sha256",
        "response_artifact_id",
        "observed_at",
    }
)
_LEGACY_PREPARED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "query",
        "transport_identity",
        "network_policy_identity",
        "route",
    }
)
# Historical pre-Q/network evidence is exactly the old two-event schema.  Keep
# this explicit: current Retained staging must never be silently projected into
# or required by the legacy audit-only path.
_LEGACY_OBSERVED_PAYLOAD_KEYS = frozenset(
    {
        "origin_kind",
        "prepared_event_id",
        "query_digest",
        "transport_identity",
        "network_policy_identity",
        "route_digest",
        "http_status",
        "response_sha256",
        "response_artifact_id",
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


def _response_artifact_id(
    *,
    attempt_id: str,
    query_digest: str,
    response_sha256: str,
) -> str:
    attempt = _exact_text(attempt_id, name="attempt_id")
    query = _exact_text(query_digest, name="query_digest")
    response = _exact_text(response_sha256, name="response_sha256")
    if _SHA256_RE.fullmatch(query) is None or _SHA256_RE.fullmatch(response) is None:
        raise ProviderOriginError(
            "provider response artifact identity requires canonical digests"
        )
    material = (
        "autotrade:provider-origin-response:v1\n"
        + attempt
        + "\n"
        + query
        + "\n"
        + response
    )
    return str(uuid5(NAMESPACE_URL, material))


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



_SELECTION_SCHEMA_VERSION = "provider-selected-authority:v1"
_SELECTION_KEYS = frozenset(
    {
        "schema_version",
        "provider_id",
        "product_family",
        "adapter_code_sha",
        "qualification_id",
        "capability_snapshot_id",
        "account_id",
        "entity_id",
        "environment",
        "provider_environment",
        "instrument_version",
        "route_policy_id",
        "entity_policy_id",
        "network_policy_id",
        "account_class",
        "release_artifact_id",
        "release_artifact_sha256",
        "reconciliation_semantics_id",
        "selection_identity",
    }
)


def _optional_exact_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _exact_text(value, name=name)


def _require_stored_selection(
    value: object,
    query_binding: AuthenticatedReadQueryBinding,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != _SELECTION_KEYS:
        raise ProviderOriginError(
            "durable selected provider authority schema is not exact"
        )
    if value.get("schema_version") != _SELECTION_SCHEMA_VERSION:
        raise ProviderOriginError(
            "durable selected provider authority version is unsupported"
        )
    expected = _query_snapshot(query_binding)
    required_text = (
        "provider_id",
        "product_family",
        "adapter_code_sha",
        "qualification_id",
        "capability_snapshot_id",
        "account_id",
        "entity_id",
        "environment",
        "provider_environment",
        "instrument_version",
        "route_policy_id",
        "entity_policy_id",
        "network_policy_id",
        "account_class",
    )
    material: dict[str, object] = {"schema_version": _SELECTION_SCHEMA_VERSION}
    for name in required_text:
        material[name] = _exact_text(value.get(name), name=name)
    if (
        material["provider_id"] != expected["provider_id"]
        or material["capability_snapshot_id"] != expected["capability_snapshot_id"]
        or material["account_id"] != expected["account_id"]
        or material["entity_id"] != expected["entity_id"]
        or material["environment"] != expected["environment"]
        or material["provider_environment"] != expected["provider_environment"]
        or material["instrument_version"] != expected["instrument_version"]
    ):
        raise ProviderOriginError(
            "durable selected provider authority conflicts with exact query scope"
        )
    if re.fullmatch(r"[0-9a-f]{40}", str(material["adapter_code_sha"])) is None:
        raise ProviderOriginError(
            "durable selected adapter_code_sha is not canonical Git SHA"
        )
    if material["network_policy_id"] != _AUTHENTICATED_READ_NETWORK_POLICY_ID:
        raise ProviderOriginError(
            "durable selected provider authority does not bind canonical direct-TLS network policy"
        )
    release_artifact_id = _optional_exact_text(
        value.get("release_artifact_id"), name="release_artifact_id"
    )
    release_artifact_sha256 = _optional_exact_text(
        value.get("release_artifact_sha256"), name="release_artifact_sha256"
    )
    if (release_artifact_id is None) != (release_artifact_sha256 is None):
        raise ProviderOriginError(
            "durable selected release artifact identity is incomplete"
        )
    if (
        release_artifact_sha256 is not None
        and _SHA256_RE.fullmatch(release_artifact_sha256) is None
    ):
        raise ProviderOriginError(
            "durable selected release artifact digest is not canonical"
        )
    reconciliation = _optional_exact_text(
        value.get("reconciliation_semantics_id"),
        name="reconciliation_semantics_id",
    )
    material["release_artifact_id"] = release_artifact_id
    material["release_artifact_sha256"] = release_artifact_sha256
    material["reconciliation_semantics_id"] = reconciliation
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    identity = _exact_text(value.get("selection_identity"), name="selection_identity")
    expected_identity = "sha256:" + sha256(encoded).hexdigest()
    if identity != expected_identity or _SHA256_RE.fullmatch(identity) is None:
        raise ProviderOriginError(
            "durable selected provider authority identity is invalid"
        )
    return {**material, "selection_identity": identity}


def _selection_record(
    selected: SelectedProviderAuthority,
    query_binding: AuthenticatedReadQueryBinding,
) -> dict[str, object]:
    if type(selected) is not SelectedProviderAuthority:
        raise ProviderOriginError(
            "selected_authority must be exact SelectedProviderAuthority"
        )
    material: dict[str, object] = {
        "schema_version": _SELECTION_SCHEMA_VERSION,
        "provider_id": selected.provider_id,
        "product_family": selected.product_family,
        "adapter_code_sha": selected.adapter_code_sha,
        "qualification_id": selected.qualification_id,
        "capability_snapshot_id": selected.capability_snapshot_id,
        "account_id": selected.account_id,
        "entity_id": selected.entity_id,
        "environment": selected.environment,
        "provider_environment": selected.provider_environment,
        "instrument_version": selected.instrument_version,
        "route_policy_id": selected.route_policy_id,
        "entity_policy_id": selected.entity_policy_id,
        "network_policy_id": selected.network_policy_id,
        "account_class": selected.account_class,
        "release_artifact_id": selected.release_artifact_id,
        "release_artifact_sha256": selected.release_artifact_sha256,
        "reconciliation_semantics_id": selected.reconciliation_semantics_id,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    record = {
        **material,
        "selection_identity": "sha256:" + sha256(encoded).hexdigest(),
    }
    return _require_stored_selection(record, query_binding)


def _require_reconciliation_generation_scope(
    store: JournalStore,
    generation: ReconciliationScopeGeneration | Mapping[str, object] | None,
    query_binding: AuthenticatedReadQueryBinding,
    selection: Mapping[str, object],
    *,
    require_current: bool,
) -> dict[str, object] | None:
    """Bind one durable acquisition generation to the exact provider-origin scope."""

    semantics = selection.get("reconciliation_semantics_id")
    if semantics is None:
        if generation is not None:
            raise ProviderOriginError(
                "reconciliation generation is not permitted without selected reconciliation semantics"
            )
        return None
    if generation is None:
        raise ProviderOriginError(
            "selected reconciliation semantics require a durable reconciliation generation"
        )
    expected = _query_snapshot(query_binding)
    try:
        if type(generation) is ReconciliationScopeGeneration:
            resolved = (
                require_current_reconciliation_scope_generation(store, generation)
                if require_current
                else load_reconciliation_scope_generation_payload(
                    store,
                    reconciliation_scope_generation_payload(generation),
                )
            )
        elif type(generation) is dict:
            resolved = (
                require_current_reconciliation_scope_generation_payload(store, generation)
                if require_current
                else load_reconciliation_scope_generation_payload(store, generation)
            )
        else:
            raise TypeError(
                "reconciliation generation must be exact ReconciliationScopeGeneration or durable payload"
            )
        payload = reconciliation_scope_generation_payload(resolved)
    except (TypeError, ValueError) as error:
        raise ProviderOriginError(
            "reconciliation scope generation is unavailable or no longer authoritative"
        ) from error

    expected_scope = {
        "provider_id": expected["provider_id"],
        "product_family": selection["product_family"],
        "account_id": expected["account_id"],
        "entity_id": expected["entity_id"],
        "environment": expected["environment"],
        "provider_environment": expected["provider_environment"],
        "route_policy_id": selection["route_policy_id"],
        "entity_policy_id": selection["entity_policy_id"],
        "network_policy_id": selection["network_policy_id"],
        "account_class": selection["account_class"],
        "adapter_code_sha": selection["adapter_code_sha"],
        "qualification_id": selection["qualification_id"],
        "capability_snapshot_id": expected["capability_snapshot_id"],
        "reconciliation_semantics_id": semantics,
    }
    if any(payload.get(name) != value for name, value in expected_scope.items()):
        raise ProviderOriginError(
            "reconciliation scope generation conflicts with selected provider/query authority"
        )
    return payload


def _require_generation_precedes_provider_prepare(
    generation_payload: Mapping[str, object] | None,
    prepared_event: Mapping[str, object],
) -> None:
    if generation_payload is None:
        return
    generation_sequence = generation_payload.get("journal_sequence")
    prepared_sequence = prepared_event.get("journal_sequence")
    if (
        type(generation_sequence) is not int
        or generation_sequence <= 0
        or type(prepared_sequence) is not int
        or prepared_sequence <= generation_sequence
    ):
        raise ProviderOriginError(
            "provider-origin Prepared event does not follow its reconciliation generation"
        )


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
    suffix = {
        _PREPARED_EVENT: ":prepared",
        _RETAINED_EVENT: ":retained",
        _OBSERVED_EVENT: ":observed",
    }.get(event_type)
    if suffix is None:
        raise ProviderOriginError("provider-origin journal event type is unsupported")
    expected_event_id = attempt_id + suffix
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
    if (
        event.get("writer_namespace") != _PROTECTED_WRITER_NAMESPACE
        or event.get("writer_authority_id") != _PROTECTED_WRITER_AUTHORITY_ID
        or _SHA256_RE.fullmatch(str(event.get("writer_authority_hash"))) is None
        or _SHA256_RE.fullmatch(str(event.get("writer_provenance_hash"))) is None
    ):
        raise ProviderOriginError(
            "provider-origin event lacks canonical durable writer provenance"
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


_ROUTE_KEYS = frozenset(
    {
        "schema_version",
        "provider_id",
        "environment",
        "provider_environment",
        "surface",
        "endpoint",
        "permission_scope",
        "data_entitlement",
        "success_statuses",
        "network_policy_identity",
        "route_digest",
    }
)


_LEGACY_ROUTE_KEYS = _ROUTE_KEYS - {"network_policy_identity"}


def _validate_legacy_route_snapshot(
    route: object,
    query_binding: AuthenticatedReadQueryBinding,
) -> dict[str, object]:
    """Authenticate the exact pre-network-policy route schema for audit only."""

    if type(route) is not dict or set(route) != _LEGACY_ROUTE_KEYS:
        raise ProviderOriginError(
            "historical authenticated-read route snapshot schema is not exact"
        )
    expected = _query_snapshot(query_binding)
    if route.get("schema_version") != _ROUTE_SCHEMA_VERSION:
        raise ProviderOriginError(
            "historical authenticated-read route snapshot version is unsupported"
        )
    expected_scope = {
        "provider_id": expected["provider_id"],
        "environment": expected["environment"],
        "provider_environment": expected["provider_environment"],
        "surface": expected["surface"],
        "endpoint": expected["endpoint"],
        "permission_scope": expected["permission_scope"],
    }
    for name, value in expected_scope.items():
        if route.get(name) != value:
            raise ProviderOriginError(
                "historical authenticated-read route conflicts with exact query scope"
            )
    entitlement = _exact_text(
        route.get("data_entitlement"),
        name="historical data_entitlement",
    )
    statuses = route.get("success_statuses")
    if (
        type(statuses) is not list
        or not statuses
        or any(
            type(status) is not int
            or isinstance(status, bool)
            or not 200 <= status <= 299
            for status in statuses
        )
        or statuses != sorted(set(statuses))
    ):
        raise ProviderOriginError(
            "historical authenticated-read success statuses are not canonical"
        )
    material = {
        "schema_version": route["schema_version"],
        "provider_id": route["provider_id"],
        "environment": route["environment"],
        "provider_environment": route["provider_environment"],
        "surface": route["surface"],
        "endpoint": route["endpoint"],
        "permission_scope": route["permission_scope"],
        "data_entitlement": entitlement,
        "success_statuses": statuses,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = "sha256:" + sha256(encoded).hexdigest()
    if route.get("route_digest") != digest or _SHA256_RE.fullmatch(digest) is None:
        raise ProviderOriginError(
            "historical authenticated-read route digest conflicts with durable route"
        )
    return {**material, "route_digest": digest}


def _validate_route_snapshot(
    route: object,
    query_binding: AuthenticatedReadQueryBinding,
) -> dict[str, object]:
    if type(route) is not dict or set(route) != _ROUTE_KEYS:
        raise ProviderOriginError(
            "authenticated-read route snapshot schema is not exact"
        )
    expected = _query_snapshot(query_binding)
    if route.get("schema_version") != _ROUTE_SCHEMA_VERSION:
        raise ProviderOriginError(
            "authenticated-read route snapshot version is unsupported"
        )
    expected_scope = {
        "provider_id": expected["provider_id"],
        "environment": expected["environment"],
        "provider_environment": expected["provider_environment"],
        "surface": expected["surface"],
        "endpoint": expected["endpoint"],
        "permission_scope": expected["permission_scope"],
    }
    for name, value in expected_scope.items():
        if route.get(name) != value:
            raise ProviderOriginError(
                "authenticated-read route snapshot conflicts with exact query scope"
            )
    entitlement = _exact_text(
        route.get("data_entitlement"),
        name="data_entitlement",
    )
    statuses = route.get("success_statuses")
    if (
        type(statuses) is not list
        or not statuses
        or any(
            type(status) is not int
            or isinstance(status, bool)
            or not 200 <= status <= 299
            for status in statuses
        )
        or statuses != sorted(set(statuses))
    ):
        raise ProviderOriginError(
            "authenticated-read route success statuses are not canonical"
        )
    network_policy_identity = _exact_text(
        route.get("network_policy_identity"),
        name="route network_policy_identity",
    )
    if _SHA256_RE.fullmatch(network_policy_identity) is None:
        raise ProviderOriginError(
            "authenticated-read route network policy identity is not canonical"
        )
    material = {
        "schema_version": route["schema_version"],
        "provider_id": route["provider_id"],
        "environment": route["environment"],
        "provider_environment": route["provider_environment"],
        "surface": route["surface"],
        "endpoint": route["endpoint"],
        "permission_scope": route["permission_scope"],
        "data_entitlement": entitlement,
        "success_statuses": statuses,
        "network_policy_identity": network_policy_identity,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = "sha256:" + sha256(encoded).hexdigest()
    if route.get("route_digest") != digest or _SHA256_RE.fullmatch(digest) is None:
        raise ProviderOriginError(
            "authenticated-read route digest conflicts with durable route"
        )
    return {**material, "route_digest": digest}


def _current_route_snapshot(
    query_binding: AuthenticatedReadQueryBinding,
) -> dict[str, object]:
    from .provider_transport import resolve_authenticated_read_route_authority

    try:
        route = resolve_authenticated_read_route_authority(query_binding)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ProviderOriginError(
            "authenticated-read route is not currently canonical"
        ) from error
    return _validate_route_snapshot(
        {
            "schema_version": route.schema_version,
            "provider_id": route.provider_id,
            "environment": route.environment,
            "provider_environment": route.provider_environment,
            "surface": route.surface.value,
            "endpoint": route.endpoint,
            "permission_scope": route.permission_scope,
            "data_entitlement": route.data_entitlement,
            "success_statuses": list(route.success_statuses),
            "network_policy_identity": route.network_policy_identity,
            "route_digest": route.route_identity,
        },
        query_binding,
    )

def _require_legacy_prepared_authority(
    payload: Mapping[str, object],
) -> tuple[str, str]:
    if payload.get("origin_kind") != _ORIGIN_KIND:
        raise ProviderOriginError(
            "historical Prepared event is not PROVIDER_ORIGIN evidence"
        )
    transport = _exact_text(
        payload.get("transport_identity"),
        name="historical transport_identity",
    )
    network_policy = _exact_text(
        payload.get("network_policy_identity"),
        name="historical network_policy_identity",
    )
    if _SHA256_RE.fullmatch(network_policy) is None:
        raise ProviderOriginError(
            "historical network_policy_identity must be canonical SHA-256"
        )
    return transport, network_policy


def _require_prepared_authority(
    payload: Mapping[str, object],
    route: Mapping[str, object],
) -> tuple[str, str]:
    if payload.get("origin_kind") != _ORIGIN_KIND:
        raise ProviderOriginError(
            "durable Prepared event is not PROVIDER_ORIGIN authority"
        )
    if payload.get("prepared_subject_schema") != _PREPARED_SUBJECT_SCHEMA_VERSION:
        raise ProviderOriginError(
            "durable Prepared subject schema is not current"
        )
    transport = _exact_text(
        payload.get("transport_identity"),
        name="transport_identity",
    )
    network_policy = _exact_text(
        payload.get("network_policy_identity"),
        name="network_policy_identity",
    )
    if _SHA256_RE.fullmatch(network_policy) is None:
        raise ProviderOriginError(
            "network_policy_identity must be canonical SHA-256"
        )
    if network_policy != route.get("network_policy_identity"):
        raise ProviderOriginError(
            "durable network policy identity conflicts with canonical route authority"
        )
    return transport, network_policy


@dataclass(frozen=True)
class HistoricalProviderOriginEvidence:
    """Authenticated legacy response evidence with no financial promotion authority."""

    evidence_schema: str
    attempt_id: str
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    capability_snapshot_id: str
    query_digest: str
    endpoint: str
    route_digest: str
    data_entitlement: str
    transport_identity: str
    historical_network_policy_identity: str
    prepared_event_id: str
    observed_event_id: str
    observed_at: str
    http_status: int
    response_sha256: str
    response_artifact_id: str
    response_bytes: bytes
    legacy_origin_ref: str
    journal_sequence: int
    _evidence_token: InitVar[object | None] = None

    def __init_subclass__(cls, **kwargs) -> None:
        raise TypeError("HistoricalProviderOriginEvidence must not be subclassed")

    def __post_init__(self, _evidence_token: object | None) -> None:
        if _evidence_token is not _HISTORICAL_EVIDENCE_TOKEN:
            raise ProviderOriginError(
                "historical provider-origin evidence must come from durable journal"
            )
        if self.evidence_schema != "provider-origin-historical:pre-q-network-v1":
            raise ProviderOriginError("historical provider-origin schema is unsupported")
        for name in (
            "attempt_id",
            "provider_id",
            "account_id",
            "environment",
            "provider_environment",
            "capability_snapshot_id",
            "query_digest",
            "endpoint",
            "route_digest",
            "data_entitlement",
            "transport_identity",
            "historical_network_policy_identity",
            "prepared_event_id",
            "observed_event_id",
            "observed_at",
            "response_sha256",
            "response_artifact_id",
            "legacy_origin_ref",
        ):
            _exact_text(getattr(self, name), name=name)
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            raise ProviderOriginError("historical http_status must be exact 100..599")
        if type(self.response_bytes) is not bytes or not self.response_bytes:
            raise ProviderOriginError("historical response bytes must be non-empty")
        try:
            require_provider_response_bytes(
                self.response_bytes,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "historical response exceeds provider response authority budget"
            ) from error
        if "sha256:" + sha256(self.response_bytes).hexdigest() != self.response_sha256:
            raise ProviderOriginError(
                "historical response digest conflicts with exact response bytes"
            )
        for digest, name in (
            (self.query_digest, "query_digest"),
            (self.route_digest, "route_digest"),
            (self.historical_network_policy_identity, "historical_network_policy_identity"),
            (self.response_sha256, "response_sha256"),
        ):
            if _SHA256_RE.fullmatch(digest) is None:
                raise ProviderOriginError(f"{name} must be canonical SHA-256")
        if _ORIGIN_REF_RE.fullmatch(self.legacy_origin_ref) is None:
            raise ProviderOriginError("legacy_origin_ref must be canonical")
        _parse_utc_text(self.observed_at, name="observed_at")
        if type(self.journal_sequence) is not int or self.journal_sequence <= 0:
            raise ProviderOriginError(
                "historical journal_sequence must be exact positive integer"
            )


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
    route_digest: str
    data_entitlement: str
    selection_identity: str
    product_family: str
    adapter_code_sha: str
    qualification_id: str
    route_policy_id: str
    entity_policy_id: str
    qualification_network_policy_id: str
    account_class: str
    release_artifact_id: str | None
    release_artifact_sha256: str | None
    reconciliation_semantics_id: str | None
    transport_identity: str
    network_policy_identity: str
    prepared_event_id: str
    observed_event_id: str
    observed_at: str
    http_status: int
    response_sha256: str
    response_artifact_id: str
    response_bytes: bytes
    origin_ref: str
    journal_sequence: int
    reconciliation_generation: Mapping[str, object] | None = None
    _binding_token: InitVar[object | None] = None

    def __init_subclass__(cls, **kwargs) -> None:
        raise TypeError("AuthenticatedReadResponseBinding must not be subclassed")

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
            "route_digest",
            "data_entitlement",
            "selection_identity",
            "product_family",
            "adapter_code_sha",
            "qualification_id",
            "route_policy_id",
            "entity_policy_id",
            "qualification_network_policy_id",
            "account_class",
            "transport_identity",
            "network_policy_identity",
            "prepared_event_id",
            "observed_event_id",
            "observed_at",
            "response_sha256",
            "response_artifact_id",
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
        if _SHA256_RE.fullmatch(self.route_digest) is None:
            raise ProviderOriginError("route_digest must be canonical")
        if _SHA256_RE.fullmatch(self.selection_identity) is None:
            raise ProviderOriginError("selection_identity must be canonical")
        if re.fullmatch(r"[0-9a-f]{40}", self.adapter_code_sha) is None:
            raise ProviderOriginError("adapter_code_sha must be canonical Git SHA")
        if (self.release_artifact_id is None) != (
            self.release_artifact_sha256 is None
        ):
            raise ProviderOriginError("release artifact identity is incomplete")
        if (
            self.release_artifact_sha256 is not None
            and _SHA256_RE.fullmatch(self.release_artifact_sha256) is None
        ):
            raise ProviderOriginError("release artifact digest must be canonical")
        _optional_exact_text(self.release_artifact_id, name="release_artifact_id")
        _optional_exact_text(
            self.reconciliation_semantics_id,
            name="reconciliation_semantics_id",
        )
        if self.reconciliation_semantics_id is None:
            if self.reconciliation_generation is not None:
                raise ProviderOriginError(
                    "reconciliation generation requires selected reconciliation semantics"
                )
        elif type(self.reconciliation_generation) is not MappingProxyType:
            raise ProviderOriginError(
                "reconciliation generation must be immutable durable payload"
            )
        if _SHA256_RE.fullmatch(self.network_policy_identity) is None:
            raise ProviderOriginError(
                "network_policy_identity must be canonical SHA-256"
            )
        if _ORIGIN_REF_RE.fullmatch(self.origin_ref) is None:
            raise ProviderOriginError("origin_ref must be canonical")
        _parse_utc_text(self.observed_at, name="observed_at")
        if type(self.journal_sequence) is not int or self.journal_sequence <= 0:
            raise ProviderOriginError(
                "journal_sequence must be an exact positive integer"
            )


@dataclass(frozen=True)
class ProviderOriginObservation:
    """Financially distinguishable observation carrying durable origin authority."""

    response_binding: AuthenticatedReadResponseBinding
    observation: ProviderResponseObservation
    _observation_token: InitVar[object | None] = None

    def __init_subclass__(cls, **kwargs) -> None:
        raise TypeError("ProviderOriginObservation must not be subclassed")

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
        if (
            self.observation.provider_environment
            != self.response_binding.provider_environment
        ):
            raise ProviderOriginError("observation provider environment mismatch")

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

    @property
    def data_entitlement(self) -> str:
        return self.response_binding.data_entitlement

    @property
    def selection_identity(self) -> str:
        return self.response_binding.selection_identity

    @property
    def qualification_id(self) -> str:
        return self.response_binding.qualification_id

    @property
    def adapter_code_sha(self) -> str:
        return self.response_binding.adapter_code_sha


class ProviderOriginJournal:
    """Durable authority for one canonical JournalStore generation."""

    def __init__(
        self,
        store: JournalStore,
        *,
        writer_capability: ProtectedWriterCapability,
        response_store: ArtifactStore,
    ) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact canonical JournalStore")
        self._store = store
        self._store_identity = JournalStore.store_identity.__get__(
            store, JournalStore
        )
        try:
            writer = JournalStore._protected_capability_metadata(
                store,
                writer_capability,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise ProviderOriginError(
                "provider-origin durable writer capability is invalid"
            ) from error
        if (
            writer["aggregate_type"] != _AGGREGATE_TYPE
            or writer["namespace"] != _PROTECTED_WRITER_NAMESPACE
            or writer["writer_authority_id"] != _PROTECTED_WRITER_AUTHORITY_ID
        ):
            raise ProviderOriginError(
                "provider-origin durable writer capability has wrong scope"
            )
        self._writer_capability = writer_capability
        if type(response_store) is not ArtifactStore:
            raise TypeError("response_store must be exact canonical ArtifactStore")
        try:
            response_reader = trusted_authenticated_reader(
                response_store.root,
                publication_store=response_store,
            )
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError(
                "provider response artifact authority cannot be bound"
            ) from error
        self._response_store = response_store
        self._response_reader = response_reader

    def _require_store(self) -> JournalStore:
        store = self._store
        if type(store) is not JournalStore:
            raise ProviderOriginError("provider-origin JournalStore authority changed")
        current = JournalStore.store_identity.__get__(store, JournalStore)
        if current != self._store_identity:
            raise ProviderOriginError("provider-origin JournalStore generation changed")
        return store

    def _load_protected_history(
        self,
        store: JournalStore,
        attempt_id: str,
    ) -> list[dict[str, object]]:
        try:
            return JournalStore.load_protected_events(
                store,
                self._writer_capability,
                attempt_id,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise ProviderOriginError(
                "provider-origin durable writer provenance is invalid"
            ) from error

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
        selected_authority: SelectedProviderAuthority,
        transport_identity: str,
        network_policy_identity: str | None = None,
        recorded_at: datetime,
        reconciliation_generation: ReconciliationScopeGeneration | None = None,
    ) -> str:
        snapshot = _query_snapshot(query_binding)
        route = _current_route_snapshot(query_binding)
        selection = _selection_record(selected_authority, query_binding)
        store = self._require_store()
        generation_payload = _require_reconciliation_generation_scope(
            store,
            reconciliation_generation,
            query_binding,
            selection,
            require_current=True,
        )
        expected_journal_sequence = None
        if generation_payload is not None:
            expected_journal_sequence = JournalStore.current_journal_sequence(store)
            if (
                _require_reconciliation_generation_scope(
                    store,
                    reconciliation_generation,
                    query_binding,
                    selection,
                    require_current=True,
                )
                != generation_payload
            ):
                raise ProviderOriginError(
                    "reconciliation scope generation changed before provider-origin prepare"
                )
        transport = _exact_text(transport_identity, name="transport_identity")
        if network_policy_identity is not None:
            compatibility_policy = _exact_text(
                network_policy_identity,
                name="network_policy_identity",
            )
            if _SHA256_RE.fullmatch(compatibility_policy) is None:
                raise ProviderOriginError(
                    "network_policy_identity must be canonical SHA-256"
                )
        # The caller compatibility value is not authority. The exact canonical
        # endpoint policy is resolved by provider_transport and committed into
        # the route digest; this is the only product network identity retained.
        policy = str(route["network_policy_identity"])
        committed_at = _utc_text(recorded_at, name="recorded_at")
        committed_point = _parse_utc_text(committed_at, name="recorded_at")
        if committed_point < _parse_utc_text(
            snapshot["prepared_at"], name="prepared_at"
        ):
            raise ProviderOriginError(
                "provider-origin prepare cannot precede query preparation"
            )
        if (
            generation_payload is not None
            and committed_point
            < _parse_utc_text(
                generation_payload["prepared_at"],
                name="reconciliation_generation.prepared_at",
            )
        ):
            raise ProviderOriginError(
                "provider-origin prepare cannot precede reconciliation generation"
            )
        attempt_id = "provider-read:" + uuid4().hex
        event_id = attempt_id + ":prepared"
        payload = {
            "origin_kind": _ORIGIN_KIND,
            "prepared_subject_schema": _PREPARED_SUBJECT_SCHEMA_VERSION,
            "query": snapshot,
            "transport_identity": transport,
            "network_policy_identity": policy,
            "route": route,
            "selection": selection,
            "reconciliation_generation": generation_payload,
        }
        append_kwargs = {}
        if expected_journal_sequence is not None:
            append_kwargs["expected_journal_sequence"] = expected_journal_sequence
        try:
            JournalStore.append_protected_event(
                store,
                self._writer_capability,
                self._event(
                    event_id=event_id,
                    event_type=_PREPARED_EVENT,
                    attempt_id=attempt_id,
                    aggregate_version=1,
                    payload=payload,
                    committed_at=committed_at,
                ),
                **append_kwargs,
            )
        except JournalSequencePreconditionFailed as error:
            raise ProviderOriginError(
                "journal changed after reconciliation-generation validation"
            ) from error
        return attempt_id

    def _record_provider_origin(
        self,
        attempt_id: str,
        query_binding: AuthenticatedReadQueryBinding,
        *,
        wire_receipt: object,
    ) -> AuthenticatedReadResponseBinding:
        from .provider_transport import (
            ProviderTransportError,
            ProviderTransportScopeError,
            retire_product_authenticated_read_receipt,
            validate_product_authenticated_read_receipt,
        )

        attempt = _exact_text(attempt_id, name="attempt_id")
        try:
            (
                receipt_transport_identity,
                receipt_network_policy_identity,
                http_status,
                raw,
                observed_at,
            ) = validate_product_authenticated_read_receipt(
                wire_receipt,
                query_binding,
            )
        except (
            ProviderTransportError,
            ProviderTransportScopeError,
            TypeError,
            ValueError,
        ) as error:
            raise ProviderOriginError(
                "provider-origin response lacks factory-issued wire execution evidence"
            ) from error
        if type(http_status) is not int or not 100 <= http_status <= 599:
            raise ProviderOriginError("http_status must be exact integer 100..599")
        if type(raw) is not bytes or not raw:
            raise ProviderOriginError("response_bytes must be exact non-empty bytes")
        try:
            raw = require_provider_response_bytes(
                raw,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "response_bytes exceed provider response authority budget"
            ) from error
        observed_text = _utc_text(observed_at, name="observed_at")
        store = self._require_store()
        events = self._load_protected_history(store, attempt)
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
        durable_route = _validate_route_snapshot(payload.get("route"), query_binding)
        selection = _require_stored_selection(payload.get("selection"), query_binding)
        generation_payload = _require_reconciliation_generation_scope(
            store,
            payload.get("reconciliation_generation"),
            query_binding,
            selection,
            require_current=False,
        )
        _require_generation_precedes_provider_prepare(generation_payload, prepared)
        current_route = _current_route_snapshot(query_binding)
        if current_route != durable_route:
            raise ProviderOriginError(
                "authenticated-read route changed after durable prepare"
            )
        transport, network_policy = _require_prepared_authority(
            payload, durable_route
        )
        if (
            receipt_transport_identity != transport
            or receipt_network_policy_identity != network_policy
        ):
            raise ProviderOriginError(
                "factory-issued wire receipt conflicts with durable Prepared authority"
            )
        prepared_at = _parse_utc_text(
            prepared.get("committed_at"), name="prepared committed_at"
        )
        observed_point = _parse_utc_text(observed_text, name="observed_at")
        if observed_point < prepared_at:
            raise ProviderOriginError(
                "provider response observation cannot precede durable prepare"
            )
        response_digest = "sha256:" + sha256(raw).hexdigest()
        response_artifact_id = _response_artifact_id(
            attempt_id=attempt,
            query_digest=expected["query_digest"],
            response_sha256=response_digest,
        )
        prepared_subject_digest = _exact_text(
            prepared.get("payload_hash"),
            name="prepared_subject_digest",
        )
        if _SHA256_RE.fullmatch(prepared_subject_digest) is None:
            raise ProviderOriginError(
                "durable Prepared subject digest is not canonical"
            )
        response_metadata = {
            "evidence_kind": "PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt,
            "prepared_subject_digest": prepared_subject_digest,
            "provider_id": expected["provider_id"],
            "provider_environment": expected["provider_environment"],
            "query_digest": expected["query_digest"],
            "route_digest": durable_route["route_digest"],
            "data_entitlement": durable_route["data_entitlement"],
            "selection_identity": selection["selection_identity"],
            "qualification_id": selection["qualification_id"],
            "adapter_code_sha": selection["adapter_code_sha"],
            "route_policy_id": selection["route_policy_id"],
            "entity_policy_id": selection["entity_policy_id"],
            "qualification_network_policy_id": selection["network_policy_id"],
            "account_class": selection["account_class"],
            "release_artifact_id": selection["release_artifact_id"],
            "release_artifact_sha256": selection["release_artifact_sha256"],
            "reconciliation_semantics_id": selection["reconciliation_semantics_id"],
            "reconciliation_generation": generation_payload,
        }
        try:
            manifest = self._response_store.publish_bytes(
                artifact_id=response_artifact_id,
                data=raw,
                media_type="application/octet-stream",
                rights={
                    "storage": True,
                    "export": False,
                    "rights_id": "provider-origin-response:v1",
                },
                source_refs=[],
                metadata=response_metadata,
            )
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError(
                "provider response bytes could not be retained durably"
            ) from error
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != response_artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("metadata") != response_metadata
        ):
            raise ProviderOriginError(
                "retained provider response manifest conflicts with exact response"
            )
        retained_event_id = attempt + ":retained"
        retained_payload = {
            "origin_kind": _ORIGIN_KIND,
            "prepared_event_id": prepared.get("event_id"),
            "prepared_subject_digest": prepared_subject_digest,
            "query_digest": expected["query_digest"],
            "transport_identity": transport,
            "network_policy_identity": network_policy,
            "route_digest": durable_route["route_digest"],
            "selection_identity": selection["selection_identity"],
            "reconciliation_generation": generation_payload,
            "http_status": http_status,
            "response_sha256": response_digest,
            "response_artifact_id": response_artifact_id,
            "observed_at": observed_text,
        }
        JournalStore.append_protected_event(
            store,
            self._writer_capability,
            self._event(
                event_id=retained_event_id,
                event_type=_RETAINED_EVENT,
                attempt_id=attempt,
                aggregate_version=2,
                payload=retained_payload,
                committed_at=observed_text,
            ),
        )
        # Retained is now the durable crash-recovery authority for this exact
        # wire fact.  The in-process receipt must become one-shot at this cut:
        # terminal Observed publication no longer needs it, and keeping it live
        # across an Observed commit failure permits replay into another attempt.
        try:
            retire_product_authenticated_read_receipt(wire_receipt)
        except ProviderTransportScopeError as error:
            raise ProviderOriginError(
                "durably retained provider response could not retire wire receipt"
            ) from error

        observed_event_id = attempt + ":observed"
        observed_payload = {
            **retained_payload,
            "retained_event_id": retained_event_id,
        }
        JournalStore.append_protected_event(
            store,
            self._writer_capability,
            self._event(
                event_id=observed_event_id,
                event_type=_OBSERVED_EVENT,
                attempt_id=attempt,
                aggregate_version=3,
                payload=observed_payload,
                committed_at=observed_text,
            ),
        )
        return self.load_response_binding(attempt, query_binding)

    def recover_response_binding(
        self,
        attempt_id: str,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> AuthenticatedReadResponseBinding:
        """Finish Retained -> Observed after restart with zero provider re-query.

        Prepared-only state proves no definitive response.  Prepared+Retained
        proves the exact response under the protected writer and authenticates
        its bytes through ArtifactStore.  Recovery accepts no wire client,
        receipt, response bytes, status or observed time from the caller.
        """

        attempt = _exact_text(attempt_id, name="attempt_id")
        expected = _query_snapshot(query_binding)
        store = self._require_store()
        events = self._load_protected_history(store, attempt)
        if len(events) == 3:
            return self.load_response_binding(attempt, query_binding)
        if len(events) != 2:
            raise ProviderOriginError(
                "provider-origin recovery requires exact Prepared + Retained state"
            )
        prepared, retained = events
        prepared_payload = _require_origin_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        _require_snapshot(prepared_payload, query_binding)
        durable_route = _validate_route_snapshot(
            prepared_payload.get("route"),
            query_binding,
        )
        selection = _require_stored_selection(
            prepared_payload.get("selection"),
            query_binding,
        )
        generation_payload = _require_reconciliation_generation_scope(
            store,
            prepared_payload.get("reconciliation_generation"),
            query_binding,
            selection,
            require_current=False,
        )
        _require_generation_precedes_provider_prepare(generation_payload, prepared)
        transport, network_policy = _require_prepared_authority(
            prepared_payload,
            durable_route,
        )
        retained_payload = _require_origin_event(
            retained,
            attempt_id=attempt,
            event_type=_RETAINED_EVENT,
            aggregate_version=2,
            payload_keys=_RETAINED_PAYLOAD_KEYS,
        )
        prepared_subject_digest = _exact_text(
            prepared.get("payload_hash"),
            name="prepared_subject_digest",
        )
        if _SHA256_RE.fullmatch(prepared_subject_digest) is None:
            raise ProviderOriginError(
                "durable Prepared subject digest is not canonical"
            )
        exact_scope = {
            "origin_kind": _ORIGIN_KIND,
            "prepared_event_id": prepared.get("event_id"),
            "prepared_subject_digest": prepared_subject_digest,
            "query_digest": expected["query_digest"],
            "transport_identity": transport,
            "network_policy_identity": network_policy,
            "route_digest": durable_route["route_digest"],
            "selection_identity": selection["selection_identity"],
            "reconciliation_generation": generation_payload,
        }
        if any(
            retained_payload.get(name) != value
            for name, value in exact_scope.items()
        ):
            raise ProviderOriginError(
                "provider-origin recovery Retained scope conflicts with Prepared"
            )
        status = retained_payload.get("http_status")
        if type(status) is not int or not 100 <= status <= 599:
            raise ProviderOriginError("retained provider HTTP status is invalid")
        response_digest = retained_payload.get("response_sha256")
        if (
            type(response_digest) is not str
            or _SHA256_RE.fullmatch(response_digest) is None
        ):
            raise ProviderOriginError(
                "retained provider response digest is not canonical"
            )
        observed_text = retained_payload.get("observed_at")
        observed_point = _parse_utc_text(
            observed_text,
            name="retained observed_at",
        )
        prepared_point = _parse_utc_text(
            prepared.get("committed_at"),
            name="prepared committed_at",
        )
        if observed_point < prepared_point:
            raise ProviderOriginError(
                "retained provider response observation precedes durable prepare"
            )
        if retained.get("committed_at") != observed_text:
            raise ProviderOriginError(
                "retained response event timestamp conflicts with payload"
            )
        response_artifact_id = retained_payload.get("response_artifact_id")
        expected_artifact_id = _response_artifact_id(
            attempt_id=attempt,
            query_digest=expected["query_digest"],
            response_sha256=response_digest,
        )
        if response_artifact_id != expected_artifact_id:
            raise ProviderOriginError(
                "retained provider response artifact identity conflicts with response subject"
            )
        expected_metadata = {
            "evidence_kind": "PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt,
            "prepared_subject_digest": prepared_subject_digest,
            "provider_id": expected["provider_id"],
            "provider_environment": expected["provider_environment"],
            "query_digest": expected["query_digest"],
            "route_digest": durable_route["route_digest"],
            "data_entitlement": durable_route["data_entitlement"],
            "selection_identity": selection["selection_identity"],
            "qualification_id": selection["qualification_id"],
            "adapter_code_sha": selection["adapter_code_sha"],
            "route_policy_id": selection["route_policy_id"],
            "entity_policy_id": selection["entity_policy_id"],
            "qualification_network_policy_id": selection["network_policy_id"],
            "account_class": selection["account_class"],
            "release_artifact_id": selection["release_artifact_id"],
            "release_artifact_sha256": selection["release_artifact_sha256"],
            "reconciliation_semantics_id": selection["reconciliation_semantics_id"],
            "reconciliation_generation": generation_payload,
        }
        try:
            manifest, raw = self._response_reader(response_artifact_id)
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError(
                "retained provider response artifact cannot be authenticated"
            ) from error
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != response_artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("media_type") != "application/octet-stream"
            or manifest.get("metadata") != expected_metadata
            or manifest.get("rights")
            != {
                "storage": True,
                "export": False,
                "rights_id": "provider-origin-response:v1",
            }
            or manifest.get("source_refs") != []
        ):
            raise ProviderOriginError(
                "retained provider response artifact manifest conflicts with response subject"
            )
        if type(raw) is not bytes or not raw:
            raise ProviderOriginError("retained provider response bytes are empty")
        try:
            require_provider_response_bytes(
                raw,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "retained provider response exceeds authority budget"
            ) from error
        if "sha256:" + sha256(raw).hexdigest() != response_digest:
            raise ProviderOriginError(
                "retained provider response digest conflicts with exact artifact bytes"
            )
        observed_payload = {
            **retained_payload,
            "retained_event_id": retained.get("event_id"),
        }
        JournalStore.append_protected_event(
            store,
            self._writer_capability,
            self._event(
                event_id=attempt + ":observed",
                event_type=_OBSERVED_EVENT,
                attempt_id=attempt,
                aggregate_version=3,
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
        events = self._load_protected_history(store, attempt)
        if len(events) != 3:
            raise ProviderOriginError(
                "provider-origin response is incomplete; Prepared, Retained and Observed are required"
            )
        prepared, retained, observed = events
        prepared_payload = _require_origin_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        _require_snapshot(prepared_payload, query_binding)
        durable_route = _validate_route_snapshot(
            prepared_payload.get("route"),
            query_binding,
        )
        selection = _require_stored_selection(
            prepared_payload.get("selection"),
            query_binding,
        )
        generation_payload = _require_reconciliation_generation_scope(
            store,
            prepared_payload.get("reconciliation_generation"),
            query_binding,
            selection,
            require_current=False,
        )
        _require_generation_precedes_provider_prepare(generation_payload, prepared)
        transport, network_policy = _require_prepared_authority(
            prepared_payload, durable_route
        )
        retained_payload = _require_origin_event(
            retained,
            attempt_id=attempt,
            event_type=_RETAINED_EVENT,
            aggregate_version=2,
            payload_keys=_RETAINED_PAYLOAD_KEYS,
        )
        observed_payload = _require_origin_event(
            observed,
            attempt_id=attempt,
            event_type=_OBSERVED_EVENT,
            aggregate_version=3,
            payload_keys=_OBSERVED_PAYLOAD_KEYS,
        )
        prepared_subject_digest = _exact_text(
            prepared.get("payload_hash"),
            name="prepared_subject_digest",
        )
        if _SHA256_RE.fullmatch(prepared_subject_digest) is None:
            raise ProviderOriginError(
                "durable Prepared subject digest is not canonical"
            )
        expected_retained_scope = {
            "origin_kind": _ORIGIN_KIND,
            "prepared_event_id": prepared.get("event_id"),
            "prepared_subject_digest": prepared_subject_digest,
            "query_digest": expected["query_digest"],
            "transport_identity": transport,
            "network_policy_identity": network_policy,
            "route_digest": durable_route["route_digest"],
            "selection_identity": selection["selection_identity"],
            "reconciliation_generation": generation_payload,
        }
        if any(
            retained_payload.get(name) != value
            for name, value in expected_retained_scope.items()
        ):
            raise ProviderOriginError(
                "provider-origin Retained event conflicts with durable Prepared scope"
            )
        if (
            observed_payload.get("retained_event_id") != retained.get("event_id")
            or {
                name: value
                for name, value in observed_payload.items()
                if name != "retained_event_id"
            }
            != retained_payload
        ):
            raise ProviderOriginError(
                "provider-origin Observed event conflicts with protected Retained response"
            )
        status = observed_payload.get("http_status")
        if type(status) is not int or not 100 <= status <= 599:
            raise ProviderOriginError("durable provider HTTP status is invalid")
        response_digest = observed_payload.get("response_sha256")
        if type(response_digest) is not str or _SHA256_RE.fullmatch(response_digest) is None:
            raise ProviderOriginError(
                "durable provider response digest is not canonical"
            )
        response_artifact_id = observed_payload.get("response_artifact_id")
        expected_artifact_id = _response_artifact_id(
            attempt_id=attempt,
            query_digest=expected["query_digest"],
            response_sha256=response_digest,
        )
        if response_artifact_id != expected_artifact_id:
            raise ProviderOriginError(
                "durable provider response artifact identity conflicts with response subject"
            )
        expected_metadata = {
            "evidence_kind": "PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt,
            "prepared_subject_digest": prepared_subject_digest,
            "provider_id": expected["provider_id"],
            "provider_environment": expected["provider_environment"],
            "query_digest": expected["query_digest"],
            "route_digest": durable_route["route_digest"],
            "data_entitlement": durable_route["data_entitlement"],
            "selection_identity": selection["selection_identity"],
            "qualification_id": selection["qualification_id"],
            "adapter_code_sha": selection["adapter_code_sha"],
            "route_policy_id": selection["route_policy_id"],
            "entity_policy_id": selection["entity_policy_id"],
            "qualification_network_policy_id": selection["network_policy_id"],
            "account_class": selection["account_class"],
            "release_artifact_id": selection["release_artifact_id"],
            "release_artifact_sha256": selection["release_artifact_sha256"],
            "reconciliation_semantics_id": selection["reconciliation_semantics_id"],
            "reconciliation_generation": generation_payload,
        }
        try:
            manifest, raw = self._response_reader(response_artifact_id)
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError(
                "durable provider response artifact cannot be authenticated"
            ) from error
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != response_artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("media_type") != "application/octet-stream"
            or manifest.get("metadata") != expected_metadata
            or manifest.get("rights")
            != {
                "storage": True,
                "export": False,
                "rights_id": "provider-origin-response:v1",
            }
            or manifest.get("source_refs") != []
        ):
            raise ProviderOriginError(
                "durable provider response artifact manifest conflicts with response subject"
            )
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
        if "sha256:" + sha256(raw).hexdigest() != response_digest:
            raise ProviderOriginError(
                "durable provider response digest conflicts with exact artifact bytes"
            )
        observed_text = observed_payload.get("observed_at")
        observed_point = _parse_utc_text(observed_text, name="observed_at")
        prepared_point = _parse_utc_text(
            prepared.get("committed_at"),
            name="prepared committed_at",
        )
        if observed_point < prepared_point:
            raise ProviderOriginError(
                "provider response observation precedes durable prepare"
            )
        if retained.get("committed_at") != observed_text:
            raise ProviderOriginError(
                "provider-origin retained event timestamp conflicts with payload"
            )
        if observed.get("committed_at") != observed_text:
            raise ProviderOriginError(
                "provider-origin observed event timestamp conflicts with payload"
            )
        retained_sequence = retained.get("journal_sequence")
        journal_sequence = observed.get("journal_sequence")
        if (
            type(retained_sequence) is not int
            or retained_sequence <= 0
            or type(journal_sequence) is not int
            or journal_sequence <= retained_sequence
        ):
            raise ProviderOriginError(
                "provider-origin Retained/Observed journal chronology is invalid"
            )
        origin_subject = {
            "schema_version": _ORIGIN_SUBJECT_SCHEMA_VERSION,
            "attempt_id": attempt,
            "prepared_event_id": str(prepared.get("event_id")),
            "observed_event_id": str(observed.get("event_id")),
            "query_digest": expected["query_digest"],
            "route_digest": str(durable_route["route_digest"]),
            "selection_identity": str(selection["selection_identity"]),
            "reconciliation_generation": generation_payload,
            "http_status": status,
            "observed_at": observed_text,
            "transport_identity": transport,
            "network_policy_identity": network_policy,
            "response_artifact_id": response_artifact_id,
            "response_sha256": response_digest,
            "journal_sequence": journal_sequence,
            "prepared_writer_provenance_hash": str(
                prepared.get("writer_provenance_hash")
            ),
            "observed_writer_provenance_hash": str(
                observed.get("writer_provenance_hash")
            ),
        }
        origin_material = json.dumps(
            origin_subject,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        origin_ref = "provider-origin:sha256:" + sha256(origin_material).hexdigest()
        return AuthenticatedReadResponseBinding(
            attempt_id=attempt,
            provider_id=expected["provider_id"],
            account_id=expected["account_id"],
            environment=expected["environment"],
            provider_environment=expected["provider_environment"],
            capability_snapshot_id=expected["capability_snapshot_id"],
            query_digest=expected["query_digest"],
            endpoint=expected["endpoint"],
            route_digest=str(durable_route["route_digest"]),
            data_entitlement=str(durable_route["data_entitlement"]),
            selection_identity=str(selection["selection_identity"]),
            product_family=str(selection["product_family"]),
            adapter_code_sha=str(selection["adapter_code_sha"]),
            qualification_id=str(selection["qualification_id"]),
            route_policy_id=str(selection["route_policy_id"]),
            entity_policy_id=str(selection["entity_policy_id"]),
            qualification_network_policy_id=str(selection["network_policy_id"]),
            account_class=str(selection["account_class"]),
            release_artifact_id=selection["release_artifact_id"],
            release_artifact_sha256=selection["release_artifact_sha256"],
            reconciliation_semantics_id=selection["reconciliation_semantics_id"],
            reconciliation_generation=(
                None
                if generation_payload is None
                else MappingProxyType(dict(generation_payload))
            ),
            transport_identity=transport,
            network_policy_identity=network_policy,
            prepared_event_id=prepared["event_id"],
            observed_event_id=observed["event_id"],
            observed_at=observed_text,
            http_status=status,
            response_sha256=response_digest,
            response_artifact_id=response_artifact_id,
            response_bytes=raw,
            origin_ref=origin_ref,
            journal_sequence=journal_sequence,
            _binding_token=_BINDING_TOKEN,
        )


    def load_historical_response_evidence(
        self,
        attempt_id: str,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> HistoricalProviderOriginEvidence:
        """Read exact pre-Q/network-route evidence without upgrading its authority."""

        attempt = _exact_text(attempt_id, name="attempt_id")
        expected = _query_snapshot(query_binding)
        store = self._require_store()
        events = self._load_protected_history(store, attempt)
        if len(events) != 2:
            raise ProviderOriginError(
                "historical provider-origin response requires Prepared and Observed"
            )
        prepared, observed = events
        prepared_payload = _require_origin_event(
            prepared,
            attempt_id=attempt,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_LEGACY_PREPARED_PAYLOAD_KEYS,
        )
        _require_snapshot(prepared_payload, query_binding)
        durable_route = _validate_legacy_route_snapshot(
            prepared_payload.get("route"),
            query_binding,
        )
        transport, historical_network_policy = _require_legacy_prepared_authority(
            prepared_payload
        )
        observed_payload = _require_origin_event(
            observed,
            attempt_id=attempt,
            event_type=_OBSERVED_EVENT,
            aggregate_version=2,
            payload_keys=_LEGACY_OBSERVED_PAYLOAD_KEYS,
        )
        if (
            observed_payload.get("origin_kind") != _ORIGIN_KIND
            or observed_payload.get("prepared_event_id") != prepared.get("event_id")
            or observed_payload.get("query_digest") != expected["query_digest"]
            or observed_payload.get("transport_identity") != transport
            or observed_payload.get("network_policy_identity")
            != historical_network_policy
            or observed_payload.get("route_digest") != durable_route["route_digest"]
        ):
            raise ProviderOriginError(
                "historical Observed event conflicts with durable Prepared scope"
            )
        status = observed_payload.get("http_status")
        if type(status) is not int or not 100 <= status <= 599:
            raise ProviderOriginError("historical provider HTTP status is invalid")
        response_digest = observed_payload.get("response_sha256")
        if (
            type(response_digest) is not str
            or _SHA256_RE.fullmatch(response_digest) is None
        ):
            raise ProviderOriginError(
                "historical provider response digest is not canonical"
            )
        response_artifact_id = observed_payload.get("response_artifact_id")
        expected_artifact_id = _response_artifact_id(
            attempt_id=attempt,
            query_digest=expected["query_digest"],
            response_sha256=response_digest,
        )
        if response_artifact_id != expected_artifact_id:
            raise ProviderOriginError(
                "historical response artifact identity conflicts with response subject"
            )
        expected_metadata = {
            "evidence_kind": "PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt,
            "provider_id": expected["provider_id"],
            "provider_environment": expected["provider_environment"],
            "query_digest": expected["query_digest"],
            "route_digest": durable_route["route_digest"],
            "data_entitlement": durable_route["data_entitlement"],
        }
        try:
            manifest, raw = self._response_reader(response_artifact_id)
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderOriginError(
                "historical provider response artifact cannot be authenticated"
            ) from error
        if (
            type(manifest) is not dict
            or manifest.get("artifact_id") != response_artifact_id
            or manifest.get("sha256") != response_digest
            or manifest.get("media_type") != "application/octet-stream"
            or manifest.get("metadata") != expected_metadata
            or manifest.get("rights")
            != {
                "storage": True,
                "export": False,
                "rights_id": "provider-origin-response:v1",
            }
            or manifest.get("source_refs") != []
        ):
            raise ProviderOriginError(
                "historical provider response artifact manifest conflicts with subject"
            )
        if type(raw) is not bytes or not raw:
            raise ProviderOriginError("historical provider response bytes are empty")
        try:
            require_provider_response_bytes(
                raw,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderOriginError(
                "historical provider response exceeds authority budget"
            ) from error
        if "sha256:" + sha256(raw).hexdigest() != response_digest:
            raise ProviderOriginError(
                "historical provider response digest conflicts with exact bytes"
            )
        observed_text = observed_payload.get("observed_at")
        _parse_utc_text(observed_text, name="observed_at")
        if observed.get("committed_at") != observed_text:
            raise ProviderOriginError(
                "historical observed event timestamp conflicts with payload"
            )
        journal_sequence = observed.get("journal_sequence")
        if type(journal_sequence) is not int or journal_sequence <= 0:
            raise ProviderOriginError(
                "historical observed journal sequence is invalid"
            )

        # Preserve the legacy identity exactly for audit. It is deliberately
        # named legacy_origin_ref and is never accepted by financial promotion.
        legacy_material = (
            attempt
            + "\n"
            + str(prepared.get("event_id"))
            + "\n"
            + str(observed.get("event_id"))
            + "\n"
            + expected["query_digest"]
            + "\n"
            + str(durable_route["route_digest"])
            + "\n"
            + str(status)
            + "\n"
            + transport
            + "\n"
            + historical_network_policy
            + "\n"
            + response_artifact_id
            + "\n"
            + response_digest
            + "\n"
            + str(journal_sequence)
            + "\n"
            + str(prepared.get("writer_provenance_hash"))
            + "\n"
            + str(observed.get("writer_provenance_hash"))
        ).encode("utf-8")
        legacy_origin_ref = (
            "provider-origin:sha256:" + sha256(legacy_material).hexdigest()
        )
        return HistoricalProviderOriginEvidence(
            evidence_schema="provider-origin-historical:pre-q-network-v1",
            attempt_id=attempt,
            provider_id=expected["provider_id"],
            account_id=expected["account_id"],
            environment=expected["environment"],
            provider_environment=expected["provider_environment"],
            capability_snapshot_id=expected["capability_snapshot_id"],
            query_digest=expected["query_digest"],
            endpoint=expected["endpoint"],
            route_digest=str(durable_route["route_digest"]),
            data_entitlement=str(durable_route["data_entitlement"]),
            transport_identity=transport,
            historical_network_policy_identity=historical_network_policy,
            prepared_event_id=prepared["event_id"],
            observed_event_id=observed["event_id"],
            observed_at=observed_text,
            http_status=status,
            response_sha256=response_digest,
            response_artifact_id=response_artifact_id,
            response_bytes=raw,
            legacy_origin_ref=legacy_origin_ref,
            journal_sequence=journal_sequence,
            _evidence_token=_HISTORICAL_EVIDENCE_TOKEN,
        )


def _selected_authority_from_durable_response(
    response_binding: AuthenticatedReadResponseBinding,
    query_binding: AuthenticatedReadQueryBinding,
) -> SelectedProviderAuthority:
    """Reconstruct the exact prior Q1+C1 decision for currentness revalidation."""

    selected = SelectedProviderAuthority(
        provider_id=response_binding.provider_id,
        product_family=response_binding.product_family,
        adapter_code_sha=response_binding.adapter_code_sha,
        qualification_id=response_binding.qualification_id,
        capability_snapshot_id=response_binding.capability_snapshot_id,
        account_id=response_binding.account_id,
        entity_id=query_binding.entity_id,
        environment=response_binding.environment,
        provider_environment=response_binding.provider_environment,
        instrument_version=query_binding.instrument_version,
        route_policy_id=response_binding.route_policy_id,
        entity_policy_id=response_binding.entity_policy_id,
        network_policy_id=response_binding.qualification_network_policy_id,
        account_class=response_binding.account_class,
        release_artifact_id=response_binding.release_artifact_id,
        release_artifact_sha256=response_binding.release_artifact_sha256,
        reconciliation_semantics_id=response_binding.reconciliation_semantics_id,
    )
    record = _selection_record(selected, query_binding)
    if record["selection_identity"] != response_binding.selection_identity:
        raise ProviderOriginError(
            "durable provider response selection identity cannot be reconstructed"
        )
    return selected


def _require_query_binding_from_current_capability(
    query_binding: AuthenticatedReadQueryBinding,
    capability: CapabilitySnapshot,
) -> None:
    """Re-derive the exact query binding from canonical current capability truth."""

    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise ProviderOriginError(
            "query_binding must be exact AuthenticatedReadQueryBinding"
        )
    if type(capability) is not CapabilitySnapshot:
        raise ProviderOriginError(
            "current capability must be exact CapabilitySnapshot"
        )
    prepared_at = _parse_utc_text(
        query_binding.prepared_at,
        name="query_binding.prepared_at",
    )
    try:
        canonical = prepare_authenticated_read_query(
            capability=capability,
            surface=query_binding.surface,
            endpoint=query_binding.endpoint,
            query=query_binding.query,
            at=prepared_at,
            permission_scope=query_binding.permission_scope,
            provider_environment=query_binding.provider_environment,
        )
    except (ProviderCoreError, TypeError, ValueError) as error:
        raise ProviderOriginError(
            "authenticated-read query cannot be re-derived from current capability"
        ) from error
    if canonical != query_binding:
        raise ProviderOriginError(
            "authenticated-read query binding does not match current capability authority"
        )



def record_product_authenticated_read_origin(
    *,
    origin_journal: ProviderOriginJournal,
    product_transport: object,
    query_binding: AuthenticatedReadQueryBinding,
    selected_authority: SelectedProviderAuthority,
    qualification_reader: ProviderQualificationCurrentReader,
    capability_registry: CapabilityRegistry,
    reconciliation_generation: ReconciliationScopeGeneration | None = None,
) -> AuthenticatedReadResponseBinding:
    """Bind current Q+C and product Prepared authority around one exact wire read."""

    from .provider_transport import (
        ProviderTransportError,
        ProviderTransportScopeError,
        execute_product_authenticated_read,
        product_authenticated_read_prepared_authority,
    )

    if type(origin_journal) is not ProviderOriginJournal:
        raise TypeError("origin_journal must be exact ProviderOriginJournal")
    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError("query_binding must be exact AuthenticatedReadQueryBinding")
    if type(selected_authority) is not SelectedProviderAuthority:
        raise TypeError("selected_authority must be exact SelectedProviderAuthority")
    if type(qualification_reader) is not ProviderQualificationCurrentReader:
        raise TypeError(
            "qualification_reader must be exact ProviderQualificationCurrentReader"
        )
    if type(capability_registry) is not CapabilityRegistry:
        raise TypeError("capability_registry must be exact CapabilityRegistry")
    if (
        selected_authority.reconciliation_semantics_id is not None
        and type(reconciliation_generation) is not ReconciliationScopeGeneration
    ):
        raise ProviderOriginError(
            "reconciliation-capable provider origin requires exact durable generation"
        )
    if (
        selected_authority.reconciliation_semantics_id is None
        and reconciliation_generation is not None
    ):
        raise ProviderOriginError(
            "reconciliation generation is not permitted without selected reconciliation semantics"
        )

    try:
        first_prepared = product_authenticated_read_prepared_authority(
            product_transport,
            query_binding,
        )
        first_point = _parse_utc_text(
            first_prepared["validated_at"],
            name="prepared_authority.validated_at",
        )
    except (
        KeyError,
        ProviderTransportError,
        ProviderTransportScopeError,
        TypeError,
        ValueError,
    ) as error:
        raise ProviderOriginError(
            "factory-issued authenticated-read prepared authority is unavailable"
        ) from error

    try:
        first_qualification, first_capability = revalidate_selected_provider_authority(
            selected_authority,
            qualification_reader=qualification_reader,
            capability_registry=capability_registry,
            at=first_point,
        )
    except (ProviderSelectionError, TypeError, ValueError) as error:
        raise ProviderOriginError(
            "selected provider Q+C authority is not current before durable prepare"
        ) from error
    _require_query_binding_from_current_capability(
        query_binding,
        first_capability,
    )

    attempt_id = origin_journal.prepare(
        query_binding,
        selected_authority=selected_authority,
        transport_identity=first_prepared["transport_identity"],
        network_policy_identity=first_prepared["network_policy_identity"],
        recorded_at=first_point,
        reconciliation_generation=reconciliation_generation,
    )

    try:
        second_prepared = product_authenticated_read_prepared_authority(
            product_transport,
            query_binding,
        )
        second_point = _parse_utc_text(
            second_prepared["validated_at"],
            name="prepared_authority.validated_at",
        )
    except (
        KeyError,
        ProviderTransportError,
        ProviderTransportScopeError,
        TypeError,
        ValueError,
    ) as error:
        raise ProviderOriginError(
            "factory-issued authenticated-read prepared authority changed after durable prepare"
        ) from error

    first_stable = {
        key: value
        for key, value in first_prepared.items()
        if key != "validated_at"
    }
    second_stable = {
        key: value
        for key, value in second_prepared.items()
        if key != "validated_at"
    }
    if second_point < first_point or second_stable != first_stable:
        raise ProviderOriginError(
            "factory-issued authenticated-read prepared authority changed during durable prepare"
        )

    try:
        second_qualification, second_capability = revalidate_selected_provider_authority(
            selected_authority,
            qualification_reader=qualification_reader,
            capability_registry=capability_registry,
            at=second_point,
        )
    except (ProviderSelectionError, TypeError, ValueError) as error:
        raise ProviderOriginError(
            "selected provider Q+C authority changed after durable prepare"
        ) from error
    if (
        second_qualification != first_qualification
        or second_capability != first_capability
    ):
        raise ProviderOriginError(
            "selected provider Q+C authority changed during authenticated-read prepare"
        )
    _require_query_binding_from_current_capability(
        query_binding,
        second_capability,
    )
    if reconciliation_generation is not None:
        _require_reconciliation_generation_scope(
            origin_journal._require_store(),
            reconciliation_generation,
            query_binding,
            _selection_record(selected_authority, query_binding),
            require_current=True,
        )

    try:
        receipt = execute_product_authenticated_read(
            product_transport,
            query_binding,
        )
    except (
        ProviderTransportError,
        ProviderTransportScopeError,
        TypeError,
        ValueError,
    ) as error:
        raise ProviderOriginError(
            "factory-issued authenticated read did not produce durable origin evidence"
        ) from error
    return origin_journal._record_provider_origin(
        attempt_id,
        query_binding,
        wire_receipt=receipt,
    )
def observe_provider_origin_json_response(
    *,
    journal: ProviderOriginJournal,
    response_binding: AuthenticatedReadResponseBinding,
    query_binding: AuthenticatedReadQueryBinding,
    required_data_entitlement: str,
    qualification_reader: ProviderQualificationCurrentReader,
    capability_registry: CapabilityRegistry,
) -> ProviderOriginObservation:
    if type(journal) is not ProviderOriginJournal:
        raise ProviderOriginError(
            "provider-origin financial promotion requires exact durable journal authority"
        )
    if type(response_binding) is not AuthenticatedReadResponseBinding:
        raise ProviderOriginError(
            "response_binding must be exact AuthenticatedReadResponseBinding"
        )
    durable_binding = journal.load_response_binding(
        response_binding.attempt_id,
        query_binding,
    )
    if durable_binding != response_binding:
        raise ProviderOriginError(
            "provider-origin response binding changed after durable revalidation"
        )
    response_binding = durable_binding
    expected = _query_snapshot(query_binding)
    current_route = _current_route_snapshot(query_binding)
    required_entitlement = _exact_text(
        required_data_entitlement,
        name="required_data_entitlement",
    )
    if (
        response_binding.route_digest != current_route["route_digest"]
        or response_binding.data_entitlement != current_route["data_entitlement"]
    ):
        raise ProviderOriginError(
            "durable authenticated-read route no longer matches canonical route"
        )
    if response_binding.data_entitlement != required_entitlement:
        raise ProviderOriginError(
            "durable authenticated-read route entitlement does not match consumer"
        )
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
    if response_binding.http_status not in current_route["success_statuses"]:
        raise ProviderOriginError(
            "provider response status is not accepted by durable authenticated-read route"
        )

    selected = _selected_authority_from_durable_response(
        response_binding,
        query_binding,
    )
    try:
        revalidate_selected_provider_authority(
            selected,
            qualification_reader=qualification_reader,
            capability_registry=capability_registry,
            at=datetime.now(timezone.utc),
        )
    except (ProviderSelectionError, TypeError, ValueError) as error:
        raise ProviderOriginError(
            "selected provider Q+C authority is no longer current at financial promotion"
        ) from error

    _require_reconciliation_generation_scope(
        journal._require_store(),
        (
            None
            if response_binding.reconciliation_generation is None
            else dict(response_binding.reconciliation_generation)
        ),
        query_binding,
        _selection_record(selected, query_binding),
        require_current=True,
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
