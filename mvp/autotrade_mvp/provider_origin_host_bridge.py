"""Cross-bind Host-signed authenticated reads to the canonical provider journal.

This bridge adds no second ledger, endpoint registry, credential authority or
network stack. It consumes the current provider-core binding, Bybit endpoint
rule, Host CNG attestation verifier and exact JournalStore generation. A Host
durability receipt is accepted only when the referenced Prepared/Observed rows
actually exist in that exact journal and bind the same response bytes.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .persistence import (
    JournalStore,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_core import (
    AuthenticatedReadQueryBinding,
    _require_authenticated_read_query_binding_authority,
)
from .provider_host_attestation import (
    HostProviderAttestationError,
    VerifiedHostObservedAttestation,
    VerifiedHostPreparedAttestation,
    _host_utc_key,
    verify_host_observed_attestation,
    verify_host_prepared_attestation,
)
from .provider_origin import (
    ProviderOriginError,
    _OBSERVED_EVENT,
    _OBSERVED_PAYLOAD_KEYS,
    _PENDING_KIND,
    _PREPARED_EVENT,
    _PREPARED_PAYLOAD_KEYS,
    _PROVIDER_ORIGIN_KIND,
    _load_origin_events,
    _query_snapshot,
    _require_origin_event,
    _require_snapshot,
)
from .provider_transport import (
    AuthenticatedReadEndpointRule,
    ProviderTransportScopeError,
    _bybit_authenticated_read_rule,
)
from .store_identity import (
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)


class ProviderOriginHostBridgeError(RuntimeError):
    """Host attestation cannot be composed with canonical provider authority."""


@dataclass(frozen=True, slots=True)
class HostProviderOriginPins:
    """Independent product-selected authorities absent from the query binding."""

    credential_handle_id: str
    credential_generation: int
    qualification_id: str
    qualification_build_id: str
    adapter_build_identity: str
    network_policy_identity: str
    transport_identity: str

    def __post_init__(self) -> None:
        for name in (
            "credential_handle_id",
            "qualification_id",
            "qualification_build_id",
            "adapter_build_identity",
            "network_policy_identity",
            "transport_identity",
        ):
            value = object.__getattribute__(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise TypeError(f"{name} must be canonical non-empty exact text")
        generation = object.__getattribute__(self, "credential_generation")
        if type(generation) is not int or generation <= 0:
            raise TypeError("credential_generation must be an exact positive integer")


_JOURNAL_IDENTITY_SCHEMA = "autotrade-provider-origin-journal-identity:v1"
_ENDPOINT_RULE_SCHEMA = "autotrade-authenticated-read-endpoint-rule:v1"


def _journal_identity_material(identity: JournalStoreIdentity) -> dict[str, object]:
    state = vars(
        require_exact_journal_store_identity(
            identity,
            subject="provider-origin Host bridge journal identity",
        )
    )
    source = state["identity_source"]
    if source == "windows_by_handle":
        return {
            "schema": _JOURNAL_IDENTITY_SCHEMA,
            "identity_source": source,
            "windows_volume_serial": state["windows_volume_serial"],
            "windows_file_index_high": state["windows_file_index_high"],
            "windows_file_index_low": state["windows_file_index_low"],
        }
    if source == "posix_stat":
        return {
            "schema": _JOURNAL_IDENTITY_SCHEMA,
            "identity_source": source,
            "canonical_path": state["canonical_path"],
            "filesystem_device": state["filesystem_device"],
            "filesystem_inode": state["filesystem_inode"],
        }
    raise ProviderOriginHostBridgeError(
        "provider-origin journal identity source is unsupported"
    )


def canonical_provider_origin_journal_identity(
    store: JournalStore,
    _require_store=require_exact_journal_store_authority,
    _material=_journal_identity_material,
    _digest=payload_digest,
) -> str:
    """Content-address the exact selected physical journal generation."""

    try:
        identity = _require_store(
            store,
            subject="provider-origin Host bridge JournalStore",
        )
        digest = _digest(_material(identity))
    except (TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginHostBridgeError(
            "canonical provider-origin JournalStore authority is unavailable"
        ) from error
    if type(digest) is not str or not digest.startswith("sha256:") or len(digest) != 71:
        raise ProviderOriginHostBridgeError(
            "canonical provider-origin journal identity is invalid"
        )
    return digest


def canonical_bybit_authenticated_read_rule_identity(
    query_binding: AuthenticatedReadQueryBinding,
    _rule_resolver=_bybit_authenticated_read_rule,
    _rule_type=AuthenticatedReadEndpointRule,
    _digest=payload_digest,
) -> tuple[str, AuthenticatedReadEndpointRule]:
    """Derive Host rule identity from the existing canonical Bybit registry."""

    try:
        rule = _rule_resolver(query_binding)
    except (TypeError, ProviderTransportScopeError) as error:
        raise ProviderOriginHostBridgeError(
            "Bybit authenticated-read endpoint rule authority is unavailable"
        ) from error
    if type(rule) is not _rule_type:
        raise ProviderOriginHostBridgeError(
            "Bybit authenticated-read endpoint rule authority is non-canonical"
        )
    state = vars(rule)
    if (
        type(state) is not dict
        or type(state.get("permission_scope")) is not str
        or type(state.get("data_entitlement")) is not str
        or type(state.get("success_statuses")) is not frozenset
        or any(type(status) is not int for status in state["success_statuses"])
    ):
        raise ProviderOriginHostBridgeError(
            "Bybit authenticated-read endpoint rule state is non-canonical"
        )
    material = {
        "schema": _ENDPOINT_RULE_SCHEMA,
        "provider_id": "BYBIT",
        "endpoint": query_binding.endpoint,
        "surface": rule.surface.value,
        "permission_scope": rule.permission_scope,
        "data_entitlement": rule.data_entitlement,
        "success_statuses": sorted(rule.success_statuses),
    }
    return _digest(material), rule


def _exact_query_snapshot(
    query_binding: AuthenticatedReadQueryBinding,
    _binding_type=AuthenticatedReadQueryBinding,
    _require_query=_require_authenticated_read_query_binding_authority,
    _snapshot=_query_snapshot,
) -> dict[str, object]:
    if type(query_binding) is not _binding_type:
        raise TypeError("query_binding must be exact AuthenticatedReadQueryBinding")
    try:
        _require_query(query_binding)
        return _snapshot(query_binding)
    except Exception as error:
        raise ProviderOriginHostBridgeError(
            "canonical authenticated-read query authority is unavailable"
        ) from error


def _require_pin_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderOriginHostBridgeError(
            f"{name} must be canonical non-empty exact text"
        )
    return value


def _require_host_subject_matches(
    verified: VerifiedHostPreparedAttestation,
    *,
    query_binding: AuthenticatedReadQueryBinding,
    pins: HostProviderOriginPins,
    _verified_type=VerifiedHostPreparedAttestation,
    _pins_type=HostProviderOriginPins,
    _snapshot=_exact_query_snapshot,
    _rule_identity=canonical_bybit_authenticated_read_rule_identity,
) -> tuple[dict[str, object], AuthenticatedReadEndpointRule]:
    if type(verified) is not _verified_type:
        raise ProviderOriginHostBridgeError(
            "Host Prepared verification result is non-canonical"
        )
    if type(pins) is not _pins_type:
        raise TypeError("pins must be exact HostProviderOriginPins")
    snapshot = _snapshot(query_binding)
    rule_identity, rule = _rule_identity(query_binding)
    subject = verified.attempt.subject
    expected = {
        "provider_id": snapshot["provider_id"],
        "account_id": snapshot["account_id"],
        "entity_id": snapshot["entity_id"],
        "runtime_environment": snapshot["environment"],
        "provider_environment": snapshot["provider_environment"],
        "endpoint": snapshot["endpoint"],
        "surface": snapshot["surface"],
        "permission_scope": snapshot["permission_scope"],
        "data_entitlement": rule.data_entitlement,
        "instrument_version": snapshot["instrument_version"],
        "query_digest": snapshot["query_digest"],
        "endpoint_rule_identity": rule_identity,
        "credential_handle_id": pins.credential_handle_id,
        "credential_generation": pins.credential_generation,
        "capability_id": snapshot["capability_snapshot_id"],
        "qualification_id": pins.qualification_id,
        "qualification_build_id": pins.qualification_build_id,
        "adapter_build_identity": pins.adapter_build_identity,
        "network_policy_identity": pins.network_policy_identity,
        "transport_identity": pins.transport_identity,
    }
    actual = {
        "provider_id": subject.provider_id,
        "account_id": subject.account_id,
        "entity_id": subject.entity_id,
        "runtime_environment": subject.runtime_environment,
        "provider_environment": subject.provider_environment,
        "endpoint": subject.endpoint,
        "surface": subject.surface,
        "permission_scope": subject.permission_scope,
        "data_entitlement": subject.data_entitlement,
        "instrument_version": subject.instrument_version,
        "query_digest": subject.query_digest,
        "endpoint_rule_identity": subject.endpoint_rule_identity,
        "credential_handle_id": subject.credential_handle_id,
        "credential_generation": subject.credential_generation,
        "capability_id": subject.capability_id,
        "qualification_id": subject.qualification_id,
        "qualification_build_id": subject.qualification_build_id,
        "adapter_build_identity": subject.adapter_build_identity,
        "network_policy_identity": subject.network_policy_identity,
        "transport_identity": subject.transport_identity,
    }
    for key, expected_value in expected.items():
        if actual[key] != expected_value:
            raise ProviderOriginHostBridgeError(
                f"Host authenticated-read subject mismatch: {key}"
            )
    if dict(verified.query) != snapshot["query"]:
        raise ProviderOriginHostBridgeError(
            "Host authenticated-read query differs from canonical binding"
        )
    return snapshot, rule


def _verify_bybit_host_prepared_against_binding_impl(
    envelope: object,
    *,
    query_binding: AuthenticatedReadQueryBinding,
    pins: HostProviderOriginPins,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    verifier,
    _snapshot=_exact_query_snapshot,
    _pin_text=_require_pin_text,
    _subject_check=_require_host_subject_matches,
) -> VerifiedHostPreparedAttestation:
    snapshot = _snapshot(query_binding)
    try:
        verified = verifier(
            envelope,
            expected_session_identity=_pin_text(
                expected_session_identity,
                name="expected_session_identity",
            ),
            expected_public_key_sha256=_pin_text(
                expected_public_key_sha256,
                name="expected_public_key_sha256",
            ),
            expected_query=dict(snapshot["query"]),
        )
    except HostProviderAttestationError as error:
        raise ProviderOriginHostBridgeError(
            "Host Prepared attestation is not independently verified"
        ) from error
    _subject_check(
        verified,
        query_binding=query_binding,
        pins=pins,
    )
    return verified


def _install_prepared_bridge(
    verifier,
    implementation=_verify_bybit_host_prepared_against_binding_impl,
):
    def verify_bybit_host_prepared_against_binding(
        envelope: object,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        pins: HostProviderOriginPins,
        expected_session_identity: str,
        expected_public_key_sha256: str,
    ) -> VerifiedHostPreparedAttestation:
        return implementation(
            envelope,
            query_binding=query_binding,
            pins=pins,
            expected_session_identity=expected_session_identity,
            expected_public_key_sha256=expected_public_key_sha256,
            verifier=verifier,
        )

    return verify_bybit_host_prepared_against_binding


verify_bybit_host_prepared_against_binding = _install_prepared_bridge(
    verify_host_prepared_attestation
)
del _install_prepared_bridge


def _host_key_from_journal_utc(value: object) -> tuple[int, ...]:
    if type(value) is not str or not value.endswith("Z"):
        raise ProviderOriginHostBridgeError(
            "provider-origin journal timestamp is non-canonical"
        )
    from datetime import datetime

    try:
        point = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ProviderOriginHostBridgeError(
            "provider-origin journal timestamp is non-canonical"
        ) from error
    if point.utcoffset() is None:
        raise ProviderOriginHostBridgeError(
            "provider-origin journal timestamp lacks timezone"
        )
    point = point.astimezone(__import__("datetime").timezone.utc)
    return (
        point.year,
        point.month,
        point.day,
        point.hour,
        point.minute,
        point.second,
        point.microsecond * 10,
    )


def _require_durable_events_match_verified_host(
    verified: VerifiedHostObservedAttestation,
    *,
    query_binding: AuthenticatedReadQueryBinding,
    store: JournalStore,
    _verified_type=VerifiedHostObservedAttestation,
    _require_store=require_exact_journal_store_authority,
    _load=_load_origin_events,
    _event_check=_require_origin_event,
    _snapshot_check=_require_snapshot,
    _journal_time_key=_host_key_from_journal_utc,
    _host_time_key=_host_utc_key,
) -> None:
    if type(verified) is not _verified_type:
        raise ProviderOriginHostBridgeError(
            "Host Observed verification result is non-canonical"
        )
    attempt = verified.prepared.attempt
    prepared_receipt = verified.prepared_durability
    observed_receipt = verified.observed_durability
    try:
        identity = _require_store(
            store,
            subject="provider-origin Host bridge JournalStore",
        )
        events = _load(store, identity, attempt.read_attempt_id)
    except (ProviderOriginError, TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginHostBridgeError(
            "canonical provider-origin journal cannot be read"
        ) from error
    if len(events) != 2:
        raise ProviderOriginHostBridgeError(
            "Host durability receipts do not reference exact Prepared/Observed journal rows"
        )
    prepared, observed = events
    try:
        prepared_payload = _event_check(
            prepared,
            attempt_id=attempt.read_attempt_id,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        observed_payload = _event_check(
            observed,
            attempt_id=attempt.read_attempt_id,
            event_type=_OBSERVED_EVENT,
            aggregate_version=2,
            payload_keys=_OBSERVED_PAYLOAD_KEYS,
        )
        _snapshot_check(prepared_payload, query_binding)
    except ProviderOriginError as error:
        raise ProviderOriginHostBridgeError(
            "Host durability receipts reference invalid provider-origin rows"
        ) from error

    if (
        prepared_payload.get("origin_kind") != _PENDING_KIND
        or observed_payload.get("origin_kind") != _PROVIDER_ORIGIN_KIND
        or prepared.get("event_id") != prepared_receipt.prepared_event_id
        or prepared.get("journal_sequence") != prepared_receipt.journal_sequence
        or observed.get("event_id") != observed_receipt.observed_event_id
        or observed.get("journal_sequence") != observed_receipt.observed_journal_sequence
        or observed_receipt.prepared_event_id != prepared.get("event_id")
        or observed_receipt.prepared_journal_sequence != prepared.get("journal_sequence")
    ):
        raise ProviderOriginHostBridgeError(
            "Host durability receipts do not match canonical journal chronology"
        )

    if (
        _journal_time_key(prepared.get("committed_at"))
        != _host_time_key(
            prepared_receipt.committed_at_utc,
            name="durable Prepared committed_at_utc",
        )
        or _journal_time_key(observed.get("committed_at"))
        != _host_time_key(
            observed_receipt.committed_at_utc,
            name="durable Observed committed_at_utc",
        )
    ):
        raise ProviderOriginHostBridgeError(
            "Host durability receipt timestamps differ from canonical journal rows"
        )

    subject = verified.prepared.attempt.subject
    if (
        prepared_payload.get("transport_identity") != subject.transport_identity
        or prepared_payload.get("network_policy_identity")
        != subject.network_policy_identity
        or observed_payload.get("transport_identity") != subject.transport_identity
        or observed_payload.get("network_policy_identity")
        != subject.network_policy_identity
        or observed_payload.get("prepared_event_id") != prepared.get("event_id")
        or observed_payload.get("query_digest") != subject.query_digest
        or observed_payload.get("http_status") != verified.receipt.http_status
        or observed_payload.get("response_sha256") != verified.receipt.response_sha256
        or _journal_time_key(observed_payload.get("observed_at"))
        != _host_time_key(
            verified.receipt.observed_at_utc,
            name="provider observed_at_utc",
        )
    ):
        raise ProviderOriginHostBridgeError(
            "Host response receipt differs from canonical provider-origin journal payload"
        )

    encoded = observed_payload.get("response_base64")
    if type(encoded) is not str or not encoded:
        raise ProviderOriginHostBridgeError(
            "canonical provider-origin journal lacks exact response bytes"
        )
    try:
        durable_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ProviderOriginHostBridgeError(
            "canonical provider-origin response bytes are invalid"
        ) from error
    if durable_bytes != verified.response_bytes:
        raise ProviderOriginHostBridgeError(
            "Host signed response bytes differ from canonical durable response bytes"
        )


def _verify_bybit_host_observed_against_journal_impl(
    envelope: object,
    *,
    query_binding: AuthenticatedReadQueryBinding,
    store: JournalStore,
    pins: HostProviderOriginPins,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    verify_observed,
    _snapshot=_exact_query_snapshot,
    _journal_identity=canonical_provider_origin_journal_identity,
    _pin_text=_require_pin_text,
    _subject_check=_require_host_subject_matches,
    _durable_check=_require_durable_events_match_verified_host,
) -> VerifiedHostObservedAttestation:
    snapshot = _snapshot(query_binding)
    journal_identity = _journal_identity(store)
    try:
        verified = verify_observed(
            envelope,
            expected_session_identity=_pin_text(
                expected_session_identity,
                name="expected_session_identity",
            ),
            expected_public_key_sha256=_pin_text(
                expected_public_key_sha256,
                name="expected_public_key_sha256",
            ),
            expected_query=dict(snapshot["query"]),
            expected_journal_identity=journal_identity,
        )
    except HostProviderAttestationError as error:
        raise ProviderOriginHostBridgeError(
            "Host Observed attestation is not independently verified"
        ) from error
    _subject_check(
        verified.prepared,
        query_binding=query_binding,
        pins=pins,
    )
    _durable_check(
        verified,
        query_binding=query_binding,
        store=store,
    )
    return verified


def _install_observed_bridge(
    verifier,
    implementation=_verify_bybit_host_observed_against_journal_impl,
):
    def verify_bybit_host_observed_against_canonical_journal(
        envelope: object,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        store: JournalStore,
        pins: HostProviderOriginPins,
        expected_session_identity: str,
        expected_public_key_sha256: str,
    ) -> VerifiedHostObservedAttestation:
        return implementation(
            envelope,
            query_binding=query_binding,
            store=store,
            pins=pins,
            expected_session_identity=expected_session_identity,
            expected_public_key_sha256=expected_public_key_sha256,
            verify_observed=verifier,
        )

    return verify_bybit_host_observed_against_canonical_journal


verify_bybit_host_observed_against_canonical_journal = _install_observed_bridge(
    verify_host_observed_attestation
)
del _install_observed_bridge
