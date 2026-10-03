"""Verify independently pinned Host provider-wire ECDSA attestations.

The Host process owns the provider-wire signing key.  Python may consume a
signed session/attempt/receipt only when the expected Host session identity and
public-key digest arrive from a separate authenticated process boundary.

The signature implementation deliberately uses Windows CNG instead of a new
Python crypto dependency.  Non-Windows runtimes fail closed: they may parse
and validate deterministic material for qualification tests, but cannot grant
Host provider-wire authority.
"""

from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
from hashlib import sha256
import re
import struct
import sys
from types import MappingProxyType
from typing import Mapping


class HostProviderAttestationError(ValueError):
    """Raised when Host provider attestation material is malformed or invalid."""


class HostProviderAttestationUnavailable(HostProviderAttestationError):
    """Raised when the platform cannot verify the Host cryptographic authority."""


_SESSION_SCHEMA = "autotrade-provider-issuer-session:v1"
_READ_ATTEMPT_SCHEMA = "autotrade-provider-authenticated-read-attempt:v1"
_READ_RECEIPT_SCHEMA = "autotrade-provider-authenticated-read-receipt:v1"
_PREPARED_ENVELOPE_SCHEMA = "autotrade-host-authenticated-read-prepared:v1"
_OBSERVED_ENVELOPE_SCHEMA = "autotrade-host-authenticated-read-observed:v2"
_PREPARED_DURABILITY_SCHEMA = "autotrade-provider-read-durable-prepared:v1"
_OBSERVED_DURABILITY_SCHEMA = "autotrade-provider-read-durable-observed:v1"
_HOST_SENDER_FENCE_SCHEMA = "autotrade-host-sender-fence:v1"
_HOST_SENDER_FENCE_METHOD = "SAME_HOST_EXCLUSIVE_LEASE_HANDOFF"
_HOST_SENDER_FENCE_ENVELOPE_SCHEMA = "autotrade-host-sender-fence-envelope:v1"
_HOST_LIFETIME_FENCE_DOMAIN = "autotrade-host-lifetime-fence-v1"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SESSION_ID_RE = re.compile(r"provider-issuer-session:sha256:[0-9a-f]{64}")
_ATTEMPT_ID_RE = re.compile(r"provider-read:[0-9a-f]{32}")
_LEASE_SCOPE_RE = re.compile(r"hf-[0-9a-f]{64}")
_HOST_UTC_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{7})Z"
)
_FINANCIAL_RUNTIMES = frozenset({"PAPER", "LIVE"})

# SubjectPublicKeyInfo for id-ecPublicKey + prime256v1 must be exactly:
# SEQUENCE { SEQUENCE { OID 1.2.840.10045.2.1, OID 1.2.840.10045.3.1.7 },
#            BIT STRING 00 04 || X(32) || Y(32) }
_P256_SPKI_PREFIX = bytes.fromhex(
    "3059301306072a8648ce3d020106082a8648ce3d03010703420004"
)
_BCRYPT_ECDSA_PUBLIC_P256_MAGIC = 0x31534345

_SESSION_KEYS = frozenset(
    {
        "schema",
        "issuer_instance_id",
        "started_at_utc",
        "public_key_spki_base64",
        "public_key_sha256",
        "session_identity",
    }
)
_SUBJECT_KEYS = frozenset(
    {
        "provider_id",
        "account_id",
        "entity_id",
        "runtime_environment",
        "provider_environment",
        "endpoint",
        "surface",
        "permission_scope",
        "data_entitlement",
        "instrument_version",
        "query_digest",
        "endpoint_rule_identity",
        "credential_handle_id",
        "credential_generation",
        "capability_id",
        "qualification_id",
        "qualification_build_id",
        "adapter_build_identity",
        "network_policy_identity",
        "transport_identity",
    }
)
_ATTEMPT_KEYS = frozenset(
    {
        "schema",
        "issuer_session_identity",
        "subject",
        "read_generation",
        "read_attempt_id",
        "prepared_at_utc",
        "binding_sha256",
        "signature_base64",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "issuer_session_identity",
        "read_attempt_binding_sha256",
        "read_attempt_id",
        "read_generation",
        "http_status",
        "response_sha256",
        "response_length",
        "observed_at_utc",
        "receipt_sha256",
        "signature_base64",
    }
)
_PREPARED_KEYS = frozenset({"schema", "issuer_session", "attempt", "query"})
_OBSERVED_KEYS = frozenset(
    {
        "schema",
        "issuer_session",
        "attempt",
        "receipt",
        "query",
        "response_base64",
        "durable_prepared",
        "durable_observed",
    }
)
_PREPARED_DURABILITY_KEYS = frozenset(
    {
        "schema",
        "issuer_session_identity",
        "read_attempt_id",
        "read_attempt_binding_sha256",
        "query_digest",
        "journal_identity",
        "prepared_event_id",
        "journal_sequence",
        "committed_at_utc",
        "receipt_identity",
    }
)
_OBSERVED_DURABILITY_KEYS = frozenset(
    {
        "schema",
        "issuer_session_identity",
        "read_attempt_id",
        "read_attempt_binding_sha256",
        "provider_receipt_sha256",
        "response_sha256",
        "http_status",
        "observed_at_utc",
        "journal_identity",
        "prepared_receipt_identity",
        "prepared_event_id",
        "prepared_journal_sequence",
        "observed_event_id",
        "observed_journal_sequence",
        "committed_at_utc",
        "receipt_identity",
    }
)
_HOST_SENDER_FENCE_ENVELOPE_KEYS = frozenset(
    {"schema", "issuer_session", "receipt"}
)
_HOST_SENDER_FENCE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "issuer_session_identity",
        "fence_method",
        "lease_scope_id",
        "lease_owner_record_sha256",
        "lease_acquired_at_utc",
        "owner_scope",
        "provider_id",
        "account_id",
        "runtime_environment",
        "provider_environment",
        "credential_handle_id",
        "credential_generation",
        "backup_manifest_sha256",
        "old_owner_id",
        "old_owner_epoch",
        "new_owner_id",
        "new_owner_epoch",
        "fenced_at_utc",
        "receipt_sha256",
        "signature_base64",
    }
)


def _exact_mapping(
    value: object,
    *,
    name: str,
    keys: frozenset[str],
) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise HostProviderAttestationError(f"{name} has non-canonical shape")
    if any(type(key) is not str for key in value):
        raise HostProviderAttestationError(f"{name} keys must be exact str")
    return value


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise HostProviderAttestationError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise HostProviderAttestationError(
            f"{name} contains invalid Unicode"
        ) from error
    return value


