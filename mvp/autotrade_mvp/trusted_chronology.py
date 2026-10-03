"""Challenge-bound external-time precursor for WP-48 trusted chronology.

This module does not create a TrustedChronologyCut and does not grant trading,
release, provider, or economic-edge authority.  It binds one fresh external-time
request to an exact durable journal frontier and parses only a canonical response
that echoes that challenge.  Signed qualification and atomic accepted-cut
publication remain separate fail-closed steps.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
import secrets
from uuid import UUID

from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .recovery import RecoveryController
from .production_host import (
    ProductionHostRuntimeOccurrence,
    require_current_production_host_runtime_occurrence,
)
from .store_identity import (
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)


_SCHEMA_VERSION = "1.1.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?Z$"
)

# Protocol v1 is a tiny, closed, flat object. Bound hostile bytes and reject
# nested containers before json.loads() can allocate an attacker-sized graph.
_MAX_MEASUREMENT_BYTES = 4096


class ChronologyScope(str, Enum):
    SOURCE_QUALIFICATION = "SOURCE_QUALIFICATION"
    RELEASE_RUNTIME = "RELEASE_RUNTIME"


@dataclass(frozen=True, slots=True)
class ChronologyChallenge:
    schema_version: str
    scope: ChronologyScope
    source_sha: str
    store_identity_digest: str
    owner_scope: str
    owner_id: str
    owner_epoch: int
    clock_incident_generation: int
    journal_sequence: int
    runtime_host_id: str
    runtime_account_id: str
    runtime_occurrence_id: str
    runtime_environment: str
    release_artifact_id: str | None
    release_artifact_sha256: str | None
    request_nonce: str
    challenge_digest: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "clock_incident_generation": str(self.clock_incident_generation),
            "journal_sequence": str(self.journal_sequence),
            "owner_epoch": str(self.owner_epoch),
            "owner_id": self.owner_id,
            "owner_scope": self.owner_scope,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "request_nonce": self.request_nonce,
            "runtime_account_id": self.runtime_account_id,
            "runtime_environment": self.runtime_environment,
            "runtime_host_id": self.runtime_host_id,
            "runtime_occurrence_id": self.runtime_occurrence_id,
            "schema_version": self.schema_version,
            "scope": self.scope.value,
            "source_sha": self.source_sha,
            "store_identity_digest": self.store_identity_digest,
        }


@dataclass(frozen=True, slots=True)
class ChronologyMeasurementTranscript:
    """Parsed challenge-bound transcript; not an accepted chronology cut."""

    schema_version: str
    challenge_digest: str
    request_nonce: str
    authority_id: str
    protocol_id: str
    protocol_version: str
    response_id: str
    utc_lower_bound: str
    utc_upper_bound: str

    @property
    def conservative_covered_utc(self) -> str:
        return self.utc_lower_bound


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _token(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"{name} must be a canonical non-empty string")
    return value


def _git_sha(value: object) -> str:
    value = _token(value, name="source_sha")
    if _GIT_SHA_RE.fullmatch(value) is None:
        raise ValueError("source_sha must be a lowercase 40-hex Git SHA")
    return value


def _sha256(value: object, *, name: str) -> str:
    value = _token(value, name=name)
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return value


def _uuid(value: object, *, name: str) -> str:
    value = _token(value, name=name)
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{name} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{name} must be a canonical lowercase UUID")
    return value


def _environment_from_owner_scope(owner_scope: str) -> str:
    environment, separator, scope = owner_scope.partition(":")
    environment = environment.strip().upper()
    if (
        environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}
        or separator != ":"
        or not scope
        or scope != scope.strip()
    ):
        raise PermissionError(
            "chronology challenge requires environment-scoped recovery owner"
        )
    return environment


def _store_identity_payload(identity: JournalStoreIdentity) -> dict[str, object]:
    identity = require_exact_journal_store_identity(
        identity,
        subject="chronology JournalStore identity",
    )
    return {
        "canonical_path": identity.canonical_path,
        "filesystem_device": (
            None
            if identity.filesystem_device is None
            else str(identity.filesystem_device)
        ),
        "filesystem_inode": (
            None
            if identity.filesystem_inode is None
            else str(identity.filesystem_inode)
        ),
        "identity_source": identity.identity_source,
        "windows_file_index_high": (
            None
            if identity.windows_file_index_high is None
            else str(identity.windows_file_index_high)
        ),
        "windows_file_index_low": (
            None
            if identity.windows_file_index_low is None
            else str(identity.windows_file_index_low)
        ),
        "windows_volume_serial": (
            None
            if identity.windows_volume_serial is None
            else str(identity.windows_volume_serial)
        ),
    }


def journal_store_identity_digest(identity: JournalStoreIdentity) -> str:
    payload = _canonical_json_bytes(_store_identity_payload(identity))
    return "sha256:" + sha256(payload).hexdigest()


def _challenge_digest(challenge: ChronologyChallenge) -> str:
    return "sha256:" + sha256(
        _canonical_json_bytes(challenge.canonical_payload())
    ).hexdigest()


def _require_exact_store(store: JournalStore) -> JournalStore:
    require_exact_journal_store_authority(
        store,
        subject="chronology JournalStore",
    )
    return store


def _selected_store_identity(store: JournalStore) -> JournalStoreIdentity:
    return require_exact_journal_store_authority(
        store,
        subject="chronology JournalStore",
    )


def _current_journal_sequence(store: JournalStore) -> int:
    identity = _selected_store_identity(store)
    with journal_store_authority_scope(store, identity):
        return JournalStore.current_journal_sequence(store)


def _require_exact_recovery(recovery: RecoveryController) -> RecoveryController:
    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be the canonical RecoveryController")
    return recovery


def _release_binding(
    scope: ChronologyScope,
    release_artifact_id: str | None,
    release_artifact_sha256: str | None,
) -> tuple[str | None, str | None]:
    if type(scope) is not ChronologyScope:
        raise TypeError("scope must be ChronologyScope")
    if scope is ChronologyScope.SOURCE_QUALIFICATION:
        if release_artifact_id is not None or release_artifact_sha256 is not None:
            raise ValueError(
                "SOURCE_QUALIFICATION chronology cannot carry release identity"
            )
        return None, None
    if (release_artifact_id is None) != (release_artifact_sha256 is None):
        raise ValueError(
            "RELEASE_RUNTIME requires release id and digest together"
        )
    if release_artifact_id is None:
        raise ValueError("RELEASE_RUNTIME requires authenticated release identity")
    return (
        _uuid(release_artifact_id, name="release_artifact_id"),
        _sha256(release_artifact_sha256, name="release_artifact_sha256"),
    )


def prepare_chronology_challenge(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    runtime_occurrence: ProductionHostRuntimeOccurrence,
    source_sha: str,
    scope: ChronologyScope,
    release_artifact_id: str | None = None,
    release_artifact_sha256: str | None = None,
) -> ChronologyChallenge:
    """Capture one stable durable frontier and generate a fresh request challenge."""

    store = _require_exact_store(store)
    recovery = _require_exact_recovery(recovery)
    source_sha = _git_sha(source_sha)
    release_artifact_id, release_artifact_sha256 = _release_binding(
        scope,
        release_artifact_id,
        release_artifact_sha256,
    )

    store_identity = _selected_store_identity(store)
    recovery_store_identity = recovery.durable_owner_store_identity
    if recovery_store_identity is None:
        raise PermissionError("chronology requires durable recovery authority")
    if recovery_store_identity != store_identity:
        raise PermissionError(
            "chronology store does not match durable recovery authority"
        )
    if recovery.clock_trusted is not True:
        raise PermissionError("local clock health is not currently trusted")
    owner_scope = recovery.owner_scope
    runtime_environment = _environment_from_owner_scope(owner_scope)
    owner = recovery.owner
    if owner is None:
        raise PermissionError("chronology requires an active recovery owner")

    runtime_occurrence = require_current_production_host_runtime_occurrence(
        journal=store,
        occurrence=runtime_occurrence,
    )
    if runtime_occurrence.environment != runtime_environment:
        raise PermissionError(
            "runtime occurrence environment does not match recovery owner scope"
        )

    journal_sequence = _current_journal_sequence(store)
    owner_chain = recovery.durable_owner_chain()
    if not owner_chain or owner_chain[-1] != owner:
        raise PermissionError("recovery owner is not the current durable owner")
    incident_generation = recovery.clock_incident_generation

    # The durable sequence detects canonical journal writes, but RecoveryController
    # is still an in-process object.  Recheck every mutable selector used above
    # and build the challenge only from the captured values so caller-side
    # rebinding cannot create a mixed-frontier capability.
    if recovery.durable_owner_store_identity != store_identity:
        raise PermissionError(
            "durable recovery store changed during chronology frontier capture"
        )
    if recovery.clock_trusted is not True:
        raise PermissionError(
            "clock health changed during chronology frontier capture"
        )
    if recovery.owner != owner or recovery.owner_scope != owner_scope:
        raise PermissionError(
            "recovery owner scope changed during chronology frontier capture"
        )
    if recovery.clock_incident_generation != incident_generation:
        raise PermissionError(
            "clock incident generation changed during chronology frontier capture"
        )
    if (
        require_current_production_host_runtime_occurrence(
            journal=store,
            occurrence=runtime_occurrence,
        )
        != runtime_occurrence
    ):
        raise PermissionError(
            "runtime occurrence changed during chronology frontier capture"
        )
    if _current_journal_sequence(store) != journal_sequence:
        raise PermissionError(
            "durable journal changed during chronology frontier capture"
        )
    # Nothing authority-bearing runs between this post-sequence selector check
    # and challenge construction; all challenge fields below use captured values.
    if (
        recovery.durable_owner_store_identity != store_identity
        or recovery.clock_trusted is not True
        or recovery.owner != owner
        or recovery.owner_scope != owner_scope
    ):
        raise PermissionError(
            "recovery owner scope changed during chronology frontier capture"
        )

    request_nonce = secrets.token_hex(32)
    challenge = ChronologyChallenge(
        schema_version=_SCHEMA_VERSION,
        scope=scope,
        source_sha=source_sha,
        store_identity_digest=journal_store_identity_digest(store_identity),
        owner_scope=owner_scope,
        owner_id=owner.owner_id,
        owner_epoch=owner.epoch,
        clock_incident_generation=incident_generation,
        journal_sequence=journal_sequence,
        runtime_host_id=runtime_occurrence.host_id,
        runtime_account_id=runtime_occurrence.account_id,
        runtime_occurrence_id=runtime_occurrence.runtime_occurrence_id,
        runtime_environment=runtime_environment,
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
        request_nonce=request_nonce,
        challenge_digest="",
    )
    return replace(
        challenge,
        challenge_digest=_challenge_digest(challenge),
    )


def _validate_challenge(challenge: ChronologyChallenge) -> ChronologyChallenge:
    if type(challenge) is not ChronologyChallenge:
        raise TypeError("challenge must be exact ChronologyChallenge")
    if challenge.schema_version != _SCHEMA_VERSION:
        raise ValueError("chronology challenge schema version is unsupported")
    if type(challenge.scope) is not ChronologyScope:
        raise TypeError("chronology challenge scope is invalid")
    _git_sha(challenge.source_sha)
    _sha256(challenge.store_identity_digest, name="store_identity_digest")
    _token(challenge.owner_scope, name="owner_scope")
    _token(challenge.owner_id, name="owner_id")
    _token(challenge.runtime_host_id, name="runtime_host_id")
    _token(challenge.runtime_account_id, name="runtime_account_id")
    _uuid(challenge.runtime_occurrence_id, name="runtime_occurrence_id")
    _environment_from_owner_scope(challenge.owner_scope)
    if challenge.runtime_environment != _environment_from_owner_scope(
        challenge.owner_scope
    ):
        raise ValueError("chronology challenge runtime environment is invalid")
    for value, name, allow_zero in (
        (challenge.owner_epoch, "owner_epoch", False),
        (
            challenge.clock_incident_generation,
            "clock_incident_generation",
            True,
        ),
        (challenge.journal_sequence, "journal_sequence", True),
    ):
        if (
            type(value) is not int
            or value < 0
            or (not allow_zero and value == 0)
        ):
            raise ValueError(f"{name} is not a canonical integer")
    _release_binding(
        challenge.scope,
        challenge.release_artifact_id,
        challenge.release_artifact_sha256,
    )
    if (
        type(challenge.request_nonce) is not str
        or _NONCE_RE.fullmatch(challenge.request_nonce) is None
    ):
        raise ValueError("chronology request nonce is invalid")
    _sha256(challenge.challenge_digest, name="challenge_digest")
    if challenge.challenge_digest != _challenge_digest(challenge):
        raise ValueError("chronology challenge digest mismatch")
    return challenge


def require_current_chronology_challenge(
    *,
    challenge: ChronologyChallenge,
    store: JournalStore,
    recovery: RecoveryController,
    runtime_occurrence: ProductionHostRuntimeOccurrence,
) -> None:
    """Fail if any durable/runtime generation changed after the request frontier."""

    challenge = _validate_challenge(challenge)
    store = _require_exact_store(store)
    recovery = _require_exact_recovery(recovery)
    runtime_occurrence = require_current_production_host_runtime_occurrence(
        journal=store,
        occurrence=runtime_occurrence,
    )
    if (
        runtime_occurrence.host_id != challenge.runtime_host_id
        or runtime_occurrence.account_id != challenge.runtime_account_id
        or runtime_occurrence.runtime_occurrence_id
        != challenge.runtime_occurrence_id
        or runtime_occurrence.environment != challenge.runtime_environment
    ):
        raise PermissionError("production runtime occurrence changed")
    store_identity = _selected_store_identity(store)
    if recovery.durable_owner_store_identity != store_identity:
        raise PermissionError("durable recovery store identity changed")
    if (
        journal_store_identity_digest(store_identity)
        != challenge.store_identity_digest
    ):
        raise PermissionError("chronology journal physical identity changed")
    if recovery.clock_trusted is not True:
        raise PermissionError("clock health changed after chronology challenge")
    owner = recovery.owner
    if (
        owner is None
        or owner.owner_id != challenge.owner_id
        or owner.epoch != challenge.owner_epoch
        or recovery.owner_scope != challenge.owner_scope
    ):
        raise PermissionError("recovery owner generation changed")
    if recovery.clock_incident_generation != challenge.clock_incident_generation:
        raise PermissionError("clock incident generation changed")
    if _current_journal_sequence(store) != challenge.journal_sequence:
        raise PermissionError("durable journal advanced after chronology challenge")
    # Catch caller-side rebinding that occurs inside the final durable read.
    # All durable generation changes advance the journal sequence; these
    # selectors cover the in-process RecoveryController surface itself.
    if recovery.durable_owner_store_identity != store_identity:
        raise PermissionError("durable recovery store identity changed")
    if recovery.clock_trusted is not True:
        raise PermissionError("clock health changed after chronology challenge")
    owner = recovery.owner
    if (
        owner is None
        or owner.owner_id != challenge.owner_id
        or owner.epoch != challenge.owner_epoch
        or recovery.owner_scope != challenge.owner_scope
    ):
        raise PermissionError("recovery owner generation changed")


def _strict_json_object(data: bytes) -> dict[str, object]:
    if type(data) is not bytes or not data:
        raise ValueError("chronology measurement must be non-empty bytes")
    if len(data) > _MAX_MEASUREMENT_BYTES:
        raise ValueError("chronology measurement exceeds protocol byte budget")

    # The v1 schema contains one root object and scalar string values only.
    # Preflight raw structure before the JSON decoder allocates nested objects.
    quoted = False
    escaped = False
    root_opened = False
    root_closed = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:  # backslash
                escaped = True
            elif byte == 34:  # quote
                quoted = False
            continue
        if byte == 34:
            quoted = True
        elif byte == 123:  # {
            if root_opened or root_closed:
                raise ValueError(
                    "chronology measurement must be a flat JSON object"
                )
            root_opened = True
        elif byte == 125:  # }
            if not root_opened or root_closed:
                raise ValueError(
                    "chronology measurement must be a flat JSON object"
                )
            root_closed = True
        elif byte in (91, 93):  # [ ]
            raise ValueError(
                "chronology measurement must be a flat JSON object"
            )

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(
                    "chronology measurement contains duplicate JSON key"
                )
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("chronology measurement is not valid UTF-8 JSON") from error
    if type(value) is not dict:
        raise ValueError("chronology measurement must be a JSON object")
    try:
        canonical = _canonical_json_bytes(value)
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError(
            "chronology measurement cannot be canonically encoded as UTF-8 JSON"
        ) from error
    if canonical != data:
        raise ValueError("chronology measurement bytes are not canonical JSON")
    return value


def _utc_instant(value: object, *, name: str) -> tuple[str, datetime]:
    value = _token(value, name=name)
    if _UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical UTC")
    if "." in value:
        fraction = value.rsplit(".", 1)[1][:-1]
        if fraction.endswith("0"):
            raise ValueError(f"{name} has non-canonical fractional seconds")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} is not a real UTC instant") from error
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{name} is not UTC")
    return value, parsed


def parse_challenge_bound_measurement(
    data: bytes,
    *,
    challenge: ChronologyChallenge,
) -> ChronologyMeasurementTranscript:
    """Parse one canonical response that cryptographically names the request."""

    challenge = _validate_challenge(challenge)
    payload = _strict_json_object(data)
    expected_keys = {
        "authority_id",
        "challenge_digest",
        "protocol_id",
        "protocol_version",
        "request_nonce",
        "response_id",
        "schema_version",
        "utc_lower_bound",
        "utc_upper_bound",
    }
    if set(payload) != expected_keys:
        raise ValueError("chronology measurement schema is not closed")
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("chronology measurement schema version is unsupported")
    if payload["challenge_digest"] != challenge.challenge_digest:
        raise PermissionError(
            "external chronology response is bound to another frontier"
        )
    if payload["request_nonce"] != challenge.request_nonce:
        raise PermissionError(
            "external chronology response does not echo request nonce"
        )
    authority_id = _token(payload["authority_id"], name="authority_id")
    protocol_id = _token(payload["protocol_id"], name="protocol_id")
    protocol_version = _token(
        payload["protocol_version"],
        name="protocol_version",
    )
    response_id = _token(payload["response_id"], name="response_id")
    lower_text, lower = _utc_instant(
        payload["utc_lower_bound"],
        name="utc_lower_bound",
    )
    upper_text, upper = _utc_instant(
        payload["utc_upper_bound"],
        name="utc_upper_bound",
    )
    if upper < lower:
        raise ValueError("chronology UTC interval is reversed")
    return ChronologyMeasurementTranscript(
        schema_version=_SCHEMA_VERSION,
        challenge_digest=challenge.challenge_digest,
        request_nonce=challenge.request_nonce,
        authority_id=authority_id,
        protocol_id=protocol_id,
        protocol_version=protocol_version,
        response_id=response_id,
        utc_lower_bound=lower_text,
        utc_upper_bound=upper_text,
    )
