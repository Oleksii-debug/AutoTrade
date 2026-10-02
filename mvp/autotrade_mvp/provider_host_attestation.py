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
_OBSERVED_ENVELOPE_SCHEMA = "autotrade-host-authenticated-read-observed:v1"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SESSION_ID_RE = re.compile(r"provider-issuer-session:sha256:[0-9a-f]{64}")
_ATTEMPT_ID_RE = re.compile(r"provider-read:[0-9a-f]{32}")
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
    {"schema", "issuer_session", "attempt", "receipt", "query", "response_base64"}
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
    year, month, day, hour, minute, second, fraction = (
        int(item) for item in match.groups()
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
class VerifiedHostObservedAttestation:
    prepared: VerifiedHostPreparedAttestation
    receipt: HostAuthenticatedReadReceipt
    response_bytes: bytes


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


def verify_host_observed_attestation(
    value: object,
    *,
    expected_session_identity: str,
    expected_public_key_sha256: str,
    expected_query: object,
) -> VerifiedHostObservedAttestation:
    """Verify exact Host Prepared + provider response receipt + response bytes."""

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
    return VerifiedHostObservedAttestation(
        prepared=prepared,
        receipt=receipt,
        response_bytes=response_bytes,
    )