def _sha256_text(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    if _SHA256_RE.fullmatch(text) is None:
        raise HostProviderAttestationError(
            f"{name} must be canonical lowercase sha256 text"
        )
    return text


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise HostProviderAttestationError(f"{name} must be a positive integer")
    return value


def _host_utc_key(value: object, *, name: str) -> tuple[int, ...]:
    text = _exact_text(value, name=name)
    match = _HOST_UTC_RE.fullmatch(text)
    if match is None:
        raise HostProviderAttestationError(
            f"{name} must use canonical Host UTC text"
        )
    year_text, month_text, day_text, hour_text, minute_text, second_text, fraction = (
        match.groups()
    )
    year, month, day, hour, minute, second = (
        int(item)
        for item in (
            year_text,
            month_text,
            day_text,
            hour_text,
            minute_text,
            second_text,
        )
    )
    # Validate calendar/time fields with stdlib without losing the seventh
    # 100-nanosecond digit used by .NET's canonical "fffffff" format.
    from datetime import datetime, timezone

    try:
        datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            int(fraction[:6]),
            tzinfo=timezone.utc,
        )
    except ValueError as error:
        raise HostProviderAttestationError(
            f"{name} must be a real UTC instant"
        ) from error
    return (year, month, day, hour, minute, second, int(fraction))


def _canonical_base64(
    value: object,
    *,
    name: str,
    expected_length: int | None = None,
) -> bytes:
    text = _exact_text(value, name=name)
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as error:
        raise HostProviderAttestationError(
            f"{name} must be canonical base64"
        ) from error
    if base64.b64encode(raw).decode("ascii") != text:
        raise HostProviderAttestationError(
            f"{name} must be canonical base64"
        )
    if expected_length is not None and len(raw) != expected_length:
        raise HostProviderAttestationError(
            f"{name} has invalid decoded length"
        )
    return raw


def canonical_host_material(*fields: str) -> bytes:
    """Mirror ProviderIssuerAuthority.CanonicalMaterial exactly."""

    if any(type(field) is not str for field in fields):
        raise TypeError("canonical Host fields must be exact str")
    chunks = [struct.pack(">I", len(fields))]
    for field in fields:
        try:
            encoded = field.encode("utf-8")
        except UnicodeEncodeError as error:
            raise HostProviderAttestationError(
                "canonical Host field contains invalid Unicode"
            ) from error
        if len(encoded) > 0x7FFFFFFF:
            raise HostProviderAttestationError(
                "canonical Host field exceeds signed Int32 length"
            )
        chunks.append(struct.pack(">I", len(encoded)))
        chunks.append(encoded)
    return b"".join(chunks)


