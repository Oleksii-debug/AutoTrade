"""Commit Host-signed authenticated-read durability into the canonical journal.

The C# Host owns the non-self-mintable process key and signed wire attempt/receipt.
This module owns only the Python JournalStore side of the durability handshake.
It creates no second ledger, network stack, endpoint registry, or provider parser.

Public entry points always execute the captured CNG-backed Host verifiers. Internal
implementation seams accept injected verified results only for deterministic tests.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from .persistence import JournalStore
from .provider_core import AuthenticatedReadQueryBinding
from .provider_host_attestation import (
    HostAuthenticatedReadReceipt,
    HostProviderAttestationError,
    HostPreparedDurabilityReceipt,
    HostObservedDurabilityReceipt,
    VerifiedHostPreparedAttestation,
    _OBSERVED_DURABILITY_SCHEMA,
    _PREPARED_DURABILITY_SCHEMA,
    _content_identity,
    _host_utc_key,
    _parse_receipt,
    canonical_host_material,
)
from .provider_origin import (
    ProviderOriginError,
    ProviderOriginJournal,
    _OBSERVED_EVENT,
    _OBSERVED_PAYLOAD_KEYS,
    _PENDING_KIND,
    _PREPARED_EVENT,
    _PREPARED_PAYLOAD_KEYS,
    _PROVIDER_ORIGIN_KIND,
    _append_origin_event,
    _load_origin_events,
    _query_snapshot,
    _require_origin_event,
    _require_snapshot,
)
from .provider_origin_host_bridge import (
    HostProviderOriginPins,
    ProviderOriginHostBridgeError,
    canonical_provider_origin_journal_identity,
    verify_bybit_host_prepared_against_binding,
)
from .persistence import require_exact_journal_store_authority


class ProviderOriginHostDurabilityError(RuntimeError):
    """Host-signed durability cannot be committed into canonical provider state."""


def _journal_utc_from_host(value: str) -> tuple[str, str]:
    """Return (Python canonical UTC, Host seven-digit canonical UTC), ceil to us.

    .NET contracts retain 100-ns precision while Python datetime retains
    microseconds. Rounding down could make a durability commit predate the signed
    Host attempt/response by 100..900 ns, so non-microsecond Host instants are
    rounded upward by one microsecond.
    """

    try:
        key = _host_utc_key(value, name="Host durability time")
    except HostProviderAttestationError as error:
        raise ProviderOriginHostDurabilityError(
            "Host durability time is non-canonical"
        ) from error
    year, month, day, hour, minute, second, ticks = key
    microsecond, remainder = divmod(ticks, 10)
    point = datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
        tzinfo=timezone.utc,
    )
    if remainder:
        point += timedelta(microseconds=1)
    python_text = point.isoformat().replace("+00:00", "Z")
    host_text = (
        point.strftime("%Y-%m-%dT%H:%M:%S")
        + f".{point.microsecond:06d}0Z"
    )
    return python_text, host_text


def _prepared_receipt(
    verified: VerifiedHostPreparedAttestation,
    *,
    journal_identity: str,
    event: dict[str, object],
    committed_at_host: str,
    _content_id=_content_identity,
    _material=canonical_host_material,
) -> HostPreparedDurabilityReceipt:
    attempt = verified.attempt
    event_id = event.get("event_id")
    sequence = event.get("journal_sequence")
    if (
        event_id != attempt.read_attempt_id + ":prepared"
        or type(sequence) is not int
        or sequence <= 0
    ):
        raise ProviderOriginHostDurabilityError(
            "canonical Prepared event identity/sequence is invalid"
        )
    receipt_identity = _content_id(
        "provider-read-durable-prepared",
        _material(
            _PREPARED_DURABILITY_SCHEMA,
            verified.issuer_session.session_identity,
            attempt.read_attempt_id,
            attempt.binding_sha256,
            attempt.subject.query_digest,
            journal_identity,
            event_id,
            str(sequence),
            committed_at_host,
        ),
    )
    return HostPreparedDurabilityReceipt(
        issuer_session_identity=verified.issuer_session.session_identity,
        read_attempt_id=attempt.read_attempt_id,
        read_attempt_binding_sha256=attempt.binding_sha256,
        query_digest=attempt.subject.query_digest,
        journal_identity=journal_identity,
        prepared_event_id=event_id,
        journal_sequence=sequence,
        committed_at_utc=committed_at_host,
        receipt_identity=receipt_identity,
    )


def _require_existing_prepared(
    event: dict[str, object],
    *,
    verified: VerifiedHostPreparedAttestation,
    query_binding: AuthenticatedReadQueryBinding,
    pins: HostProviderOriginPins,
    _check=_require_origin_event,
    _snapshot_check=_require_snapshot,
) -> dict[str, object]:
    attempt = verified.attempt
    try:
        payload = _check(
            event,
            attempt_id=attempt.read_attempt_id,
            event_type=_PREPARED_EVENT,
            aggregate_version=1,
            payload_keys=_PREPARED_PAYLOAD_KEYS,
        )
        _snapshot_check(payload, query_binding)
    except ProviderOriginError as error:
        raise ProviderOriginHostDurabilityError(
            "existing provider-origin Prepared row is invalid"
        ) from error
    if (
        payload.get("origin_kind") != _PENDING_KIND
        or payload.get("transport_identity") != pins.transport_identity
        or payload.get("network_policy_identity") != pins.network_policy_identity
    ):
        raise ProviderOriginHostDurabilityError(
            "existing provider-origin Prepared row conflicts with Host authority"
        )
    return payload


def _commit_host_prepared_impl(
    envelope: object,
    *,
    query_binding: AuthenticatedReadQueryBinding,
    store: JournalStore,
    pins: HostProviderOriginPins,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    verify_prepared,
    _journal_identity=canonical_provider_origin_journal_identity,
    _require_store=require_exact_journal_store_authority,
    _load=_load_origin_events,
    _append=_append_origin_event,
    _snapshot=_query_snapshot,
    _event_builder=ProviderOriginJournal._event,
) -> HostPreparedDurabilityReceipt:
    try:
        verified = verify_prepared(
            envelope,
            query_binding=query_binding,
            pins=pins,
            expected_session_identity=expected_session_identity,
            expected_public_key_sha256=expected_public_key_sha256,
        )
    except ProviderOriginHostBridgeError as error:
        raise ProviderOriginHostDurabilityError(
            "Host Prepared authority is not verified"
        ) from error
    if type(verified) is not VerifiedHostPreparedAttestation:
        raise ProviderOriginHostDurabilityError(
            "Host Prepared verifier returned non-canonical result"
        )
    if type(pins) is not HostProviderOriginPins:
        raise TypeError("pins must be exact HostProviderOriginPins")

    attempt = verified.attempt
    snapshot = _snapshot(query_binding)
    if attempt.subject.query_digest != snapshot["query_digest"]:
        raise ProviderOriginHostDurabilityError(
            "Host Prepared query digest changed after verification"
        )
    journal_identity = _journal_identity(store)
    try:
        identity = _require_store(
            store,
            subject="Host provider-origin durability JournalStore",
        )
        events = _load(store, identity, attempt.read_attempt_id)
    except (ProviderOriginError, TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginHostDurabilityError(
            "canonical provider-origin journal is unavailable"
        ) from error

    committed_at, committed_at_host = _journal_utc_from_host(
        attempt.prepared_at_utc
    )
    if not events:
        event = _event_builder(
            event_id=attempt.read_attempt_id + ":prepared",
            event_type=_PREPARED_EVENT,
            attempt_id=attempt.read_attempt_id,
            aggregate_version=1,
            payload={
                "origin_kind": _PENDING_KIND,
                "query": snapshot,
                "transport_identity": pins.transport_identity,
                "network_policy_identity": pins.network_policy_identity,
            },
            committed_at=committed_at,
        )
        try:
            _append(store, identity, event)
            events = _load(store, identity, attempt.read_attempt_id)
        except (ProviderOriginError, TypeError, RuntimeError, ValueError) as error:
            raise ProviderOriginHostDurabilityError(
                "Host Prepared durability commit failed"
            ) from error

    if len(events) != 1:
        raise ProviderOriginHostDurabilityError(
            "Host Prepared durability requires exactly one Prepared row"
        )
    _require_existing_prepared(
        events[0],
        verified=verified,
        query_binding=query_binding,
        pins=pins,
    )
    event_time = events[0].get("committed_at")
    if type(event_time) is not str:
        raise ProviderOriginHostDurabilityError(
            "canonical Prepared committed_at is unavailable"
        )
    actual_python, actual_host = _journal_utc_from_host(
        committed_at_host
    )
    if event_time != actual_python:
        # Idempotent replay is allowed only for the same signed Host attempt.
        raise ProviderOriginHostDurabilityError(
            "existing provider-origin Prepared time conflicts with signed Host attempt"
        )
    return _prepared_receipt(
        verified,
        journal_identity=journal_identity,
        event=events[0],
        committed_at_host=actual_host,
    )


def _install_prepared_commit(verifier, implementation=_commit_host_prepared_impl):
    def commit_bybit_host_prepared_to_canonical_journal(
        envelope: object,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        store: JournalStore,
        pins: HostProviderOriginPins,
        expected_session_identity: str,
        expected_public_key_sha256: str,
    ) -> HostPreparedDurabilityReceipt:
        return implementation(
            envelope,
            query_binding=query_binding,
            store=store,
            pins=pins,
            expected_session_identity=expected_session_identity,
            expected_public_key_sha256=expected_public_key_sha256,
            verify_prepared=verifier,
        )

    return commit_bybit_host_prepared_to_canonical_journal


commit_bybit_host_prepared_to_canonical_journal = _install_prepared_commit(
    verify_bybit_host_prepared_against_binding
)
del _install_prepared_commit


def _observed_receipt(
    verified: VerifiedHostPreparedAttestation,
    provider_receipt: HostAuthenticatedReadReceipt,
    prepared_receipt: HostPreparedDurabilityReceipt,
    *,
    event: dict[str, object],
    committed_at_host: str,
    _content_id=_content_identity,
    _material=canonical_host_material,
) -> HostObservedDurabilityReceipt:
    event_id = event.get("event_id")
    sequence = event.get("journal_sequence")
    if (
        event_id != verified.attempt.read_attempt_id + ":observed"
        or type(sequence) is not int
        or sequence <= prepared_receipt.journal_sequence
    ):
        raise ProviderOriginHostDurabilityError(
            "canonical Observed event identity/sequence is invalid"
        )
    receipt_identity = _content_id(
        "provider-read-durable-observed",
        _material(
            _OBSERVED_DURABILITY_SCHEMA,
            verified.issuer_session.session_identity,
            verified.attempt.read_attempt_id,
            verified.attempt.binding_sha256,
            provider_receipt.receipt_sha256,
            provider_receipt.response_sha256,
            str(provider_receipt.http_status),
            provider_receipt.observed_at_utc,
            prepared_receipt.journal_identity,
            prepared_receipt.receipt_identity,
            prepared_receipt.prepared_event_id,
            str(prepared_receipt.journal_sequence),
            event_id,
            str(sequence),
            committed_at_host,
        ),
    )
    return HostObservedDurabilityReceipt(
        issuer_session_identity=verified.issuer_session.session_identity,
        read_attempt_id=verified.attempt.read_attempt_id,
        read_attempt_binding_sha256=verified.attempt.binding_sha256,
        provider_receipt_sha256=provider_receipt.receipt_sha256,
        response_sha256=provider_receipt.response_sha256,
        http_status=provider_receipt.http_status,
        observed_at_utc=provider_receipt.observed_at_utc,
        journal_identity=prepared_receipt.journal_identity,
        prepared_receipt_identity=prepared_receipt.receipt_identity,
        prepared_event_id=prepared_receipt.prepared_event_id,
        prepared_journal_sequence=prepared_receipt.journal_sequence,
        observed_event_id=event_id,
        observed_journal_sequence=sequence,
        committed_at_utc=committed_at_host,
        receipt_identity=receipt_identity,
    )


def _commit_host_observed_impl(
    prepared_envelope: object,
    receipt_value: object,
    response_bytes: bytes,
    *,
    query_binding: AuthenticatedReadQueryBinding,
    store: JournalStore,
    pins: HostProviderOriginPins,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    verify_prepared,
    verify_receipt,
    _prepared_commit=_commit_host_prepared_impl,
    _require_store=require_exact_journal_store_authority,
    _load=_load_origin_events,
    _append=_append_origin_event,
    _event_builder=ProviderOriginJournal._event,
) -> HostObservedDurabilityReceipt:
    if type(response_bytes) is not bytes or not response_bytes:
        raise ProviderOriginHostDurabilityError(
            "Host Observed durability requires exact non-empty response bytes"
        )
    prepared_receipt = _prepared_commit(
        prepared_envelope,
        query_binding=query_binding,
        store=store,
        pins=pins,
        expected_session_identity=expected_session_identity,
        expected_public_key_sha256=expected_public_key_sha256,
        verify_prepared=verify_prepared,
    )
    try:
        verified = verify_prepared(
            prepared_envelope,
            query_binding=query_binding,
            pins=pins,
            expected_session_identity=expected_session_identity,
            expected_public_key_sha256=expected_public_key_sha256,
        )
        provider_receipt = verify_receipt(
            receipt_value,
            prepared=verified,
            response_bytes=response_bytes,
        )
    except (ProviderOriginHostBridgeError, HostProviderAttestationError) as error:
        raise ProviderOriginHostDurabilityError(
            "Host response receipt is not cryptographically verified"
        ) from error
    if type(provider_receipt) is not HostAuthenticatedReadReceipt:
        raise ProviderOriginHostDurabilityError(
            "Host response verifier returned non-canonical receipt"
        )

    try:
        identity = _require_store(
            store,
            subject="Host provider-origin durability JournalStore",
        )
        events = _load(store, identity, verified.attempt.read_attempt_id)
    except (ProviderOriginError, TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginHostDurabilityError(
            "canonical provider-origin journal is unavailable"
        ) from error
    if len(events) not in {1, 2}:
        raise ProviderOriginHostDurabilityError(
            "Host Observed durability requires Prepared with at most one Observed row"
        )
    prepared_event = events[0]
    _require_existing_prepared(
        prepared_event,
        verified=verified,
        query_binding=query_binding,
        pins=pins,
    )
    observed_python, observed_host = _journal_utc_from_host(
        provider_receipt.observed_at_utc
    )
    response_sha = "sha256:" + sha256(response_bytes).hexdigest()
    if response_sha != provider_receipt.response_sha256:
        raise ProviderOriginHostDurabilityError(
            "Host response receipt digest changed after verification"
        )

    if len(events) == 1:
        observed = _event_builder(
            event_id=verified.attempt.read_attempt_id + ":observed",
            event_type=_OBSERVED_EVENT,
            attempt_id=verified.attempt.read_attempt_id,
            aggregate_version=2,
            payload={
                "origin_kind": _PROVIDER_ORIGIN_KIND,
                "prepared_event_id": prepared_event["event_id"],
                "query_digest": verified.attempt.subject.query_digest,
                "transport_identity": pins.transport_identity,
                "network_policy_identity": pins.network_policy_identity,
                "http_status": provider_receipt.http_status,
                "response_sha256": response_sha,
                "response_base64": base64.b64encode(response_bytes).decode("ascii"),
                "observed_at": observed_python,
            },
            committed_at=observed_python,
        )
        try:
            _append(store, identity, observed)
            events = _load(store, identity, verified.attempt.read_attempt_id)
        except (ProviderOriginError, TypeError, RuntimeError, ValueError) as error:
            raise ProviderOriginHostDurabilityError(
                "Host Observed durability commit failed"
            ) from error

    if len(events) != 2:
        raise ProviderOriginHostDurabilityError(
            "Host Observed durability did not produce exact Prepared/Observed chronology"
        )
    observed_event = events[1]
    try:
        payload = _require_origin_event(
            observed_event,
            attempt_id=verified.attempt.read_attempt_id,
            event_type=_OBSERVED_EVENT,
            aggregate_version=2,
            payload_keys=_OBSERVED_PAYLOAD_KEYS,
        )
    except ProviderOriginError as error:
        raise ProviderOriginHostDurabilityError(
            "existing provider-origin Observed row is invalid"
        ) from error
    expected_payload = {
        "origin_kind": _PROVIDER_ORIGIN_KIND,
        "prepared_event_id": prepared_event["event_id"],
        "query_digest": verified.attempt.subject.query_digest,
        "transport_identity": pins.transport_identity,
        "network_policy_identity": pins.network_policy_identity,
        "http_status": provider_receipt.http_status,
        "response_sha256": response_sha,
        "response_base64": base64.b64encode(response_bytes).decode("ascii"),
        "observed_at": observed_python,
    }
    if payload != expected_payload or observed_event.get("committed_at") != observed_python:
        raise ProviderOriginHostDurabilityError(
            "existing provider-origin Observed row conflicts with signed Host response"
        )
    return _observed_receipt(
        verified,
        provider_receipt,
        prepared_receipt,
        event=observed_event,
        committed_at_host=observed_host,
    )


def _install_observed_commit(
    prepared_verifier,
    receipt_verifier,
    implementation=_commit_host_observed_impl,
):
    def commit_bybit_host_observed_to_canonical_journal(
        prepared_envelope: object,
        receipt_value: object,
        response_bytes: bytes,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        store: JournalStore,
        pins: HostProviderOriginPins,
        expected_session_identity: str,
        expected_public_key_sha256: str,
    ) -> HostObservedDurabilityReceipt:
        return implementation(
            prepared_envelope,
            receipt_value,
            response_bytes,
            query_binding=query_binding,
            store=store,
            pins=pins,
            expected_session_identity=expected_session_identity,
            expected_public_key_sha256=expected_public_key_sha256,
            verify_prepared=prepared_verifier,
            verify_receipt=receipt_verifier,
        )

    return commit_bybit_host_observed_to_canonical_journal


commit_bybit_host_observed_to_canonical_journal = _install_observed_commit(
    verify_bybit_host_prepared_against_binding,
    _parse_receipt,
)
del _install_observed_commit
