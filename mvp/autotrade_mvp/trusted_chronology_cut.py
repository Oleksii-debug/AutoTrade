"""Durable accepted trusted-chronology cuts for WP-48.

This module is the terminal composition layer above the canonical chronology
challenge protocol. It deliberately reuses that protocol's source/release/runtime
selectors, the shared qualification trust boundary, JournalStore, and recovery
authority. It does not implement a clock service, mint a signer/trust root,
grant provider/PAPER/LIVE authority, or establish economic edge.

SOURCE_QUALIFICATION remains runtime-free exactly as the canonical precursor
defines it. RELEASE_RUNTIME inherits the precursor's exact ProductionHostRuntime
occurrence binding.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from uuid import NAMESPACE_URL, UUID, uuid5

from autotrade_runtime.artifacts import ArtifactStore

from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .production_host import (
    ProductionHostRuntime,
    ProductionHostRuntimeOccurrence,
    require_current_production_host_runtime_occurrence,
)
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    SignedQualificationAttestation,
    parse_signed_qualification_attestation,
    verify_canonical_qualification_attestation,
)
from .recovery import RecoveryController
from .sender_gate import journal_sender_gate
from .trusted_chronology import (
    ChronologyChallenge,
    ChronologyMeasurementTranscript,
    ChronologyScope,
    journal_store_identity_digest,
    parse_challenge_bound_measurement,
    prepare_chronology_challenge,
)


_SCHEMA_VERSION = "1.0.0"
_CHALLENGE_SCHEMA_VERSION = "1.1.0"
_AGGREGATE_TYPE = "trusted_chronology"
_PREPARED_EVENT = "TrustedChronologyChallengePrepared"
_ACCEPTED_EVENT = "TrustedChronologyCutAccepted"

# Canonical trust scope recorded in #1018. The independently controlled
# qualification policy must authorize this scope; this module never supplies a
# caller-selected policy or trust root.
_QUALIFICATION_DOMAIN = "HOST_CLOCK"
_QUALIFICATION_GATE = "CHRONOLOGY"
_QUALIFICATION_PACKAGE = "WP-48"
_QUALIFICATION_PROTOCOL = "trusted-chronology-cut-v1"
_QUALIFICATION_PROTOCOL_VERSION = "1.0.0"
_QUALIFICATION_REQUIREMENT = "independent-utc-chronology-cut"

# The dynamic requirement is additional signed material. It binds one exact
# challenge/measurement/limit subject without replacing the canonical requirement.
_DYNAMIC_REQUIREMENT_PREFIX = "trusted-chronology:"
_MEASUREMENT_KIND = "TRUSTED_CHRONOLOGY_MEASUREMENT"
_MEASUREMENT_MEDIA_TYPE = "application/json"

# Product-owned protocol-v1 limits. Callers cannot relax these.
_MAX_REQUEST_ELAPSED_NS = 30_000_000_000
_MAX_LOCAL_RATE_DIVERGENCE_NS = 1_000_000_000
_MAX_EXTERNAL_UNCERTAINTY_US = 2_000_000
_MAX_EXTERNAL_LOCAL_OFFSET_US = 5_000_000
_MAX_SIGNED_RECEIPT_BYTES = 1_048_576

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TrustedChronologyError(ValueError):
    """Raised when trusted chronology cannot cross the terminal trust boundary."""


@dataclass(frozen=True, slots=True)
class DurableChronologyAttempt:
    challenge: ChronologyChallenge
    prepared_event_id: str
    prepared_journal_sequence: int
    started_monotonic_ns: int
    started_wall_utc_ns: int


@dataclass(frozen=True, slots=True)
class TrustedChronologyCut:
    cut_id: str
    challenge_digest: str
    scope: ChronologyScope
    source_sha: str
    store_identity_digest: str
    owner_scope: str
    owner_id: str
    owner_epoch: int
    clock_incident_generation: int
    runtime_environment: str
    release_artifact_id: str | None
    release_artifact_sha256: str | None
    runtime_host_id: str | None
    runtime_occurrence_id: str | None
    runtime_occurrence_version: int | None
    runtime_occurrence_journal_sequence: int | None
    covered_utc: str
    utc_lower_bound: str
    utc_upper_bound: str
    external_authority_id: str
    external_protocol_id: str
    external_protocol_version: str
    external_response_id: str
    measurement_artifact_id: str
    measurement_sha256: str
    measurement_requirement_id: str
    accepted_attestation_id: str
    accepted_attestation_digest: str
    accepted_policy_id: str
    accepted_trust_root_id: str
    accepted_journal_sequence: int
    cut_digest: str


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _storage_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _token(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise TrustedChronologyError(f"{name} must be canonical non-empty text")
    return value


def _git_sha(value: object, *, name: str = "source_sha") -> str:
    if (
        type(value) is not str
        or len(value) != 40
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise TrustedChronologyError(
            f"{name} must be a lowercase 40-character Git SHA"
        )
    return value


def _digest(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(ch not in "0123456789abcdef" for ch in value[7:])
    ):
        raise TrustedChronologyError(f"{name} must be canonical sha256:<hex>")
    return value


def _uuid_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TrustedChronologyError(f"{name} must be exact str")
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise TrustedChronologyError(f"{name} must be a canonical UUID") from error
    if str(parsed) != value:
        raise TrustedChronologyError(f"{name} must be a canonical lowercase UUID")
    return value


def _decimal_int(value: object, *, name: str, positive: bool = False) -> int:
    if (
        type(value) is not str
        or not value
        or not value.isdigit()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise TrustedChronologyError(f"{name} is not a canonical decimal integer")
    parsed = int(value)
    if positive and parsed <= 0:
        raise TrustedChronologyError(f"{name} must be positive")
    return parsed


def _exact_nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TrustedChronologyError(f"{name} must be a non-negative exact int")
    return value


def _exact_positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise TrustedChronologyError(f"{name} must be a positive exact int")
    return value


def _runtime_environment(value: object) -> str:
    if type(value) is not str or value not in {
        "REPLAY",
        "SIMULATION",
        "PAPER",
        "LIVE",
    }:
        raise TrustedChronologyError("runtime_environment is not canonical")
    return value


def _instant(value: object, *, name: str) -> tuple[str, datetime]:
    value = _token(value, name=name)
    if not value.endswith("Z"):
        raise TrustedChronologyError(f"{name} must be canonical UTC")
    if "." in value:
        fraction = value.rsplit(".", 1)[1][:-1]
        if not fraction or len(fraction) > 6 or fraction.endswith("0"):
            raise TrustedChronologyError(f"{name} has non-canonical fractional seconds")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise TrustedChronologyError(f"{name} is not a real UTC instant") from error
    if parsed.tzinfo != timezone.utc:
        raise TrustedChronologyError(f"{name} is not UTC")
    rendered = parsed.isoformat().replace("+00:00", "Z")
    if parsed.microsecond == 0:
        rendered = parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if rendered != value:
        raise TrustedChronologyError(f"{name} is not canonical UTC")
    return value, parsed


def _datetime_us(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _selected_store_identity(store: JournalStore):
    return require_exact_journal_store_authority(
        store,
        subject="trusted chronology JournalStore",
    )


def _current_sequence(store: JournalStore) -> int:
    identity = _selected_store_identity(store)
    with journal_store_authority_scope(store, identity):
        return JournalStore.current_journal_sequence(store)


def _load_events(
    store: JournalStore,
    aggregate_id: str,
) -> tuple[dict[str, object], ...]:
    identity = _selected_store_identity(store)
    with journal_store_authority_scope(store, identity):
        events = JournalStore.load_events(store, _AGGREGATE_TYPE, aggregate_id)
    if type(events) is not list:
        raise TrustedChronologyError("trusted chronology journal read is non-canonical")
    return tuple(events)


def _require_exact_recovery(recovery: RecoveryController) -> RecoveryController:
    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    return recovery


def _owner_account(owner_scope: str) -> str:
    scope = _token(owner_scope, name="owner_scope")
    environment, separator, account = scope.partition(":")
    if (
        separator != ":"
        or environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}
        or not account
        or account != account.strip()
    ):
        raise TrustedChronologyError(
            "trusted chronology requires environment/account-scoped recovery owner"
        )
    return account


def _challenge_digest(challenge: ChronologyChallenge) -> str:
    return "sha256:" + sha256(
        _canonical_json_bytes(challenge.canonical_payload())
    ).hexdigest()


def _optional_positive_decimal(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    return _decimal_int(value, name=name, positive=True)


def _challenge_from_payload(
    raw: object,
    *,
    expected_digest: object,
) -> ChronologyChallenge:
    if type(raw) is not dict:
        raise TrustedChronologyError("durable chronology challenge is not an object")
    expected_keys = {
        "clock_incident_generation",
        "journal_sequence",
        "owner_epoch",
        "owner_id",
        "owner_scope",
        "release_artifact_id",
        "release_artifact_sha256",
        "runtime_host_id",
        "runtime_occurrence_id",
        "runtime_occurrence_journal_sequence",
        "runtime_occurrence_version",
        "request_nonce",
        "runtime_environment",
        "schema_version",
        "scope",
        "source_sha",
        "store_identity_digest",
    }
    if set(raw) != expected_keys:
        raise TrustedChronologyError("durable chronology challenge schema is not closed")
    if raw.get("schema_version") != _CHALLENGE_SCHEMA_VERSION:
        raise TrustedChronologyError("durable chronology challenge version is unsupported")
    try:
        scope = ChronologyScope(raw.get("scope"))
    except (ValueError, TypeError) as error:
        raise TrustedChronologyError("durable chronology challenge scope is invalid") from error

    source_sha = _git_sha(raw.get("source_sha"))
    store_digest = _digest(
        raw.get("store_identity_digest"),
        name="store_identity_digest",
    )
    owner_scope = _token(raw.get("owner_scope"), name="owner_scope")
    owner_id = _token(raw.get("owner_id"), name="owner_id")
    owner_epoch = _decimal_int(raw.get("owner_epoch"), name="owner_epoch", positive=True)
    incident_generation = _decimal_int(
        raw.get("clock_incident_generation"),
        name="clock_incident_generation",
    )
    journal_sequence = _decimal_int(raw.get("journal_sequence"), name="journal_sequence")
    environment = _runtime_environment(raw.get("runtime_environment"))
    if owner_scope.partition(":")[0] != environment:
        raise TrustedChronologyError(
            "durable chronology challenge owner/runtime environment mismatch"
        )

    release_id = raw.get("release_artifact_id")
    release_sha = raw.get("release_artifact_sha256")
    runtime_host_id = raw.get("runtime_host_id")
    runtime_occurrence_id = raw.get("runtime_occurrence_id")
    runtime_occurrence_version = _optional_positive_decimal(
        raw.get("runtime_occurrence_version"),
        name="runtime_occurrence_version",
    )
    runtime_occurrence_sequence = _optional_positive_decimal(
        raw.get("runtime_occurrence_journal_sequence"),
        name="runtime_occurrence_journal_sequence",
    )

    if scope is ChronologyScope.SOURCE_QUALIFICATION:
        if release_id is not None or release_sha is not None:
            raise TrustedChronologyError(
                "SOURCE_QUALIFICATION challenge cannot carry release identity"
            )
        if any(
            value is not None
            for value in (
                runtime_host_id,
                runtime_occurrence_id,
                runtime_occurrence_version,
                runtime_occurrence_sequence,
            )
        ):
            raise TrustedChronologyError(
                "SOURCE_QUALIFICATION challenge cannot carry runtime occurrence"
            )
    else:
        release_id = _uuid_text(release_id, name="release_artifact_id")
        release_sha = _digest(release_sha, name="release_artifact_sha256")
        runtime_host_id = _token(runtime_host_id, name="runtime_host_id")
        runtime_occurrence_id = _uuid_text(
            runtime_occurrence_id,
            name="runtime_occurrence_id",
        )
        if runtime_occurrence_version is None or runtime_occurrence_sequence is None:
            raise TrustedChronologyError(
                "RELEASE_RUNTIME challenge runtime occurrence is incomplete"
            )
        if runtime_occurrence_sequence > journal_sequence:
            raise TrustedChronologyError(
                "runtime occurrence postdates chronology challenge frontier"
            )

    nonce = raw.get("request_nonce")
    if (
        type(nonce) is not str
        or len(nonce) != 64
        or any(ch not in "0123456789abcdef" for ch in nonce)
    ):
        raise TrustedChronologyError("durable chronology request nonce is invalid")

    challenge = ChronologyChallenge(
        schema_version=_CHALLENGE_SCHEMA_VERSION,
        scope=scope,
        source_sha=source_sha,
        store_identity_digest=store_digest,
        owner_scope=owner_scope,
        owner_id=owner_id,
        owner_epoch=owner_epoch,
        clock_incident_generation=incident_generation,
        journal_sequence=journal_sequence,
        runtime_environment=environment,
        release_artifact_id=release_id,
        release_artifact_sha256=release_sha,
        runtime_host_id=runtime_host_id,
        runtime_occurrence_id=runtime_occurrence_id,
        runtime_occurrence_version=runtime_occurrence_version,
        runtime_occurrence_journal_sequence=runtime_occurrence_sequence,
        request_nonce=nonce,
        challenge_digest="",
    )
    digest = _digest(expected_digest, name="challenge_digest")
    challenge = replace(challenge, challenge_digest=digest)
    if challenge.canonical_payload() != raw:
        raise TrustedChronologyError("durable chronology challenge is not canonical")
    if _challenge_digest(challenge) != digest:
        raise TrustedChronologyError("durable chronology challenge digest mismatch")
    return challenge


def _prepared_event_id(challenge_digest: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://autotrade.local/trusted-chronology/prepared/"
            + challenge_digest,
        )
    )


def _cut_id(challenge_digest: str, attestation_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://autotrade.local/trusted-chronology/cut/"
            + challenge_digest
            + "/"
            + attestation_id,
        )
    )


def _snapshot_challenge(challenge: ChronologyChallenge) -> ChronologyChallenge:
    """Reconstruct a challenge only from exact inert scalar fields."""

    if type(challenge) is not ChronologyChallenge:
        raise TypeError("attempt challenge must be exact ChronologyChallenge")
    if type(challenge.scope) is not ChronologyScope:
        raise TrustedChronologyError("attempt challenge scope must be exact ChronologyScope")

    release_id = challenge.release_artifact_id
    if release_id is not None:
        release_id = _uuid_text(release_id, name="release_artifact_id")
    release_sha = challenge.release_artifact_sha256
    if release_sha is not None:
        release_sha = _digest(release_sha, name="release_artifact_sha256")
    runtime_host_id = challenge.runtime_host_id
    if runtime_host_id is not None:
        runtime_host_id = _token(runtime_host_id, name="runtime_host_id")
    runtime_occurrence_id = challenge.runtime_occurrence_id
    if runtime_occurrence_id is not None:
        runtime_occurrence_id = _uuid_text(
            runtime_occurrence_id,
            name="runtime_occurrence_id",
        )
    runtime_occurrence_version = challenge.runtime_occurrence_version
    if runtime_occurrence_version is not None:
        runtime_occurrence_version = _exact_positive_int(
            runtime_occurrence_version,
            name="runtime_occurrence_version",
        )
    runtime_occurrence_sequence = challenge.runtime_occurrence_journal_sequence
    if runtime_occurrence_sequence is not None:
        runtime_occurrence_sequence = _exact_positive_int(
            runtime_occurrence_sequence,
            name="runtime_occurrence_journal_sequence",
        )

    payload = {
        "clock_incident_generation": str(
            _exact_nonnegative_int(
                challenge.clock_incident_generation,
                name="clock_incident_generation",
            )
        ),
        "journal_sequence": str(
            _exact_nonnegative_int(
                challenge.journal_sequence,
                name="journal_sequence",
            )
        ),
        "owner_epoch": str(
            _exact_positive_int(challenge.owner_epoch, name="owner_epoch")
        ),
        "owner_id": _token(challenge.owner_id, name="owner_id"),
        "owner_scope": _token(challenge.owner_scope, name="owner_scope"),
        "release_artifact_id": release_id,
        "release_artifact_sha256": release_sha,
        "runtime_host_id": runtime_host_id,
        "runtime_occurrence_id": runtime_occurrence_id,
        "runtime_occurrence_journal_sequence": (
            None
            if runtime_occurrence_sequence is None
            else str(runtime_occurrence_sequence)
        ),
        "runtime_occurrence_version": (
            None
            if runtime_occurrence_version is None
            else str(runtime_occurrence_version)
        ),
        "request_nonce": _token(challenge.request_nonce, name="request_nonce"),
        "runtime_environment": _runtime_environment(challenge.runtime_environment),
        "schema_version": _token(challenge.schema_version, name="schema_version"),
        "scope": challenge.scope.value,
        "source_sha": _git_sha(challenge.source_sha),
        "store_identity_digest": _digest(
            challenge.store_identity_digest,
            name="store_identity_digest",
        ),
    }
    return _challenge_from_payload(
        payload,
        expected_digest=_digest(
            challenge.challenge_digest,
            name="challenge_digest",
        ),
    )


def _snapshot_attempt(
    attempt: DurableChronologyAttempt,
) -> DurableChronologyAttempt:
    """Detach caller-owned attempt/challenge state before authority-bearing use."""

    if type(attempt) is not DurableChronologyAttempt:
        raise TypeError("attempt must be exact DurableChronologyAttempt")
    challenge = _snapshot_challenge(attempt.challenge)
    prepared_event_id = _uuid_text(
        attempt.prepared_event_id,
        name="prepared_event_id",
    )
    if prepared_event_id != _prepared_event_id(challenge.challenge_digest):
        raise TrustedChronologyError(
            "trusted chronology prepared event id differs from challenge"
        )
    return DurableChronologyAttempt(
        challenge=challenge,
        prepared_event_id=prepared_event_id,
        prepared_journal_sequence=_exact_positive_int(
            attempt.prepared_journal_sequence,
            name="prepared_journal_sequence",
        ),
        started_monotonic_ns=_exact_nonnegative_int(
            attempt.started_monotonic_ns,
            name="started_monotonic_ns",
        ),
        started_wall_utc_ns=_exact_nonnegative_int(
            attempt.started_wall_utc_ns,
            name="started_wall_utc_ns",
        ),
    )


def _snapshot_cut(cut: TrustedChronologyCut) -> TrustedChronologyCut:
    """Detach caller-owned cut fields before durable/read-side validation."""

    if type(cut) is not TrustedChronologyCut:
        raise TypeError("cut must be exact TrustedChronologyCut")
    if type(cut.scope) is not ChronologyScope:
        raise TrustedChronologyError("cut scope must be exact ChronologyScope")

    release_id = cut.release_artifact_id
    if release_id is not None:
        release_id = _uuid_text(release_id, name="release_artifact_id")
    release_sha = cut.release_artifact_sha256
    if release_sha is not None:
        release_sha = _digest(release_sha, name="release_artifact_sha256")
    runtime_host_id = cut.runtime_host_id
    if runtime_host_id is not None:
        runtime_host_id = _token(runtime_host_id, name="runtime_host_id")
    runtime_occurrence_id = cut.runtime_occurrence_id
    if runtime_occurrence_id is not None:
        runtime_occurrence_id = _uuid_text(
            runtime_occurrence_id,
            name="runtime_occurrence_id",
        )
    runtime_occurrence_version = cut.runtime_occurrence_version
    if runtime_occurrence_version is not None:
        runtime_occurrence_version = _exact_positive_int(
            runtime_occurrence_version,
            name="runtime_occurrence_version",
        )
    runtime_occurrence_sequence = cut.runtime_occurrence_journal_sequence
    if runtime_occurrence_sequence is not None:
        runtime_occurrence_sequence = _exact_positive_int(
            runtime_occurrence_sequence,
            name="runtime_occurrence_journal_sequence",
        )

    covered_utc, covered = _instant(cut.covered_utc, name="covered_utc")
    lower_utc, lower = _instant(cut.utc_lower_bound, name="utc_lower_bound")
    upper_utc, upper = _instant(cut.utc_upper_bound, name="utc_upper_bound")
    if covered_utc != lower_utc or covered != lower or upper < lower:
        raise TrustedChronologyError("trusted chronology cut UTC bounds are invalid")

    if cut.scope is ChronologyScope.SOURCE_QUALIFICATION:
        if any(
            value is not None
            for value in (
                release_id,
                release_sha,
                runtime_host_id,
                runtime_occurrence_id,
                runtime_occurrence_version,
                runtime_occurrence_sequence,
            )
        ):
            raise TrustedChronologyError(
                "source chronology cut cannot carry release/runtime identity"
            )
    elif any(
        value is None
        for value in (
            release_id,
            release_sha,
            runtime_host_id,
            runtime_occurrence_id,
            runtime_occurrence_version,
            runtime_occurrence_sequence,
        )
    ):
        raise TrustedChronologyError(
            "release chronology cut must carry complete release/runtime identity"
        )

    return TrustedChronologyCut(
        cut_id=_uuid_text(cut.cut_id, name="cut_id"),
        challenge_digest=_digest(cut.challenge_digest, name="challenge_digest"),
        scope=cut.scope,
        source_sha=_git_sha(cut.source_sha),
        store_identity_digest=_digest(
            cut.store_identity_digest,
            name="store_identity_digest",
        ),
        owner_scope=_token(cut.owner_scope, name="owner_scope"),
        owner_id=_token(cut.owner_id, name="owner_id"),
        owner_epoch=_exact_positive_int(cut.owner_epoch, name="owner_epoch"),
        clock_incident_generation=_exact_nonnegative_int(
            cut.clock_incident_generation,
            name="clock_incident_generation",
        ),
        runtime_environment=_runtime_environment(cut.runtime_environment),
        release_artifact_id=release_id,
        release_artifact_sha256=release_sha,
        runtime_host_id=runtime_host_id,
        runtime_occurrence_id=runtime_occurrence_id,
        runtime_occurrence_version=runtime_occurrence_version,
        runtime_occurrence_journal_sequence=runtime_occurrence_sequence,
        covered_utc=covered_utc,
        utc_lower_bound=lower_utc,
        utc_upper_bound=upper_utc,
        external_authority_id=_token(
            cut.external_authority_id,
            name="external_authority_id",
        ),
        external_protocol_id=_token(
            cut.external_protocol_id,
            name="external_protocol_id",
        ),
        external_protocol_version=_token(
            cut.external_protocol_version,
            name="external_protocol_version",
        ),
        external_response_id=_token(
            cut.external_response_id,
            name="external_response_id",
        ),
        measurement_artifact_id=_uuid_text(
            cut.measurement_artifact_id,
            name="measurement_artifact_id",
        ),
        measurement_sha256=_digest(
            cut.measurement_sha256,
            name="measurement_sha256",
        ),
        measurement_requirement_id=_token(
            cut.measurement_requirement_id,
            name="measurement_requirement_id",
        ),
        accepted_attestation_id=_uuid_text(
            cut.accepted_attestation_id,
            name="accepted_attestation_id",
        ),
        accepted_attestation_digest=_digest(
            cut.accepted_attestation_digest,
            name="accepted_attestation_digest",
        ),
        accepted_policy_id=_digest(
            cut.accepted_policy_id,
            name="accepted_policy_id",
        ),
        accepted_trust_root_id=_digest(
            cut.accepted_trust_root_id,
            name="accepted_trust_root_id",
        ),
        accepted_journal_sequence=_exact_positive_int(
            cut.accepted_journal_sequence,
            name="accepted_journal_sequence",
        ),
        cut_digest=_digest(cut.cut_digest, name="cut_digest"),
    )


def _cut_digest(payload: dict[str, object]) -> str:
    material = dict(payload)
    material.pop("cut_digest", None)
    return "sha256:" + sha256(_canonical_json_bytes(material)).hexdigest()


def _limits_payload() -> dict[str, str]:
    return {
        "max_external_local_offset_us": str(_MAX_EXTERNAL_LOCAL_OFFSET_US),
        "max_external_uncertainty_us": str(_MAX_EXTERNAL_UNCERTAINTY_US),
        "max_local_rate_divergence_ns": str(_MAX_LOCAL_RATE_DIVERGENCE_NS),
        "max_request_elapsed_ns": str(_MAX_REQUEST_ELAPSED_NS),
    }


def _validate_limits(value: object) -> None:
    if type(value) is not dict or set(value) != set(_limits_payload()):
        raise TrustedChronologyError("trusted chronology protocol limits are malformed")
    if value != _limits_payload():
        raise TrustedChronologyError(
            "trusted chronology protocol limits differ from protocol-v1 policy"
        )


def _prepared_payload(
    *,
    challenge: ChronologyChallenge,
    started_monotonic_ns: int,
    started_wall_utc_ns: int,
) -> dict[str, object]:
    return {
        "challenge": challenge.canonical_payload(),
        "challenge_digest": challenge.challenge_digest,
        "limits": _limits_payload(),
        "schema_version": _SCHEMA_VERSION,
        "started_monotonic_ns": str(started_monotonic_ns),
        "started_wall_utc_ns": str(started_wall_utc_ns),
    }


def _validate_prepared_event(
    event: object,
) -> tuple[ChronologyChallenge, int, int, int]:
    if type(event) is not dict:
        raise TrustedChronologyError("trusted chronology prepared event is non-canonical")
    if (
        event.get("event_type") != _PREPARED_EVENT
        or event.get("aggregate_type") != _AGGREGATE_TYPE
        or event.get("aggregate_version") != 1
    ):
        raise TrustedChronologyError("trusted chronology prepared event identity is invalid")
    payload = event.get("payload")
    if type(payload) is not dict or payload_digest(payload) != event.get("payload_hash"):
        raise TrustedChronologyError("trusted chronology prepared payload is invalid")
    if set(payload) != {
        "challenge",
        "challenge_digest",
        "limits",
        "schema_version",
        "started_monotonic_ns",
        "started_wall_utc_ns",
    } or payload.get("schema_version") != _SCHEMA_VERSION:
        raise TrustedChronologyError("trusted chronology prepared payload schema is invalid")
    challenge = _challenge_from_payload(
        payload.get("challenge"),
        expected_digest=payload.get("challenge_digest"),
    )
    if event.get("aggregate_id") != challenge.challenge_digest:
        raise TrustedChronologyError("trusted chronology aggregate/challenge binding is invalid")
    if event.get("event_id") != _prepared_event_id(challenge.challenge_digest):
        raise TrustedChronologyError("trusted chronology prepared event id is invalid")
    _validate_limits(payload.get("limits"))
    started_mono = _decimal_int(
        payload.get("started_monotonic_ns"),
        name="started_monotonic_ns",
    )
    started_wall = _decimal_int(
        payload.get("started_wall_utc_ns"),
        name="started_wall_utc_ns",
    )
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise TrustedChronologyError("trusted chronology prepared sequence is invalid")
    if sequence != challenge.journal_sequence + 1:
        raise TrustedChronologyError(
            "trusted chronology prepared event is not the sole post-challenge write"
        )
    return challenge, started_mono, started_wall, sequence


def _require_runtime_binding(
    *,
    store: JournalStore,
    challenge: ChronologyChallenge,
    runtime: ProductionHostRuntime | None,
) -> None:
    if challenge.scope is ChronologyScope.SOURCE_QUALIFICATION:
        if runtime is not None:
            raise PermissionError(
                "SOURCE_QUALIFICATION accepted cut cannot carry production runtime"
            )
        if any(
            value is not None
            for value in (
                challenge.runtime_host_id,
                challenge.runtime_occurrence_id,
                challenge.runtime_occurrence_version,
                challenge.runtime_occurrence_journal_sequence,
            )
        ):
            raise TrustedChronologyError(
                "SOURCE_QUALIFICATION durable challenge has runtime occurrence"
            )
        return

    if type(runtime) is not ProductionHostRuntime:
        raise TypeError("RELEASE_RUNTIME requires exact ProductionHostRuntime")
    selected_identity = _selected_store_identity(store)
    runtime_identity = require_exact_journal_store_authority(
        runtime.journal,
        subject="trusted chronology production runtime JournalStore",
    )
    if runtime_identity != selected_identity:
        raise PermissionError(
            "production runtime does not share trusted chronology JournalStore"
        )
    occurrence = runtime.runtime_occurrence
    if type(occurrence) is not ProductionHostRuntimeOccurrence:
        raise TypeError("production runtime occurrence is not canonical")
    occurrence = require_current_production_host_runtime_occurrence(
        journal=store,
        occurrence=occurrence,
    )
    if (
        occurrence.host_id != challenge.runtime_host_id
        or occurrence.runtime_occurrence_id != challenge.runtime_occurrence_id
        or occurrence.aggregate_version != challenge.runtime_occurrence_version
        or occurrence.journal_sequence
        != challenge.runtime_occurrence_journal_sequence
    ):
        raise PermissionError(
            "trusted chronology production runtime occurrence changed"
        )
    if occurrence.environment != challenge.runtime_environment:
        raise PermissionError(
            "trusted chronology production runtime environment changed"
        )
    if occurrence.account_id != _owner_account(challenge.owner_scope):
        raise PermissionError(
            "trusted chronology production runtime account changed"
        )


def prepare_durable_chronology_challenge(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    source_sha: str,
    scope: ChronologyScope,
    release_artifact_id: str | None = None,
    release_artifact_sha256: str | None = None,
    runtime: ProductionHostRuntime | None = None,
) -> DurableChronologyAttempt:
    """Persist one canonical external-time challenge before it can be answered."""

    _selected_store_identity(store)
    recovery = _require_exact_recovery(recovery)
    with journal_sender_gate(store):
        challenge = prepare_chronology_challenge(
            store=store,
            recovery=recovery,
            source_sha=source_sha,
            scope=scope,
            release_artifact_id=release_artifact_id,
            release_artifact_sha256=release_artifact_sha256,
            runtime=runtime,
        )
        started_monotonic_ns = time.monotonic_ns()
        started_wall_utc_ns = time.time_ns()
        payload = _prepared_payload(
            challenge=challenge,
            started_monotonic_ns=started_monotonic_ns,
            started_wall_utc_ns=started_wall_utc_ns,
        )
        event_id = _prepared_event_id(challenge.challenge_digest)
        event = {
            "event_id": event_id,
            "event_type": _PREPARED_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": challenge.challenge_digest,
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _storage_timestamp(),
        }
        JournalStore.commit_command(
            store,
            command_id=event_id,
            actor="trusted-chronology",
            environment=challenge.runtime_environment,
            idempotency_key=event_id,
            request={"challenge_digest": challenge.challenge_digest},
            result={"status": "PREPARED"},
            state_version=1,
            events=[(event, None)],
            expected_journal_sequence=challenge.journal_sequence,
        )
        events = _load_events(store, challenge.challenge_digest)
        if len(events) != 1:
            raise TrustedChronologyError("trusted chronology prepare did not round-trip")
        durable, durable_mono, durable_wall, sequence = _validate_prepared_event(
            events[0]
        )
        if (
            durable != challenge
            or durable_mono != started_monotonic_ns
            or durable_wall != started_wall_utc_ns
        ):
            raise TrustedChronologyError(
                "trusted chronology prepared challenge changed during persistence"
            )
        return DurableChronologyAttempt(
            challenge=challenge,
            prepared_event_id=event_id,
            prepared_journal_sequence=sequence,
            started_monotonic_ns=started_monotonic_ns,
            started_wall_utc_ns=started_wall_utc_ns,
        )


def chronology_measurement_requirement(
    attempt: DurableChronologyAttempt,
    measurement_bytes: bytes,
) -> str:
    """Derive the additional signed subject requirement for one exact response."""

    attempt = _snapshot_attempt(attempt)
    if type(measurement_bytes) is not bytes:
        raise TypeError("measurement_bytes must be exact bytes")
    transcript = parse_challenge_bound_measurement(
        measurement_bytes,
        challenge=attempt.challenge,
    )
    subject = {
        "challenge": attempt.challenge.canonical_payload(),
        "challenge_digest": attempt.challenge.challenge_digest,
        "external_authority_id": transcript.authority_id,
        "external_protocol_id": transcript.protocol_id,
        "external_protocol_version": transcript.protocol_version,
        "external_response_id": transcript.response_id,
        "limits": _limits_payload(),
        "measurement_sha256": "sha256:" + sha256(measurement_bytes).hexdigest(),
        "utc_lower_bound": transcript.utc_lower_bound,
        "utc_upper_bound": transcript.utc_upper_bound,
    }
    return _DYNAMIC_REQUIREMENT_PREFIX + sha256(
        _canonical_json_bytes(subject)
    ).hexdigest()


def _require_open_attempt(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    attempt: DurableChronologyAttempt,
    runtime: ProductionHostRuntime | None,
) -> int:
    attempt = _snapshot_attempt(attempt)
    recovery = _require_exact_recovery(recovery)
    identity = _selected_store_identity(store)
    challenge = attempt.challenge
    if type(challenge) is not ChronologyChallenge:
        raise TypeError("attempt challenge must be exact ChronologyChallenge")
    if journal_store_identity_digest(identity) != challenge.store_identity_digest:
        raise PermissionError("trusted chronology JournalStore identity changed")
    if recovery.durable_owner_store_identity != identity:
        raise PermissionError("trusted chronology recovery JournalStore changed")
    if recovery.clock_trusted is not True:
        raise PermissionError("trusted chronology local clock health is not trusted")
    owner = recovery.owner
    if (
        owner is None
        or owner.owner_id != challenge.owner_id
        or owner.epoch != challenge.owner_epoch
        or recovery.owner_scope != challenge.owner_scope
    ):
        raise PermissionError("trusted chronology recovery owner changed")
    if recovery.clock_incident_generation != challenge.clock_incident_generation:
        raise PermissionError("trusted chronology clock incident generation changed")
    _require_runtime_binding(
        store=store,
        challenge=challenge,
        runtime=runtime,
    )

    events = _load_events(store, challenge.challenge_digest)
    if len(events) != 1:
        raise PermissionError(
            "trusted chronology attempt is missing, consumed, or structurally invalid"
        )
    durable, started_mono, started_wall, sequence = _validate_prepared_event(events[0])
    if (
        durable != challenge
        or events[0].get("event_id") != attempt.prepared_event_id
        or sequence != attempt.prepared_journal_sequence
        or started_mono != attempt.started_monotonic_ns
        or started_wall != attempt.started_wall_utc_ns
    ):
        raise PermissionError(
            "trusted chronology attempt differs from durable prepared authority"
        )
    if _current_sequence(store) != sequence:
        raise PermissionError(
            "durable journal advanced after trusted chronology challenge preparation"
        )
    return sequence


def _check_measurement_limits(
    *,
    attempt: DurableChronologyAttempt,
    transcript: ChronologyMeasurementTranscript,
    finished_monotonic_ns: int,
    finished_wall_utc_ns: int,
) -> dict[str, int]:
    start_mono = _exact_nonnegative_int(
        attempt.started_monotonic_ns,
        name="started_monotonic_ns",
    )
    start_wall = _exact_nonnegative_int(
        attempt.started_wall_utc_ns,
        name="started_wall_utc_ns",
    )
    finish_mono = _exact_nonnegative_int(
        finished_monotonic_ns,
        name="finished_monotonic_ns",
    )
    finish_wall = _exact_nonnegative_int(
        finished_wall_utc_ns,
        name="finished_wall_utc_ns",
    )
    if finish_mono < start_mono:
        raise TrustedChronologyError("local monotonic clock moved backward")
    monotonic_elapsed = finish_mono - start_mono
    if monotonic_elapsed > _MAX_REQUEST_ELAPSED_NS:
        raise TrustedChronologyError("chronology response exceeded freshness limit")
    wall_elapsed = finish_wall - start_wall
    if wall_elapsed < 0:
        raise TrustedChronologyError("local wall clock moved backward")
    local_rate_divergence = abs(wall_elapsed - monotonic_elapsed)
    if local_rate_divergence > _MAX_LOCAL_RATE_DIVERGENCE_NS:
        raise TrustedChronologyError(
            "local wall clock diverged from monotonic observation"
        )

    _lower_text, lower = _instant(
        transcript.utc_lower_bound,
        name="utc_lower_bound",
    )
    _upper_text, upper = _instant(
        transcript.utc_upper_bound,
        name="utc_upper_bound",
    )
    lower_us = _datetime_us(lower)
    upper_us = _datetime_us(upper)
    uncertainty_us = upper_us - lower_us
    if uncertainty_us < 0 or uncertainty_us > _MAX_EXTERNAL_UNCERTAINTY_US:
        raise TrustedChronologyError(
            "external chronology uncertainty exceeds protocol limit"
        )
    finish_wall_us = finish_wall // 1_000
    if finish_wall_us < lower_us:
        external_local_offset_us = lower_us - finish_wall_us
    elif finish_wall_us > upper_us:
        external_local_offset_us = finish_wall_us - upper_us
    else:
        external_local_offset_us = 0
    if external_local_offset_us > _MAX_EXTERNAL_LOCAL_OFFSET_US:
        raise TrustedChronologyError(
            "external chronology disagrees with local clock beyond protocol limit"
        )
    return {
        "external_local_offset_us": external_local_offset_us,
        "external_uncertainty_us": uncertainty_us,
        "local_rate_divergence_ns": local_rate_divergence,
        "request_elapsed_ns": monotonic_elapsed,
    }


def _measurement_evidence_ref(
    accepted: AcceptedQualificationAttestation,
    *,
    measurement_sha256: str,
) -> EvidenceArtifactRef:
    if type(accepted.evidence_refs) is not tuple:
        raise TrustedChronologyError("accepted chronology evidence refs are non-canonical")
    matches = tuple(
        ref
        for ref in accepted.evidence_refs
        if type(ref) is EvidenceArtifactRef
        and ref.evidence_kind == _MEASUREMENT_KIND
        and ref.media_type == _MEASUREMENT_MEDIA_TYPE
        and ref.sha256 == measurement_sha256
        and ref.source_sha == accepted.source_sha
    )
    if len(matches) != 1:
        raise TrustedChronologyError(
            "accepted chronology attestation must bind exactly one raw measurement"
        )
    return matches[0]


def _require_accepted_binding(
    accepted: AcceptedQualificationAttestation,
    *,
    challenge: ChronologyChallenge,
    measurement_sha256: str,
    dynamic_requirement: str,
    measurement_upper_bound: str,
) -> EvidenceArtifactRef:
    if type(accepted) is not AcceptedQualificationAttestation:
        raise TrustedChronologyError(
            "canonical chronology verifier returned non-canonical acceptance"
        )
    if type(accepted.requirement_ids) is not tuple:
        raise TrustedChronologyError("accepted chronology requirement ids are non-canonical")
    if type(accepted.unresolved_limits) is not tuple:
        raise TrustedChronologyError("accepted chronology unresolved limits are non-canonical")
    if accepted.result != "PASS" or accepted.unresolved_limits:
        raise TrustedChronologyError(
            "trusted chronology requires PASS with no unresolved limits"
        )
    _upper_text, measurement_completed = _instant(
        measurement_upper_bound,
        name="measurement_upper_bound",
    )
    _completed_text, completed_at = _instant(
        accepted.completed_at,
        name="accepted completed_at",
    )
    _signed_text, signed_at = _instant(
        accepted.signed_at,
        name="accepted signed_at",
    )
    if completed_at < measurement_completed or signed_at < measurement_completed:
        raise TrustedChronologyError(
            "accepted chronology receipt predates authorized measurement"
        )
    if (
        accepted.source_sha != challenge.source_sha
        or accepted.domain != _QUALIFICATION_DOMAIN
        or accepted.gate != _QUALIFICATION_GATE
        or accepted.package_id != _QUALIFICATION_PACKAGE
        or accepted.protocol_id != _QUALIFICATION_PROTOCOL
        or accepted.protocol_version != _QUALIFICATION_PROTOCOL_VERSION
        or accepted.requirement_id != _QUALIFICATION_REQUIREMENT
        or _QUALIFICATION_REQUIREMENT not in accepted.requirement_ids
        or dynamic_requirement not in accepted.requirement_ids
    ):
        raise TrustedChronologyError(
            "accepted chronology attestation scope/subject differs from request"
        )
    if challenge.scope is ChronologyScope.SOURCE_QUALIFICATION:
        if (
            accepted.release_artifact_id is not None
            or accepted.release_artifact_sha256 is not None
        ):
            raise TrustedChronologyError(
                "SOURCE_QUALIFICATION cut cannot consume release-scoped attestation"
            )
    elif (
        accepted.release_artifact_id != challenge.release_artifact_id
        or accepted.release_artifact_sha256 != challenge.release_artifact_sha256
    ):
        raise TrustedChronologyError(
            "RELEASE_RUNTIME attestation release identity mismatch"
        )
    return _measurement_evidence_ref(
        accepted,
        measurement_sha256=measurement_sha256,
    )


def _reject_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TrustedChronologyError(
                "persisted signed chronology receipt contains duplicate JSON key"
            )
        result[key] = value
    return result


def _strict_json_value(text: object, *, name: str) -> object:
    value = _token(text, name=name)
    raw = value.encode("utf-8")
    if len(raw) > _MAX_SIGNED_RECEIPT_BYTES:
        raise TrustedChronologyError(f"{name} exceeds retained receipt byte budget")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TrustedChronologyError(f"{name} is not valid canonical JSON") from error
    if _canonical_json_bytes(parsed) != raw:
        raise TrustedChronologyError(f"{name} is not canonical JSON")
    return parsed


def _signed_receipt_json(
    accepted: AcceptedQualificationAttestation,
) -> str:
    attestation = _strict_json_value(
        accepted.attestation_json,
        name="accepted attestation_json",
    )
    signature = _token(accepted.signature_b64, name="accepted signature_b64")
    raw = _canonical_json_bytes(
        {
            "attestation": attestation,
            "signature_b64": signature,
        }
    )
    if len(raw) > _MAX_SIGNED_RECEIPT_BYTES:
        raise TrustedChronologyError("signed chronology receipt exceeds byte budget")
    return raw.decode("utf-8")


def _verify_receipt(
    *,
    receipt: SignedQualificationAttestation,
    challenge: ChronologyChallenge,
    measurement_sha256: str,
    dynamic_requirement: str,
    measurement_upper_bound: str,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
) -> tuple[AcceptedQualificationAttestation, EvidenceArtifactRef]:
    accepted = verify_canonical_qualification_attestation(
        receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=challenge.source_sha,
        expected_domain=_QUALIFICATION_DOMAIN,
        expected_gate=_QUALIFICATION_GATE,
        expected_package_id=_QUALIFICATION_PACKAGE,
        expected_protocol_id=_QUALIFICATION_PROTOCOL,
        expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
        expected_requirement_id=_QUALIFICATION_REQUIREMENT,
        expected_release_artifact_id=challenge.release_artifact_id,
        expected_release_artifact_sha256=challenge.release_artifact_sha256,
    )
    ref = _require_accepted_binding(
        accepted,
        challenge=challenge,
        measurement_sha256=measurement_sha256,
        dynamic_requirement=dynamic_requirement,
        measurement_upper_bound=measurement_upper_bound,
    )
    return accepted, ref


def _accepted_payload(
    *,
    attempt: DurableChronologyAttempt,
    transcript: ChronologyMeasurementTranscript,
    measurement_ref: EvidenceArtifactRef,
    measurement_sha256: str,
    dynamic_requirement: str,
    accepted: AcceptedQualificationAttestation,
    metrics: dict[str, int],
) -> dict[str, object]:
    challenge = attempt.challenge
    cut_id = _cut_id(challenge.challenge_digest, accepted.attestation_id)
    payload: dict[str, object] = {
        "accepted_attestation_digest": accepted.attestation_digest,
        "accepted_attestation_id": accepted.attestation_id,
        "accepted_policy_id": accepted.policy_id,
        "accepted_trust_root_id": accepted.trust_root_id,
        "challenge_digest": challenge.challenge_digest,
        "clock_incident_generation": str(challenge.clock_incident_generation),
        "covered_utc": transcript.conservative_covered_utc,
        "cut_id": cut_id,
        "external_authority_id": transcript.authority_id,
        "external_local_offset_us": str(metrics["external_local_offset_us"]),
        "external_protocol_id": transcript.protocol_id,
        "external_protocol_version": transcript.protocol_version,
        "external_response_id": transcript.response_id,
        "external_uncertainty_us": str(metrics["external_uncertainty_us"]),
        "limits": _limits_payload(),
        "local_rate_divergence_ns": str(metrics["local_rate_divergence_ns"]),
        "measurement_artifact_id": measurement_ref.artifact_id,
        "measurement_requirement_id": dynamic_requirement,
        "measurement_sha256": measurement_sha256,
        "owner_epoch": str(challenge.owner_epoch),
        "owner_id": challenge.owner_id,
        "owner_scope": challenge.owner_scope,
        "release_artifact_id": challenge.release_artifact_id,
        "release_artifact_sha256": challenge.release_artifact_sha256,
        "request_elapsed_ns": str(metrics["request_elapsed_ns"]),
        "runtime_environment": challenge.runtime_environment,
        "runtime_host_id": challenge.runtime_host_id,
        "runtime_occurrence_id": challenge.runtime_occurrence_id,
        "runtime_occurrence_journal_sequence": (
            None
            if challenge.runtime_occurrence_journal_sequence is None
            else str(challenge.runtime_occurrence_journal_sequence)
        ),
        "runtime_occurrence_version": (
            None
            if challenge.runtime_occurrence_version is None
            else str(challenge.runtime_occurrence_version)
        ),
        "schema_version": _SCHEMA_VERSION,
        "scope": challenge.scope.value,
        "signed_receipt_json": _signed_receipt_json(accepted),
        "source_sha": challenge.source_sha,
        "store_identity_digest": challenge.store_identity_digest,
        "utc_lower_bound": transcript.utc_lower_bound,
        "utc_upper_bound": transcript.utc_upper_bound,
    }
    payload["cut_digest"] = _cut_digest(payload)
    return payload


def _validate_accepted_event(
    event: object,
) -> tuple[dict[str, object], int]:
    if type(event) is not dict:
        raise TrustedChronologyError("trusted chronology accepted event is non-canonical")
    if (
        event.get("event_type") != _ACCEPTED_EVENT
        or event.get("aggregate_type") != _AGGREGATE_TYPE
        or event.get("aggregate_version") != 2
    ):
        raise TrustedChronologyError("trusted chronology accepted event identity is invalid")
    payload = event.get("payload")
    if type(payload) is not dict or payload_digest(payload) != event.get("payload_hash"):
        raise TrustedChronologyError("trusted chronology accepted payload is invalid")
    expected = {
        "accepted_attestation_digest",
        "accepted_attestation_id",
        "accepted_policy_id",
        "accepted_trust_root_id",
        "challenge_digest",
        "clock_incident_generation",
        "covered_utc",
        "cut_digest",
        "cut_id",
        "external_authority_id",
        "external_local_offset_us",
        "external_protocol_id",
        "external_protocol_version",
        "external_response_id",
        "external_uncertainty_us",
        "limits",
        "local_rate_divergence_ns",
        "measurement_artifact_id",
        "measurement_requirement_id",
        "measurement_sha256",
        "owner_epoch",
        "owner_id",
        "owner_scope",
        "release_artifact_id",
        "release_artifact_sha256",
        "request_elapsed_ns",
        "runtime_environment",
        "runtime_host_id",
        "runtime_occurrence_id",
        "runtime_occurrence_journal_sequence",
        "runtime_occurrence_version",
        "schema_version",
        "scope",
        "signed_receipt_json",
        "source_sha",
        "store_identity_digest",
        "utc_lower_bound",
        "utc_upper_bound",
    }
    if set(payload) != expected or payload.get("schema_version") != _SCHEMA_VERSION:
        raise TrustedChronologyError("trusted chronology accepted payload schema is invalid")

    challenge_digest = _digest(
        payload.get("challenge_digest"),
        name="challenge_digest",
    )
    if event.get("aggregate_id") != challenge_digest:
        raise TrustedChronologyError("trusted chronology cut aggregate binding is invalid")
    attestation_id = _uuid_text(
        payload.get("accepted_attestation_id"),
        name="accepted_attestation_id",
    )
    cut_id = _uuid_text(payload.get("cut_id"), name="cut_id")
    if cut_id != _cut_id(challenge_digest, attestation_id):
        raise TrustedChronologyError("trusted chronology cut identity is invalid")
    if event.get("event_id") != cut_id:
        raise TrustedChronologyError("trusted chronology cut event id is invalid")
    if payload.get("cut_digest") != _cut_digest(payload):
        raise TrustedChronologyError("trusted chronology cut digest mismatch")

    try:
        scope = ChronologyScope(payload.get("scope"))
    except (ValueError, TypeError) as error:
        raise TrustedChronologyError("trusted chronology cut scope is invalid") from error
    _git_sha(payload.get("source_sha"))
    _digest(payload.get("store_identity_digest"), name="store_identity_digest")
    owner_scope = _token(payload.get("owner_scope"), name="owner_scope")
    _token(payload.get("owner_id"), name="owner_id")
    _decimal_int(payload.get("owner_epoch"), name="owner_epoch", positive=True)
    _decimal_int(
        payload.get("clock_incident_generation"),
        name="clock_incident_generation",
    )
    environment = _runtime_environment(payload.get("runtime_environment"))
    if owner_scope.partition(":")[0] != environment:
        raise TrustedChronologyError("trusted chronology cut owner/environment mismatch")

    release_id = payload.get("release_artifact_id")
    release_sha = payload.get("release_artifact_sha256")
    runtime_values = (
        payload.get("runtime_host_id"),
        payload.get("runtime_occurrence_id"),
        payload.get("runtime_occurrence_version"),
        payload.get("runtime_occurrence_journal_sequence"),
    )
    if scope is ChronologyScope.SOURCE_QUALIFICATION:
        if release_id is not None or release_sha is not None:
            raise TrustedChronologyError(
                "source chronology cut cannot carry release identity"
            )
        if any(value is not None for value in runtime_values):
            raise TrustedChronologyError(
                "source chronology cut cannot carry runtime occurrence"
            )
    else:
        _uuid_text(release_id, name="release_artifact_id")
        _digest(release_sha, name="release_artifact_sha256")
        _token(payload.get("runtime_host_id"), name="runtime_host_id")
        _uuid_text(payload.get("runtime_occurrence_id"), name="runtime_occurrence_id")
        _decimal_int(
            payload.get("runtime_occurrence_version"),
            name="runtime_occurrence_version",
            positive=True,
        )
        _decimal_int(
            payload.get("runtime_occurrence_journal_sequence"),
            name="runtime_occurrence_journal_sequence",
            positive=True,
        )

    covered_text, covered = _instant(payload.get("covered_utc"), name="covered_utc")
    lower_text, lower = _instant(
        payload.get("utc_lower_bound"),
        name="utc_lower_bound",
    )
    _upper_text, upper = _instant(
        payload.get("utc_upper_bound"),
        name="utc_upper_bound",
    )
    if covered_text != lower_text or covered != lower:
        raise TrustedChronologyError(
            "trusted chronology cut must use conservative lower UTC bound"
        )
    if upper < lower:
        raise TrustedChronologyError("trusted chronology cut UTC interval is reversed")

    for name in (
        "external_authority_id",
        "external_protocol_id",
        "external_protocol_version",
        "external_response_id",
        "measurement_requirement_id",
    ):
        _token(payload.get(name), name=name)
    _uuid_text(payload.get("measurement_artifact_id"), name="measurement_artifact_id")
    _digest(payload.get("measurement_sha256"), name="measurement_sha256")
    _digest(
        payload.get("accepted_attestation_digest"),
        name="accepted_attestation_digest",
    )
    _digest(payload.get("accepted_policy_id"), name="accepted_policy_id")
    _digest(
        payload.get("accepted_trust_root_id"),
        name="accepted_trust_root_id",
    )
    _validate_limits(payload.get("limits"))
    for name, limit in (
        ("request_elapsed_ns", _MAX_REQUEST_ELAPSED_NS),
        ("local_rate_divergence_ns", _MAX_LOCAL_RATE_DIVERGENCE_NS),
        ("external_uncertainty_us", _MAX_EXTERNAL_UNCERTAINTY_US),
        ("external_local_offset_us", _MAX_EXTERNAL_LOCAL_OFFSET_US),
    ):
        observed = _decimal_int(payload.get(name), name=name)
        if observed > limit:
            raise TrustedChronologyError(
                f"trusted chronology cut {name} exceeds protocol-v1 policy"
            )
    _strict_json_value(
        payload.get("signed_receipt_json"),
        name="signed_receipt_json",
    )
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise TrustedChronologyError("trusted chronology accepted sequence is invalid")
    return payload, sequence


def _cut_from_event(event: object) -> TrustedChronologyCut:
    payload, sequence = _validate_accepted_event(event)
    scope = ChronologyScope(payload["scope"])
    runtime_version = _optional_positive_decimal(
        payload["runtime_occurrence_version"],
        name="runtime_occurrence_version",
    )
    runtime_sequence = _optional_positive_decimal(
        payload["runtime_occurrence_journal_sequence"],
        name="runtime_occurrence_journal_sequence",
    )
    return TrustedChronologyCut(
        cut_id=_uuid_text(payload["cut_id"], name="cut_id"),
        challenge_digest=_digest(payload["challenge_digest"], name="challenge_digest"),
        scope=scope,
        source_sha=_git_sha(payload["source_sha"]),
        store_identity_digest=_digest(
            payload["store_identity_digest"],
            name="store_identity_digest",
        ),
        owner_scope=_token(payload["owner_scope"], name="owner_scope"),
        owner_id=_token(payload["owner_id"], name="owner_id"),
        owner_epoch=_decimal_int(payload["owner_epoch"], name="owner_epoch", positive=True),
        clock_incident_generation=_decimal_int(
            payload["clock_incident_generation"],
            name="clock_incident_generation",
        ),
        runtime_environment=_runtime_environment(payload["runtime_environment"]),
        release_artifact_id=payload["release_artifact_id"],
        release_artifact_sha256=payload["release_artifact_sha256"],
        runtime_host_id=payload["runtime_host_id"],
        runtime_occurrence_id=payload["runtime_occurrence_id"],
        runtime_occurrence_version=runtime_version,
        runtime_occurrence_journal_sequence=runtime_sequence,
        covered_utc=_token(payload["covered_utc"], name="covered_utc"),
        utc_lower_bound=_token(payload["utc_lower_bound"], name="utc_lower_bound"),
        utc_upper_bound=_token(payload["utc_upper_bound"], name="utc_upper_bound"),
        external_authority_id=_token(
            payload["external_authority_id"],
            name="external_authority_id",
        ),
        external_protocol_id=_token(
            payload["external_protocol_id"],
            name="external_protocol_id",
        ),
        external_protocol_version=_token(
            payload["external_protocol_version"],
            name="external_protocol_version",
        ),
        external_response_id=_token(
            payload["external_response_id"],
            name="external_response_id",
        ),
        measurement_artifact_id=_uuid_text(
            payload["measurement_artifact_id"],
            name="measurement_artifact_id",
        ),
        measurement_sha256=_digest(
            payload["measurement_sha256"],
            name="measurement_sha256",
        ),
        measurement_requirement_id=_token(
            payload["measurement_requirement_id"],
            name="measurement_requirement_id",
        ),
        accepted_attestation_id=_uuid_text(
            payload["accepted_attestation_id"],
            name="accepted_attestation_id",
        ),
        accepted_attestation_digest=_digest(
            payload["accepted_attestation_digest"],
            name="accepted_attestation_digest",
        ),
        accepted_policy_id=_digest(
            payload["accepted_policy_id"],
            name="accepted_policy_id",
        ),
        accepted_trust_root_id=_digest(
            payload["accepted_trust_root_id"],
            name="accepted_trust_root_id",
        ),
        accepted_journal_sequence=sequence,
        cut_digest=_digest(payload["cut_digest"], name="cut_digest"),
    )


def _parse_persisted_receipt(payload: dict[str, object]) -> SignedQualificationAttestation:
    parsed = _strict_json_value(
        payload.get("signed_receipt_json"),
        name="signed_receipt_json",
    )
    try:
        receipt = parse_signed_qualification_attestation(parsed)
    except (TypeError, ValueError) as error:
        raise TrustedChronologyError(
            "persisted signed chronology receipt cannot be parsed"
        ) from error
    if type(receipt) is not SignedQualificationAttestation:
        raise TrustedChronologyError("persisted chronology receipt is non-canonical")
    return receipt


def accept_trusted_chronology_cut(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    attempt: DurableChronologyAttempt,
    measurement_bytes: bytes,
    receipt: SignedQualificationAttestation,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
    runtime: ProductionHostRuntime | None = None,
) -> TrustedChronologyCut:
    """Verify and atomically persist one conservative trusted UTC horizon."""

    if type(measurement_bytes) is not bytes:
        raise TypeError("measurement_bytes must be exact bytes")
    if type(receipt) is not SignedQualificationAttestation:
        raise TypeError("receipt must be exact SignedQualificationAttestation")
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")

    recovery = _require_exact_recovery(recovery)
    with journal_sender_gate(store):
        attempt = _snapshot_attempt(attempt)
        journal_cut = _require_open_attempt(
            store=store,
            recovery=recovery,
            attempt=attempt,
            runtime=runtime,
        )
        transcript = parse_challenge_bound_measurement(
            measurement_bytes,
            challenge=attempt.challenge,
        )
        metrics = _check_measurement_limits(
            attempt=attempt,
            transcript=transcript,
            finished_monotonic_ns=time.monotonic_ns(),
            finished_wall_utc_ns=time.time_ns(),
        )
        measurement_sha256 = "sha256:" + sha256(measurement_bytes).hexdigest()
        dynamic_requirement = chronology_measurement_requirement(
            attempt,
            measurement_bytes,
        )
        accepted, measurement_ref = _verify_receipt(
            receipt=receipt,
            challenge=attempt.challenge,
            measurement_sha256=measurement_sha256,
            dynamic_requirement=dynamic_requirement,
            measurement_upper_bound=transcript.utc_upper_bound,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
        )

        if (
            _require_open_attempt(
                store=store,
                recovery=recovery,
                attempt=attempt,
                runtime=runtime,
            )
            != journal_cut
        ):
            raise TrustedChronologyError(
                "trusted chronology prepared frontier changed during verification"
            )

        payload = _accepted_payload(
            attempt=attempt,
            transcript=transcript,
            measurement_ref=measurement_ref,
            measurement_sha256=measurement_sha256,
            dynamic_requirement=dynamic_requirement,
            accepted=accepted,
            metrics=metrics,
        )
        cut_id = payload["cut_id"]
        event = {
            "event_id": cut_id,
            "event_type": _ACCEPTED_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": attempt.challenge.challenge_digest,
            "aggregate_version": "2",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _storage_timestamp(),
        }
        JournalStore.commit_command(
            store,
            command_id=cut_id,
            actor="trusted-chronology",
            environment=attempt.challenge.runtime_environment,
            idempotency_key=cut_id,
            request={
                "challenge_digest": attempt.challenge.challenge_digest,
                "measurement_sha256": measurement_sha256,
                "accepted_attestation_id": accepted.attestation_id,
            },
            result={"status": "ACCEPTED", "cut_id": cut_id},
            state_version=2,
            events=[(event, None)],
            expected_journal_sequence=journal_cut,
        )
        events = _load_events(store, attempt.challenge.challenge_digest)
        if len(events) != 2:
            raise TrustedChronologyError("trusted chronology cut did not round-trip")
        durable_challenge, *_ = _validate_prepared_event(events[0])
        if durable_challenge != attempt.challenge:
            raise TrustedChronologyError(
                "trusted chronology prepared challenge changed before acceptance"
            )
        return _cut_from_event(events[1])


def _reverify_durable_acceptance(
    *,
    payload: dict[str, object],
    challenge: ChronologyChallenge,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
) -> None:
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    receipt = _parse_persisted_receipt(payload)
    measurement_sha256 = _digest(
        payload.get("measurement_sha256"),
        name="measurement_sha256",
    )
    dynamic_requirement = _token(
        payload.get("measurement_requirement_id"),
        name="measurement_requirement_id",
    )
    accepted, measurement_ref = _verify_receipt(
        receipt=receipt,
        challenge=challenge,
        measurement_sha256=measurement_sha256,
        dynamic_requirement=dynamic_requirement,
        measurement_upper_bound=_token(
            payload.get("utc_upper_bound"),
            name="utc_upper_bound",
        ),
        evidence_store=evidence_store,
        evidence_root=evidence_root,
    )
    if (
        accepted.attestation_id != payload.get("accepted_attestation_id")
        or accepted.attestation_digest != payload.get("accepted_attestation_digest")
        or accepted.policy_id != payload.get("accepted_policy_id")
        or accepted.trust_root_id != payload.get("accepted_trust_root_id")
        or measurement_ref.artifact_id != payload.get("measurement_artifact_id")
    ):
        raise TrustedChronologyError(
            "reverified signed chronology authority differs from durable cut"
        )


def require_current_trusted_chronology_cut(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    cut: TrustedChronologyCut,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
    expected_source_sha: str,
    expected_scope: ChronologyScope,
    expected_release_artifact_id: str | None = None,
    expected_release_artifact_sha256: str | None = None,
    runtime: ProductionHostRuntime | None = None,
) -> TrustedChronologyCut:
    """Reverify one durable cut and require owner/incident/runtime currentness."""

    cut = _snapshot_cut(cut)
    if type(expected_scope) is not ChronologyScope:
        raise TypeError("expected_scope must be exact ChronologyScope")
    expected_source_sha = _git_sha(
        expected_source_sha,
        name="expected_source_sha",
    )
    if expected_scope is ChronologyScope.SOURCE_QUALIFICATION:
        if (
            expected_release_artifact_id is not None
            or expected_release_artifact_sha256 is not None
        ):
            raise PermissionError(
                "SOURCE_QUALIFICATION expectation cannot carry release identity"
            )
    else:
        expected_release_artifact_id = _uuid_text(
            expected_release_artifact_id,
            name="expected_release_artifact_id",
        )
        expected_release_artifact_sha256 = _digest(
            expected_release_artifact_sha256,
            name="expected_release_artifact_sha256",
        )

    recovery = _require_exact_recovery(recovery)
    identity = _selected_store_identity(store)
    challenge_digest = _digest(cut.challenge_digest, name="challenge_digest")
    events = _load_events(store, challenge_digest)
    if len(events) != 2:
        raise PermissionError("trusted chronology cut durable aggregate is unavailable")
    challenge, *_prepared = _validate_prepared_event(events[0])
    durable = _cut_from_event(events[1])
    if durable != cut:
        raise PermissionError("trusted chronology cut differs from durable authority")
    if challenge.challenge_digest != durable.challenge_digest:
        raise PermissionError("trusted chronology prepared/cut challenge binding changed")
    if journal_store_identity_digest(identity) != challenge.store_identity_digest:
        raise PermissionError("trusted chronology cut JournalStore identity changed")
    if recovery.durable_owner_store_identity != identity:
        raise PermissionError("trusted chronology recovery JournalStore changed")
    if recovery.clock_trusted is not True:
        raise PermissionError("trusted chronology clock health is no longer trusted")
    owner = recovery.owner
    if (
        owner is None
        or owner.owner_id != challenge.owner_id
        or owner.epoch != challenge.owner_epoch
        or recovery.owner_scope != challenge.owner_scope
        or recovery.clock_incident_generation != challenge.clock_incident_generation
    ):
        raise PermissionError("trusted chronology owner/incident generation changed")
    _require_runtime_binding(
        store=store,
        challenge=challenge,
        runtime=runtime,
    )
    if expected_source_sha != challenge.source_sha or expected_scope is not challenge.scope:
        raise PermissionError("trusted chronology source/scope mismatch")
    if expected_scope is ChronologyScope.SOURCE_QUALIFICATION:
        if durable.release_artifact_id is not None or durable.release_artifact_sha256 is not None:
            raise PermissionError("source chronology cannot satisfy release scope")
    elif (
        expected_release_artifact_id != challenge.release_artifact_id
        or expected_release_artifact_sha256 != challenge.release_artifact_sha256
        or durable.release_artifact_id != challenge.release_artifact_id
        or durable.release_artifact_sha256 != challenge.release_artifact_sha256
    ):
        raise PermissionError("trusted chronology release identity mismatch")

    payload, _sequence = _validate_accepted_event(events[1])
    _reverify_durable_acceptance(
        payload=payload,
        challenge=challenge,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
    )
    return durable


def require_chronology_horizon(
    cut: TrustedChronologyCut,
    *claimed_instants: str,
) -> None:
    """Require terminal-evidence timestamps to be covered conservatively."""

    cut = _snapshot_cut(cut)
    _covered_text, covered = _instant(cut.covered_utc, name="covered_utc")
    for index, value in enumerate(claimed_instants):
        _text, observed = _instant(value, name=f"claimed_instant[{index}]")
        if observed > covered:
            raise PermissionError(
                "claimed terminal evidence horizon is not covered by trusted chronology"
            )