def _digest(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _content_identity(prefix: str, value: bytes) -> str:
    return prefix + ":" + _digest(value)


def _p256_point_from_spki(spki: bytes) -> tuple[bytes, bytes]:
    if (
        len(spki) != len(_P256_SPKI_PREFIX) + 64
        or not spki.startswith(_P256_SPKI_PREFIX)
    ):
        raise HostProviderAttestationError(
            "Host public key must be exact P-256 SubjectPublicKeyInfo"
        )
    point = spki[len(_P256_SPKI_PREFIX) :]
    x = point[:32]
    y = point[32:]
    if len(x) != 32 or len(y) != 32 or not any(x) or not any(y):
        raise HostProviderAttestationError(
            "Host P-256 public point is invalid"
        )
    return x, y


def _verify_p256_sha256_p1363(
    *,
    spki: bytes,
    material: bytes,
    signature: bytes,
) -> None:
    """Verify one C# ECDSA P-256 SHA-256 P1363 signature through CNG."""

    if sys.platform != "win32":
        raise HostProviderAttestationUnavailable(
            "Host provider attestation verification requires Windows CNG"
        )
    if len(signature) != 64:
        raise HostProviderAttestationError(
            "Host P-256 signature must be 64-byte P1363 r||s"
        )
    x, y = _p256_point_from_spki(spki)
    blob = struct.pack(
        "<II",
        _BCRYPT_ECDSA_PUBLIC_P256_MAGIC,
        32,
    ) + x + y
    digest = sha256(material).digest()

    try:
        bcrypt = ctypes.WinDLL("bcrypt.dll")
    except (AttributeError, OSError) as error:
        raise HostProviderAttestationUnavailable(
            "Windows CNG bcrypt provider is unavailable"
        ) from error

    handle_type = ctypes.c_void_p
    ntstatus = ctypes.c_long
    ulong = ctypes.c_ulong
    byte_pointer = ctypes.POINTER(ctypes.c_ubyte)

    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(handle_type),
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ulong,
    ]
    bcrypt.BCryptOpenAlgorithmProvider.restype = ntstatus
    bcrypt.BCryptImportKeyPair.argtypes = [
        handle_type,
        handle_type,
        ctypes.c_wchar_p,
        ctypes.POINTER(handle_type),
        byte_pointer,
        ulong,
        ulong,
    ]
    bcrypt.BCryptImportKeyPair.restype = ntstatus
    bcrypt.BCryptVerifySignature.argtypes = [
        handle_type,
        ctypes.c_void_p,
        byte_pointer,
        ulong,
        byte_pointer,
        ulong,
        ulong,
    ]
    bcrypt.BCryptVerifySignature.restype = ntstatus
    bcrypt.BCryptDestroyKey.argtypes = [handle_type]
    bcrypt.BCryptDestroyKey.restype = ntstatus
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [handle_type, ulong]
    bcrypt.BCryptCloseAlgorithmProvider.restype = ntstatus

    algorithm = handle_type()
    key = handle_type()
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    digest_buffer = (ctypes.c_ubyte * len(digest)).from_buffer_copy(digest)
    signature_buffer = (ctypes.c_ubyte * len(signature)).from_buffer_copy(signature)

    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(algorithm),
        "ECDSA_P256",
        None,
        0,
    )
    if status != 0:
        raise HostProviderAttestationUnavailable(
            f"Windows CNG ECDSA_P256 provider open failed: 0x{status & 0xFFFFFFFF:08x}"
        )
    try:
        status = bcrypt.BCryptImportKeyPair(
            algorithm,
            None,
            "ECCPUBLICBLOB",
            ctypes.byref(key),
            blob_buffer,
            len(blob),
            0,
        )
        if status != 0:
            raise HostProviderAttestationError(
                f"Host P-256 public key import failed: 0x{status & 0xFFFFFFFF:08x}"
            )
        try:
            status = bcrypt.BCryptVerifySignature(
                key,
                None,
                digest_buffer,
                len(digest),
                signature_buffer,
                len(signature),
                0,
            )
            if status != 0:
                raise HostProviderAttestationError(
                    "Host provider attestation signature is invalid"
                )
        finally:
            bcrypt.BCryptDestroyKey(key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


@dataclass(frozen=True, slots=True)
class HostIssuerSession:
    issuer_instance_id: str
    started_at_utc: str
    public_key_spki_base64: str
    public_key_sha256: str
    session_identity: str
    public_key_spki: bytes


@dataclass(frozen=True, slots=True)
class HostAuthenticatedReadSubject:
    provider_id: str
    account_id: str
    entity_id: str
    runtime_environment: str
    provider_environment: str
    endpoint: str
    surface: str
    permission_scope: str
    data_entitlement: str
    instrument_version: str
    query_digest: str
    endpoint_rule_identity: str
    credential_handle_id: str
    credential_generation: int
    capability_id: str
    qualification_id: str
    qualification_build_id: str
    adapter_build_identity: str
    network_policy_identity: str
    transport_identity: str


@dataclass(frozen=True, slots=True)
class HostAuthenticatedReadAttempt:
    issuer_session_identity: str
    subject: HostAuthenticatedReadSubject
    read_generation: int
    read_attempt_id: str
    prepared_at_utc: str
    binding_sha256: str
    signature_base64: str


@dataclass(frozen=True, slots=True)
class HostAuthenticatedReadReceipt:
    issuer_session_identity: str
    read_attempt_binding_sha256: str
    read_attempt_id: str
    read_generation: int
    http_status: int
    response_sha256: str
    response_length: int
    observed_at_utc: str
    receipt_sha256: str
    signature_base64: str


@dataclass(frozen=True, slots=True)
class VerifiedHostPreparedAttestation:
    issuer_session: HostIssuerSession
    attempt: HostAuthenticatedReadAttempt
    query: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class HostPreparedDurabilityReceipt:
    issuer_session_identity: str
    read_attempt_id: str
    read_attempt_binding_sha256: str
    query_digest: str
    journal_identity: str
    prepared_event_id: str
    journal_sequence: int
    committed_at_utc: str
    receipt_identity: str


@dataclass(frozen=True, slots=True)
class HostObservedDurabilityReceipt:
    issuer_session_identity: str
    read_attempt_id: str
    read_attempt_binding_sha256: str
    provider_receipt_sha256: str
    response_sha256: str
    http_status: int
    observed_at_utc: str
    journal_identity: str
    prepared_receipt_identity: str
    prepared_event_id: str
    prepared_journal_sequence: int
    observed_event_id: str
    observed_journal_sequence: int
    committed_at_utc: str
    receipt_identity: str


@dataclass(frozen=True, slots=True)
class VerifiedHostObservedAttestation:
    prepared: VerifiedHostPreparedAttestation
    receipt: HostAuthenticatedReadReceipt
    response_bytes: bytes
    prepared_durability: HostPreparedDurabilityReceipt
    observed_durability: HostObservedDurabilityReceipt


@dataclass(frozen=True, slots=True)
class HostSenderFenceReceipt:
    issuer_session_identity: str
    fence_method: str
    lease_scope_id: str
    lease_owner_record_sha256: str
    lease_acquired_at_utc: str
    owner_scope: str
    provider_id: str
    account_id: str
    runtime_environment: str
    provider_environment: str
    credential_handle_id: str
    credential_generation: int
    backup_manifest_sha256: str
    old_owner_id: str
    old_owner_epoch: int
    new_owner_id: str
    new_owner_epoch: int
    fenced_at_utc: str
    receipt_sha256: str
    signature_base64: str


@dataclass(frozen=True, slots=True)
class VerifiedHostSenderFenceAttestation:
    issuer_session: HostIssuerSession
    receipt: HostSenderFenceReceipt


def _parse_session(value: object) -> HostIssuerSession:
    raw = _exact_mapping(value, name="issuer_session", keys=_SESSION_KEYS)
    if raw["schema"] != _SESSION_SCHEMA:
        raise HostProviderAttestationError("Host issuer session schema is invalid")
    issuer_id = _exact_text(
        raw["issuer_instance_id"],
        name="issuer_instance_id",
    )
    started = _exact_text(raw["started_at_utc"], name="started_at_utc")
    _host_utc_key(started, name="started_at_utc")
    spki_text = _exact_text(
        raw["public_key_spki_base64"],
        name="public_key_spki_base64",
    )
    spki = _canonical_base64(
        spki_text,
        name="public_key_spki_base64",
    )
    _p256_point_from_spki(spki)
    key_digest = _sha256_text(
        raw["public_key_sha256"],
        name="public_key_sha256",
    )
    if key_digest != _digest(spki):
        raise HostProviderAttestationError(
            "Host issuer public-key digest conflicts with SPKI bytes"
        )
    session_identity = _exact_text(
        raw["session_identity"],
        name="session_identity",
    )
    if _SESSION_ID_RE.fullmatch(session_identity) is None:
        raise HostProviderAttestationError(
            "Host issuer session identity is non-canonical"
        )
    expected_identity = _content_identity(
        "provider-issuer-session",
        canonical_host_material(
            _SESSION_SCHEMA,
            issuer_id,
            started,
            spki_text,
            key_digest,
        ),
    )
    if session_identity != expected_identity:
        raise HostProviderAttestationError(
            "Host issuer session identity conflicts with session material"
        )
    return HostIssuerSession(
        issuer_instance_id=issuer_id,
        started_at_utc=started,
        public_key_spki_base64=spki_text,
        public_key_sha256=key_digest,
        session_identity=session_identity,
        public_key_spki=spki,
    )


def _parse_subject(value: object) -> HostAuthenticatedReadSubject:
    raw = _exact_mapping(value, name="read subject", keys=_SUBJECT_KEYS)
    text_names = (
        "provider_id",
        "account_id",
        "entity_id",
        "runtime_environment",
        "provider_environment",
        "endpoint",
        "surface",
        "permission_scope",
        "data_entitlement",
        "instrument_version",
        "credential_handle_id",
        "capability_id",
        "qualification_id",
        "qualification_build_id",
        "adapter_build_identity",
        "transport_identity",
    )
    values = {
        name: _exact_text(raw[name], name=name)
        for name in text_names
    }
    runtime = values["runtime_environment"]
    if runtime not in _FINANCIAL_RUNTIMES:
        raise HostProviderAttestationError(
            "Host authenticated-read subject is outside PAPER/LIVE"
        )
    query_digest = _sha256_text(raw["query_digest"], name="query_digest")
    endpoint_rule = _sha256_text(
        raw["endpoint_rule_identity"],
        name="endpoint_rule_identity",
    )
    network_policy = _sha256_text(
        raw["network_policy_identity"],
        name="network_policy_identity",
    )
    generation = _positive_int(
        raw["credential_generation"],
        name="credential_generation",
    )
    return HostAuthenticatedReadSubject(
        provider_id=values["provider_id"],
        account_id=values["account_id"],
        entity_id=values["entity_id"],
        runtime_environment=runtime,
        provider_environment=values["provider_environment"],
        endpoint=values["endpoint"],
        surface=values["surface"],
        permission_scope=values["permission_scope"],
        data_entitlement=values["data_entitlement"],
        instrument_version=values["instrument_version"],
        query_digest=query_digest,
        endpoint_rule_identity=endpoint_rule,
        credential_handle_id=values["credential_handle_id"],
        credential_generation=generation,
        capability_id=values["capability_id"],
        qualification_id=values["qualification_id"],
        qualification_build_id=values["qualification_build_id"],
        adapter_build_identity=values["adapter_build_identity"],
        network_policy_identity=network_policy,
        transport_identity=values["transport_identity"],
    )


def _attempt_material(
    session_identity: str,
    subject: HostAuthenticatedReadSubject,
    read_generation: int,
    read_attempt_id: str,
    prepared_at_utc: str,
) -> bytes:
    return canonical_host_material(
        _READ_ATTEMPT_SCHEMA,
        session_identity,
        subject.provider_id,
        subject.account_id,
        subject.entity_id,
        subject.runtime_environment,
        subject.provider_environment,
        subject.endpoint,
        subject.surface,
        subject.permission_scope,
        subject.data_entitlement,
        subject.instrument_version,
        subject.query_digest,
        subject.endpoint_rule_identity,
        subject.credential_handle_id,
        str(subject.credential_generation),
        subject.capability_id,
        subject.qualification_id,
        subject.qualification_build_id,
        subject.adapter_build_identity,
        subject.network_policy_identity,
        subject.transport_identity,
        str(read_generation),
        read_attempt_id,
        prepared_at_utc,
    )


def _parse_attempt(
    value: object,
    *,
    session: HostIssuerSession,
) -> HostAuthenticatedReadAttempt:
    raw = _exact_mapping(value, name="read attempt", keys=_ATTEMPT_KEYS)
    if raw["schema"] != _READ_ATTEMPT_SCHEMA:
        raise HostProviderAttestationError("Host read-attempt schema is invalid")
    session_identity = _exact_text(
        raw["issuer_session_identity"],
        name="issuer_session_identity",
    )
    if session_identity != session.session_identity:
        raise HostProviderAttestationError(
            "Host read attempt uses a different issuer session"
        )
    subject = _parse_subject(raw["subject"])
    generation = _positive_int(raw["read_generation"], name="read_generation")
    attempt_id = _exact_text(raw["read_attempt_id"], name="read_attempt_id")
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise HostProviderAttestationError(
            "Host read_attempt_id is non-canonical"
        )
    prepared = _exact_text(raw["prepared_at_utc"], name="prepared_at_utc")
    if _host_utc_key(prepared, name="prepared_at_utc") < _host_utc_key(
        session.started_at_utc,
        name="started_at_utc",
    ):
        raise HostProviderAttestationError(
            "Host read attempt predates issuer session"
        )
    binding = _sha256_text(raw["binding_sha256"], name="binding_sha256")
    signature_text = _exact_text(
        raw["signature_base64"],
        name="signature_base64",
    )
    signature = _canonical_base64(
        signature_text,
        name="signature_base64",
        expected_length=64,
    )
    material = _attempt_material(
        session_identity,
        subject,
        generation,
        attempt_id,
        prepared,
    )
    if binding != _digest(material):
        raise HostProviderAttestationError(
            "Host read-attempt binding digest conflicts with signed material"
        )
    _verify_p256_sha256_p1363(
        spki=session.public_key_spki,
        material=material,
        signature=signature,
    )
    return HostAuthenticatedReadAttempt(
        issuer_session_identity=session_identity,
        subject=subject,
        read_generation=generation,
        read_attempt_id=attempt_id,
        prepared_at_utc=prepared,
        binding_sha256=binding,
        signature_base64=signature_text,
    )


def _exact_query(value: object) -> Mapping[str, str]:
    if type(value) is not dict or not value or len(value) > 64:
        raise HostProviderAttestationError(
            "Host authenticated-read query must be a non-empty exact object"
        )
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _exact_text(raw_key, name="query key")
        if type(raw_value) is not str or raw_value != raw_value.strip():
            raise HostProviderAttestationError(
                "Host authenticated-read query value must be canonical text"
            )
        if len(key) > 2048 or len(raw_value) > 2048:
            raise HostProviderAttestationError(
                "Host authenticated-read query component exceeds bound"
            )
        try:
            key.encode("ascii")
            raw_value.encode("ascii")
        except UnicodeEncodeError as error:
            raise HostProviderAttestationError(
                "Host authenticated-read query must be ASCII"
            ) from error
        if any(ord(item) < 0x20 for item in key + raw_value):
            raise HostProviderAttestationError(
                "Host authenticated-read query must be printable ASCII"
            )
        result[key] = raw_value
    return MappingProxyType(result)


def verify_host_prepared_attestation(
    value: object,
    *,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    expected_query: object,
) -> VerifiedHostPreparedAttestation:
    """Verify one signed Host Prepared envelope against independent pins.

    The v1 Host attempt signs the prepared-read binding digest, not the serialized
    query object itself.  The exact query therefore needs an independent pin at
    this verification boundary; otherwise a post-serialization substitution
    could be returned as if it were Host-verified material.
    """

    expected_session = _exact_text(
        expected_session_identity,
        name="expected_session_identity",
    )
    if _SESSION_ID_RE.fullmatch(expected_session) is None:
        raise HostProviderAttestationError(
            "expected_session_identity is non-canonical"
        )
    expected_key = _sha256_text(
        expected_public_key_sha256,
        name="expected_public_key_sha256",
    )
    raw = _exact_mapping(value, name="Host Prepared envelope", keys=_PREPARED_KEYS)
    if raw["schema"] != _PREPARED_ENVELOPE_SCHEMA:
        raise HostProviderAttestationError(
            "Host Prepared envelope schema is invalid"
        )
    query = _exact_query(raw["query"])
    pinned_query = _exact_query(expected_query)
    if query != pinned_query:
        raise HostProviderAttestationError(
            "Host authenticated-read query does not match independently pinned query"
        )
    session = _parse_session(raw["issuer_session"])
    if (
        session.session_identity != expected_session
        or session.public_key_sha256 != expected_key
    ):
        raise HostProviderAttestationError(
            "Host issuer session does not match independently pinned authority"
        )
    attempt = _parse_attempt(raw["attempt"], session=session)
    return VerifiedHostPreparedAttestation(
        issuer_session=session,
        attempt=attempt,
        query=query,
    )


def _receipt_material(receipt: HostAuthenticatedReadReceipt) -> bytes:
    return canonical_host_material(
        _READ_RECEIPT_SCHEMA,
        receipt.issuer_session_identity,
        receipt.read_attempt_binding_sha256,
        receipt.read_attempt_id,
        str(receipt.read_generation),
        str(receipt.http_status),
        receipt.response_sha256,
        str(receipt.response_length),
        receipt.observed_at_utc,
    )


def _parse_receipt(
    value: object,
    *,
    prepared: VerifiedHostPreparedAttestation,
    response_bytes: bytes,
) -> HostAuthenticatedReadReceipt:
    raw = _exact_mapping(value, name="read receipt", keys=_RECEIPT_KEYS)
    if raw["schema"] != _READ_RECEIPT_SCHEMA:
        raise HostProviderAttestationError("Host read-receipt schema is invalid")
    session = prepared.issuer_session
    attempt = prepared.attempt
    session_identity = _exact_text(
        raw["issuer_session_identity"],
        name="issuer_session_identity",
    )
    attempt_binding = _sha256_text(
        raw["read_attempt_binding_sha256"],
        name="read_attempt_binding_sha256",
    )
    attempt_id = _exact_text(raw["read_attempt_id"], name="read_attempt_id")
    generation = _positive_int(raw["read_generation"], name="read_generation")
    status = raw["http_status"]
    if type(status) is not int or not 100 <= status <= 599:
        raise HostProviderAttestationError(
            "Host read receipt HTTP status is invalid"
        )
    response_digest = _sha256_text(
        raw["response_sha256"],
        name="response_sha256",
    )
    response_length = _positive_int(
        raw["response_length"],
        name="response_length",
    )
    observed = _exact_text(raw["observed_at_utc"], name="observed_at_utc")
    if _host_utc_key(observed, name="observed_at_utc") < _host_utc_key(
        attempt.prepared_at_utc,
        name="prepared_at_utc",
    ):
        raise HostProviderAttestationError(
            "Host read receipt predates signed attempt"
        )
    receipt_sha = _sha256_text(raw["receipt_sha256"], name="receipt_sha256")
    signature_text = _exact_text(
        raw["signature_base64"],
        name="signature_base64",
    )
    signature = _canonical_base64(
        signature_text,
        name="signature_base64",
        expected_length=64,
    )
    if (
        session_identity != session.session_identity
        or attempt_binding != attempt.binding_sha256
        or attempt_id != attempt.read_attempt_id
        or generation != attempt.read_generation
    ):
        raise HostProviderAttestationError(
            "Host read receipt does not bind the verified Prepared attempt"
        )
    if response_length != len(response_bytes) or response_digest != _digest(
        response_bytes
    ):
        raise HostProviderAttestationError(
            "Host read receipt does not bind exact response bytes"
        )
    receipt = HostAuthenticatedReadReceipt(
        issuer_session_identity=session_identity,
        read_attempt_binding_sha256=attempt_binding,
        read_attempt_id=attempt_id,
        read_generation=generation,
        http_status=status,
        response_sha256=response_digest,
        response_length=response_length,
        observed_at_utc=observed,
        receipt_sha256=receipt_sha,
        signature_base64=signature_text,
    )
    material = _receipt_material(receipt)
    if receipt_sha != _digest(material):
        raise HostProviderAttestationError(
            "Host read-receipt digest conflicts with signed material"
        )
    _verify_p256_sha256_p1363(
        spki=session.public_key_spki,
        material=material,
        signature=signature,
    )
    return receipt


def _parse_prepared_durability(
    value: object,
    *,
    prepared: VerifiedHostPreparedAttestation,
    expected_journal_identity: str,
) -> HostPreparedDurabilityReceipt:
    raw = _exact_mapping(
        value,
        name="Host durable Prepared receipt",
        keys=_PREPARED_DURABILITY_KEYS,
    )
    if raw["schema"] != _PREPARED_DURABILITY_SCHEMA:
        raise HostProviderAttestationError(
            "Host durable Prepared receipt schema is invalid"
        )
    attempt = prepared.attempt
    session_identity = _exact_text(
        raw["issuer_session_identity"],
        name="durable Prepared issuer_session_identity",
    )
    attempt_id = _exact_text(
        raw["read_attempt_id"],
        name="durable Prepared read_attempt_id",
    )
    attempt_binding = _sha256_text(
        raw["read_attempt_binding_sha256"],
        name="durable Prepared read_attempt_binding_sha256",
    )
    query_digest = _sha256_text(
        raw["query_digest"],
        name="durable Prepared query_digest",
    )
    journal_identity = _sha256_text(
        raw["journal_identity"],
        name="durable Prepared journal_identity",
    )
    prepared_event_id = _exact_text(
        raw["prepared_event_id"],
        name="durable Prepared prepared_event_id",
    )
    sequence = _positive_int(
        raw["journal_sequence"],
        name="durable Prepared journal_sequence",
    )
    committed = _exact_text(
        raw["committed_at_utc"],
        name="durable Prepared committed_at_utc",
    )
    committed_key = _host_utc_key(
        committed,
        name="durable Prepared committed_at_utc",
    )
    receipt_identity = _exact_text(
        raw["receipt_identity"],
        name="durable Prepared receipt_identity",
    )
    if (
        session_identity != prepared.issuer_session.session_identity
        or attempt_id != attempt.read_attempt_id
        or attempt_binding != attempt.binding_sha256
        or query_digest != attempt.subject.query_digest
        or journal_identity != expected_journal_identity
        or prepared_event_id != attempt.read_attempt_id + ":prepared"
        or committed_key
        < _host_utc_key(attempt.prepared_at_utc, name="prepared_at_utc")
    ):
        raise HostProviderAttestationError(
            "Host durable Prepared receipt conflicts with verified read scope"
        )
    expected_identity = _content_identity(
        "provider-read-durable-prepared",
        canonical_host_material(
            _PREPARED_DURABILITY_SCHEMA,
            session_identity,
            attempt_id,
            attempt_binding,
            query_digest,
            journal_identity,
            prepared_event_id,
            str(sequence),
            committed,
        ),
    )
    if receipt_identity != expected_identity:
        raise HostProviderAttestationError(
            "Host durable Prepared receipt identity is invalid"
        )
    return HostPreparedDurabilityReceipt(
        issuer_session_identity=session_identity,
        read_attempt_id=attempt_id,
        read_attempt_binding_sha256=attempt_binding,
        query_digest=query_digest,
        journal_identity=journal_identity,
        prepared_event_id=prepared_event_id,
        journal_sequence=sequence,
        committed_at_utc=committed,
        receipt_identity=receipt_identity,
    )


def _parse_observed_durability(
    value: object,
    *,
    prepared: VerifiedHostPreparedAttestation,
    provider_receipt: HostAuthenticatedReadReceipt,
    prepared_durability: HostPreparedDurabilityReceipt,
) -> HostObservedDurabilityReceipt:
    raw = _exact_mapping(
        value,
        name="Host durable Observed receipt",
        keys=_OBSERVED_DURABILITY_KEYS,
    )
    if raw["schema"] != _OBSERVED_DURABILITY_SCHEMA:
        raise HostProviderAttestationError(
            "Host durable Observed receipt schema is invalid"
        )
    attempt = prepared.attempt
    session_identity = _exact_text(
        raw["issuer_session_identity"],
        name="durable Observed issuer_session_identity",
    )
    attempt_id = _exact_text(
        raw["read_attempt_id"],
        name="durable Observed read_attempt_id",
    )
    attempt_binding = _sha256_text(
        raw["read_attempt_binding_sha256"],
        name="durable Observed read_attempt_binding_sha256",
    )
    provider_receipt_sha = _sha256_text(
        raw["provider_receipt_sha256"],
        name="durable Observed provider_receipt_sha256",
    )
    response_sha = _sha256_text(
        raw["response_sha256"],
        name="durable Observed response_sha256",
    )
    http_status = raw["http_status"]
    if type(http_status) is not int or not 100 <= http_status <= 599:
        raise HostProviderAttestationError(
            "Host durable Observed HTTP status is invalid"
        )
    observed_at = _exact_text(
        raw["observed_at_utc"],
        name="durable Observed observed_at_utc",
    )
    _host_utc_key(observed_at, name="durable Observed observed_at_utc")
    journal_identity = _sha256_text(
        raw["journal_identity"],
        name="durable Observed journal_identity",
    )
    prepared_receipt_identity = _exact_text(
        raw["prepared_receipt_identity"],
        name="durable Observed prepared_receipt_identity",
    )
    prepared_event_id = _exact_text(
        raw["prepared_event_id"],
        name="durable Observed prepared_event_id",
    )
    prepared_sequence = _positive_int(
        raw["prepared_journal_sequence"],
        name="durable Observed prepared_journal_sequence",
    )
    observed_event_id = _exact_text(
        raw["observed_event_id"],
        name="durable Observed observed_event_id",
    )
    observed_sequence = _positive_int(
        raw["observed_journal_sequence"],
        name="durable Observed observed_journal_sequence",
    )
    committed = _exact_text(
        raw["committed_at_utc"],
        name="durable Observed committed_at_utc",
    )
    committed_key = _host_utc_key(
        committed,
        name="durable Observed committed_at_utc",
    )
    receipt_identity = _exact_text(
        raw["receipt_identity"],
        name="durable Observed receipt_identity",
    )
    if (
        session_identity != prepared.issuer_session.session_identity
        or attempt_id != attempt.read_attempt_id
        or attempt_binding != attempt.binding_sha256
        or provider_receipt_sha != provider_receipt.receipt_sha256
        or response_sha != provider_receipt.response_sha256
        or http_status != provider_receipt.http_status
        or observed_at != provider_receipt.observed_at_utc
        or journal_identity != prepared_durability.journal_identity
        or prepared_receipt_identity != prepared_durability.receipt_identity
        or prepared_event_id != prepared_durability.prepared_event_id
        or prepared_sequence != prepared_durability.journal_sequence
        or observed_event_id != attempt.read_attempt_id + ":observed"
        or observed_sequence <= prepared_sequence
        or committed_key
        < _host_utc_key(
            provider_receipt.observed_at_utc,
            name="provider observed_at_utc",
        )
    ):
        raise HostProviderAttestationError(
            "Host durable Observed receipt conflicts with verified provider response"
        )
    expected_identity = _content_identity(
        "provider-read-durable-observed",
        canonical_host_material(
            _OBSERVED_DURABILITY_SCHEMA,
            session_identity,
            attempt_id,
            attempt_binding,
            provider_receipt_sha,
            response_sha,
            str(http_status),
            observed_at,
            journal_identity,
            prepared_receipt_identity,
            prepared_event_id,
            str(prepared_sequence),
            observed_event_id,
            str(observed_sequence),
            committed,
        ),
    )
    if receipt_identity != expected_identity:
        raise HostProviderAttestationError(
            "Host durable Observed receipt identity is invalid"
        )
    return HostObservedDurabilityReceipt(
        issuer_session_identity=session_identity,
        read_attempt_id=attempt_id,
        read_attempt_binding_sha256=attempt_binding,
        provider_receipt_sha256=provider_receipt_sha,
        response_sha256=response_sha,
        http_status=http_status,
        observed_at_utc=observed_at,
        journal_identity=journal_identity,
        prepared_receipt_identity=prepared_receipt_identity,
        prepared_event_id=prepared_event_id,
        prepared_journal_sequence=prepared_sequence,
        observed_event_id=observed_event_id,
        observed_journal_sequence=observed_sequence,
        committed_at_utc=committed,
        receipt_identity=receipt_identity,
    )


def verify_host_observed_attestation(
    value: object,
    *,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    expected_query: object,
    expected_journal_identity: str,
) -> VerifiedHostObservedAttestation:
    """Verify signed Host response plus independently pinned durable journal cuts."""

    expected_journal = _sha256_text(
        expected_journal_identity,
        name="expected_journal_identity",
    )
    raw = _exact_mapping(value, name="Host Observed envelope", keys=_OBSERVED_KEYS)
    if raw["schema"] != _OBSERVED_ENVELOPE_SCHEMA:
        raise HostProviderAttestationError(
            "Host Observed envelope schema is invalid"
        )
    prepared = verify_host_prepared_attestation(
        {
            "schema": _PREPARED_ENVELOPE_SCHEMA,
            "issuer_session": raw["issuer_session"],
            "attempt": raw["attempt"],
            "query": raw["query"],
        },
        expected_session_identity=expected_session_identity,
        expected_public_key_sha256=expected_public_key_sha256,
        expected_query=expected_query,
    )
    response_bytes = _canonical_base64(
        raw["response_base64"],
        name="response_base64",
    )
    if not response_bytes:
        raise HostProviderAttestationError(
            "Host observed response bytes must be non-empty"
        )
    receipt = _parse_receipt(
        raw["receipt"],
        prepared=prepared,
        response_bytes=response_bytes,
    )
    prepared_durability = _parse_prepared_durability(
        raw["durable_prepared"],
        prepared=prepared,
        expected_journal_identity=expected_journal,
    )
    observed_durability = _parse_observed_durability(
        raw["durable_observed"],
        prepared=prepared,
        provider_receipt=receipt,
        prepared_durability=prepared_durability,
    )
    return VerifiedHostObservedAttestation(
        prepared=prepared,
        receipt=receipt,
        response_bytes=response_bytes,
        prepared_durability=prepared_durability,
        observed_durability=observed_durability,
    )


def _host_lifetime_lease_scope_id(
    provider_id: str,
    provider_environment: str,
    runtime_environment: str,
    account_id: str,
) -> str:
    material = (
        _HOST_LIFETIME_FENCE_DOMAIN
        + "\0"
        + provider_id
        + "\0"
        + provider_environment
        + "\0"
        + runtime_environment
        + "\0"
        + account_id
    ).encode("utf-8")
    return "hf-" + sha256(material).hexdigest()


def _host_sender_fence_material(receipt: HostSenderFenceReceipt) -> bytes:
    return canonical_host_material(
        _HOST_SENDER_FENCE_SCHEMA,
        receipt.issuer_session_identity,
        receipt.fence_method,
        receipt.lease_scope_id,
        receipt.lease_owner_record_sha256,
        receipt.lease_acquired_at_utc,
        receipt.owner_scope,
        receipt.provider_id,
        receipt.account_id,
        receipt.runtime_environment,
        receipt.provider_environment,
        receipt.credential_handle_id,
        str(receipt.credential_generation),
        receipt.backup_manifest_sha256,
        receipt.old_owner_id,
        str(receipt.old_owner_epoch),
        receipt.new_owner_id,
        str(receipt.new_owner_epoch),
        receipt.fenced_at_utc,
    )


def verify_host_sender_fence_attestation(
    value: object,
    *,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    expected_backup_manifest_sha256: str,
    expected_owner_scope: str,
    expected_provider_id: str,
    expected_account_id: str,
    expected_runtime_environment: str,
    expected_provider_environment: str,
    expected_credential_handle_id: str,
    expected_credential_generation: int,
    expected_old_owner_id: str,
    expected_old_owner_epoch: int,
    expected_new_owner_id: str,
    expected_new_owner_epoch: int,
) -> VerifiedHostSenderFenceAttestation:
    """Verify one Host-issued same-machine sender-fence challenge response.

    This verifier authenticates a successor Host that currently holds the exact
    OS lifetime lease and binds the signed proof to the caller's independently
    resolved restore/owner/provider/credential transition.  It does not promote
    the same-host lease into clean-machine or remote-provider credential
    revocation authority.
    """

    expected_session = _exact_text(
        expected_session_identity,
        name="expected_session_identity",
    )
    if _SESSION_ID_RE.fullmatch(expected_session) is None:
        raise HostProviderAttestationError(
            "expected_session_identity is non-canonical"
        )
    expected_key = _sha256_text(
        expected_public_key_sha256,
        name="expected_public_key_sha256",
    )
    expected_manifest = _sha256_text(
        expected_backup_manifest_sha256,
        name="expected_backup_manifest_sha256",
    )
    expected_scope = _exact_text(expected_owner_scope, name="expected_owner_scope")
    expected_provider = _exact_text(expected_provider_id, name="expected_provider_id")
    expected_account = _exact_text(expected_account_id, name="expected_account_id")
    expected_runtime = _exact_text(
        expected_runtime_environment,
        name="expected_runtime_environment",
    )
    expected_provider_environment_text = _exact_text(
        expected_provider_environment,
        name="expected_provider_environment",
    )
    expected_handle = _exact_text(
        expected_credential_handle_id,
        name="expected_credential_handle_id",
    )
    expected_old = _exact_text(expected_old_owner_id, name="expected_old_owner_id")
    expected_new = _exact_text(expected_new_owner_id, name="expected_new_owner_id")
    expected_generation = _positive_int(
        expected_credential_generation,
        name="expected_credential_generation",
    )
    expected_old_epoch_value = _positive_int(
        expected_old_owner_epoch,
        name="expected_old_owner_epoch",
    )
    expected_new_epoch_value = _positive_int(
        expected_new_owner_epoch,
        name="expected_new_owner_epoch",
    )
    if expected_new_epoch_value != expected_old_epoch_value + 1:
        raise HostProviderAttestationError(
            "expected sender-fence owner transition is not consecutive"
        )

    raw = _exact_mapping(
        value,
        name="Host sender-fence envelope",
        keys=_HOST_SENDER_FENCE_ENVELOPE_KEYS,
    )
    if raw["schema"] != _HOST_SENDER_FENCE_ENVELOPE_SCHEMA:
        raise HostProviderAttestationError(
            "Host sender-fence envelope schema is invalid"
        )
    session = _parse_session(raw["issuer_session"])
    if (
        session.session_identity != expected_session
        or session.public_key_sha256 != expected_key
    ):
        raise HostProviderAttestationError(
            "Host sender-fence issuer does not match independently pinned authority"
        )

    receipt_raw = _exact_mapping(
        raw["receipt"],
        name="Host sender-fence receipt",
        keys=_HOST_SENDER_FENCE_RECEIPT_KEYS,
    )
    if receipt_raw["schema"] != _HOST_SENDER_FENCE_SCHEMA:
        raise HostProviderAttestationError(
            "Host sender-fence receipt schema is invalid"
        )
    issuer_session_identity = _exact_text(
        receipt_raw["issuer_session_identity"],
        name="issuer_session_identity",
    )
    if issuer_session_identity != session.session_identity:
        raise HostProviderAttestationError(
            "Host sender-fence receipt uses a different issuer session"
        )
    fence_method = _exact_text(receipt_raw["fence_method"], name="fence_method")
    if fence_method != _HOST_SENDER_FENCE_METHOD:
        raise HostProviderAttestationError(
            "Host sender-fence method is unsupported"
        )
    lease_scope_id = _exact_text(
        receipt_raw["lease_scope_id"],
        name="lease_scope_id",
    )
    if _LEASE_SCOPE_RE.fullmatch(lease_scope_id) is None:
        raise HostProviderAttestationError(
            "Host sender-fence lease scope is non-canonical"
        )
    lease_owner_record_sha256 = _sha256_text(
        receipt_raw["lease_owner_record_sha256"],
        name="lease_owner_record_sha256",
    )
    lease_acquired_at_utc = _exact_text(
        receipt_raw["lease_acquired_at_utc"],
        name="lease_acquired_at_utc",
    )
    acquired_key = _host_utc_key(
        lease_acquired_at_utc,
        name="lease_acquired_at_utc",
    )
    if acquired_key < _host_utc_key(
        session.started_at_utc,
        name="started_at_utc",
    ):
        raise HostProviderAttestationError(
            "Host sender-fence lease predates issuer session"
        )

    owner_scope = _exact_text(receipt_raw["owner_scope"], name="owner_scope")
    provider_id = _exact_text(receipt_raw["provider_id"], name="provider_id")
    account_id = _exact_text(receipt_raw["account_id"], name="account_id")
    runtime_environment = _exact_text(
        receipt_raw["runtime_environment"],
        name="runtime_environment",
    )
    provider_environment = _exact_text(
        receipt_raw["provider_environment"],
        name="provider_environment",
    )
    credential_handle_id = _exact_text(
        receipt_raw["credential_handle_id"],
        name="credential_handle_id",
    )
    credential_generation = _positive_int(
        receipt_raw["credential_generation"],
        name="credential_generation",
    )
    backup_manifest_sha256 = _sha256_text(
        receipt_raw["backup_manifest_sha256"],
        name="backup_manifest_sha256",
    )
    old_owner_id = _exact_text(
        receipt_raw["old_owner_id"],
        name="old_owner_id",
    )
    old_owner_epoch = _positive_int(
        receipt_raw["old_owner_epoch"],
        name="old_owner_epoch",
    )
    new_owner_id = _exact_text(
        receipt_raw["new_owner_id"],
        name="new_owner_id",
    )
    new_owner_epoch = _positive_int(
        receipt_raw["new_owner_epoch"],
        name="new_owner_epoch",
    )
    fenced_at_utc = _exact_text(
        receipt_raw["fenced_at_utc"],
        name="fenced_at_utc",
    )
    fenced_key = _host_utc_key(fenced_at_utc, name="fenced_at_utc")
    if fenced_key < acquired_key:
        raise HostProviderAttestationError(
            "Host sender-fence receipt predates lease acquisition"
        )
    if old_owner_id == new_owner_id or new_owner_epoch != old_owner_epoch + 1:
        raise HostProviderAttestationError(
            "Host sender-fence owner transition is invalid"
        )
    if runtime_environment not in _FINANCIAL_RUNTIMES:
        raise HostProviderAttestationError(
            "Host sender-fence runtime is outside PAPER/LIVE"
        )
    if (
        provider_id != provider_id.upper()
        or provider_environment != provider_environment.upper()
    ):
        raise HostProviderAttestationError(
            "Host sender-fence provider domain is non-canonical"
        )
    if owner_scope != runtime_environment + ":" + account_id:
        raise HostProviderAttestationError(
            "Host sender-fence owner scope is inconsistent"
        )
    if lease_scope_id != _host_lifetime_lease_scope_id(
        provider_id,
        provider_environment,
        runtime_environment,
        account_id,
    ):
        raise HostProviderAttestationError(
            "Host sender-fence lease identity is inconsistent"
        )

    observed = (
        backup_manifest_sha256,
        owner_scope,
        provider_id,
        account_id,
        runtime_environment,
        provider_environment,
        credential_handle_id,
        credential_generation,
        old_owner_id,
        old_owner_epoch,
        new_owner_id,
        new_owner_epoch,
    )
    expected = (
        expected_manifest,
        expected_scope,
        expected_provider,
        expected_account,
        expected_runtime,
        expected_provider_environment_text,
        expected_handle,
        expected_generation,
        expected_old,
        expected_old_epoch_value,
        expected_new,
        expected_new_epoch_value,
    )
    if observed != expected:
        raise HostProviderAttestationError(
            "Host sender-fence receipt does not match independently resolved transition"
        )

    receipt_sha256 = _sha256_text(
        receipt_raw["receipt_sha256"],
        name="receipt_sha256",
    )
    signature_text = _exact_text(
        receipt_raw["signature_base64"],
        name="signature_base64",
    )
    signature = _canonical_base64(
        signature_text,
        name="signature_base64",
        expected_length=64,
    )
    receipt = HostSenderFenceReceipt(
        issuer_session_identity=issuer_session_identity,
        fence_method=fence_method,
        lease_scope_id=lease_scope_id,
        lease_owner_record_sha256=lease_owner_record_sha256,
        lease_acquired_at_utc=lease_acquired_at_utc,
        owner_scope=owner_scope,
        provider_id=provider_id,
        account_id=account_id,
        runtime_environment=runtime_environment,
        provider_environment=provider_environment,
        credential_handle_id=credential_handle_id,
        credential_generation=credential_generation,
        backup_manifest_sha256=backup_manifest_sha256,
        old_owner_id=old_owner_id,
        old_owner_epoch=old_owner_epoch,
        new_owner_id=new_owner_id,
        new_owner_epoch=new_owner_epoch,
        fenced_at_utc=fenced_at_utc,
        receipt_sha256=receipt_sha256,
        signature_base64=signature_text,
    )
    material = _host_sender_fence_material(receipt)
    if receipt.receipt_sha256 != _digest(material):
        raise HostProviderAttestationError(
            "Host sender-fence receipt digest conflicts with exact material"
        )
    _verify_p256_sha256_p1363(
        spki=session.public_key_spki,
        material=material,
        signature=signature,
    )
    return VerifiedHostSenderFenceAttestation(
        issuer_session=session,
        receipt=receipt,
    )
