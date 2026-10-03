"""Accepted durable trusted chronology cuts for WP-48.

This module composes the existing challenge-bound external-time precursor,
canonical signed qualification verifier, durable recovery/clock-incident
authority and the canonical production-host runtime-occurrence selector.

It intentionally does not implement NTP/PTP, create a signer/trust root, grant
provider/PAPER/LIVE authority, or establish economic edge.  A cut only proves a
conservative UTC horizon for one exact runtime occurrence after independently
signed measurement evidence has crossed the shared qualification trust boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
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
    ProductionHostRuntimeOccurrence,
    require_current_production_host_runtime_occurrence,
)
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    SignedQualificationAttestation,
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
_AGGREGATE_TYPE = "trusted_chronology"
_PREPARED_EVENT = "TrustedChronologyChallengePrepared"
_ACCEPTED_EVENT = "TrustedChronologyCutAccepted"
_QUALIFICATION_DOMAIN = "RECOVERY"
_QUALIFICATION_GATE = "TRUSTED_CHRONOLOGY"
_QUALIFICATION_PACKAGE = "WP-48"
_QUALIFICATION_PROTOCOL = "trusted-chronology-v1"
_QUALIFICATION_PROTOCOL_VERSION = "1.0.0"
_MEASUREMENT_KIND = "TRUSTED_CHRONOLOGY_MEASUREMENT"
_MEASUREMENT_MEDIA_TYPE = "application/json"

# Protocol-v1 product-owned fail-closed limits.  Callers cannot relax these.
# They are deliberately conservative enough for ordinary network timestamp
# qualification while still detecting stale replies and material local-clock
# movement.  They are part of the signed subject requirement and durable cut.
_MAX_REQUEST_ELAPSED_NS = 30_000_000_000
_MAX_LOCAL_RATE_DIVERGENCE_NS = 1_000_000_000
_MAX_EXTERNAL_UNCERTAINTY_US = 2_000_000
_MAX_EXTERNAL_LOCAL_OFFSET_US = 5_000_000

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TrustedChronologyError(ValueError):
    """Raised when chronology evidence cannot establish a durable accepted cut."""


@dataclass(frozen=True, slots=True)
class DurableChronologyAttempt:
    challenge: ChronologyChallenge
    runtime_occurrence: ProductionHostRuntimeOccurrence
    prepared_event_id: str
    prepared_journal_sequence: int
    started_monotonic_ns: int
    started_wall_utc_ns: int


@dataclass(frozen=True, slots=True)
class TrustedChronologyCut:
    cut_id: str
    scope: ChronologyScope
    source_sha: str
    store_identity_digest: str
    owner_scope: str
    owner_id: str
    owner_epoch: int
    clock_incident_generation: int
    runtime_environment: str
    runtime_occurrence_id: str
    host_id: str
    account_id: str
    release_artifact_id: str | None
    release_artifact_sha256: str | None
    covered_utc: str
    utc_lower_bound: str
    utc_upper_bound: str
    external_authority_id: str
    external_protocol_id: str
    external_protocol_version: str
    external_response_id: str
    measurement_artifact_id: str
    measurement_sha256: str
    accepted_attestation_id: str
    accepted_attestation_digest: str
    accepted_policy_id: str
    accepted_trust_root_id: str
    accepted_journal_sequence: int
    cut_digest: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "accepted_attestation_digest": self.accepted_attestation_digest,
            "accepted_attestation_id": self.accepted_attestation_id,
            "accepted_journal_sequence": str(self.accepted_journal_sequence),
            "accepted_policy_id": self.accepted_policy_id,
            "accepted_trust_root_id": self.accepted_trust_root_id,
            "account_id": self.account_id,
            "clock_incident_generation": str(self.clock_incident_generation),
            "covered_utc": self.covered_utc,
            "cut_id": self.cut_id,
            "external_authority_id": self.external_authority_id,
            "external_protocol_id": self.external_protocol_id,
            "external_protocol_version": self.external_protocol_version,
            "external_response_id": self.external_response_id,
            "host_id": self.host_id,
            "measurement_artifact_id": self.measurement_artifact_id,
            "measurement_sha256": self.measurement_sha256,
            "owner_epoch": str(self.owner_epoch),
            "owner_id": self.owner_id,
            "owner_scope": self.owner_scope,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "runtime_environment": self.runtime_environment,
            "runtime_occurrence_id": self.runtime_occurrence_id,
            "schema_version": _SCHEMA_VERSION,
            "scope": self.scope.value,
            "source_sha": self.source_sha,
            "store_identity_digest": self.store_identity_digest,
            "utc_lower_bound": self.utc_lower_bound,
            "utc_upper_bound": self.utc_upper_bound,
        }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _storage_timestamp() -> str:
    # Storage metadata only; never chronology authority.
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _exact_positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise TrustedChronologyError(f"{name} must be a positive exact int")
    return value


def _exact_nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TrustedChronologyError(f"{name} must be a non-negative exact int")
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


def _digest(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(ch not in "0123456789abcdef" for ch in value[7:])
    ):
        raise TrustedChronologyError(f"{name} must be canonical sha256:<hex>")
    return value


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


def _instant(value: object, *, name: str) -> tuple[str, datetime]:
    value = _token(value, name=name)
    if not value.endswith("Z"):
        raise TrustedChronologyError(f"{name} must be UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise TrustedChronologyError(f"{name} is not a real UTC instant") from error
    if parsed.tzinfo != timezone.utc:
        raise TrustedChronologyError(f"{name} is not UTC")
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != value:
        # Accept the precursor's second-precision form, where isoformat emits +00:00.
        if parsed.microsecond == 0:
            canonical = parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if canonical != value:
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


def _load_events(store: JournalStore, aggregate_id: str) -> tuple[dict[str, object], ...]:
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


def _snapshot_occurrence(
    *,
    store: JournalStore,
    occurrence: ProductionHostRuntimeOccurrence,
) -> ProductionHostRuntimeOccurrence:
    if type(occurrence) is not ProductionHostRuntimeOccurrence:
        raise TypeError(
            "runtime_occurrence must be exact ProductionHostRuntimeOccurrence"
        )
    return require_current_production_host_runtime_occurrence(
        journal=store,
        occurrence=occurrence,
    )


def _require_runtime_binding(
    *,
    challenge: ChronologyChallenge,
    occurrence: ProductionHostRuntimeOccurrence,
) -> None:
    if occurrence.environment != challenge.runtime_environment:
        raise TrustedChronologyError(
            "runtime occurrence environment does not match chronology owner scope"
        )


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


def _cut_digest(payload: dict[str, object]) -> str:
    material = dict(payload)
    material.pop("cut_digest", None)
    return "sha256:" + sha256(_canonical_json_bytes(material)).hexdigest()


def _prepared_payload(
    attempt: DurableChronologyAttempt,
) -> dict[str, object]:
    challenge = attempt.challenge
    occurrence = attempt.runtime_occurrence
    return {
        "account_id": occurrence.account_id,
        "challenge": challenge.canonical_payload(),
        "challenge_digest": challenge.challenge_digest,
        "clock_incident_generation": str(challenge.clock_incident_generation),
        "host_id": occurrence.host_id,
        "max_external_local_offset_us": str(_MAX_EXTERNAL_LOCAL_OFFSET_US),
        "max_external_uncertainty_us": str(_MAX_EXTERNAL_UNCERTAINTY_US),
        "max_local_rate_divergence_ns": str(_MAX_LOCAL_RATE_DIVERGENCE_NS),
        "max_request_elapsed_ns": str(_MAX_REQUEST_ELAPSED_NS),
        "owner_epoch": str(challenge.owner_epoch),
        "owner_id": challenge.owner_id,
        "owner_scope": challenge.owner_scope,
        "runtime_environment": challenge.runtime_environment,
        "runtime_occurrence_aggregate_version": str(occurrence.aggregate_version),
        "runtime_occurrence_id": occurrence.runtime_occurrence_id,
        "runtime_occurrence_journal_sequence": str(occurrence.journal_sequence),
        "schema_version": _SCHEMA_VERSION,
        "started_monotonic_ns": str(attempt.started_monotonic_ns),
        "started_wall_utc_ns": str(attempt.started_wall_utc_ns),
        "store_identity_digest": challenge.store_identity_digest,
    }


def _validate_prepared_event(
    event: object,
    *,
    attempt: DurableChronologyAttempt | None = None,
) -> dict[str, object]:
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
    expected_keys = {
        "account_id",
        "challenge",
        "challenge_digest",
        "clock_incident_generation",
        "host_id",
        "max_external_local_offset_us",
        "max_external_uncertainty_us",
        "max_local_rate_divergence_ns",
        "max_request_elapsed_ns",
        "owner_epoch",
        "owner_id",
        "owner_scope",
        "runtime_environment",
        "runtime_occurrence_aggregate_version",
        "runtime_occurrence_id",
        "runtime_occurrence_journal_sequence",
        "schema_version",
        "started_monotonic_ns",
        "started_wall_utc_ns",
        "store_identity_digest",
    }
    if set(payload) != expected_keys or payload.get("schema_version") != _SCHEMA_VERSION:
        raise TrustedChronologyError("trusted chronology prepared payload schema is invalid")
    if event.get("aggregate_id") != payload.get("challenge_digest"):
        raise TrustedChronologyError("trusted chronology aggregate/challenge binding is invalid")
    if event.get("event_id") != _prepared_event_id(str(payload.get("challenge_digest"))):
        raise TrustedChronologyError("trusted chronology prepared event id is invalid")
    if attempt is not None and payload != _prepared_payload(attempt):
        raise TrustedChronologyError("durable chronology attempt differs from prepared event")
    return payload


def prepare_durable_chronology_challenge(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    runtime_occurrence: ProductionHostRuntimeOccurrence,
    source_sha: str,
    scope: ChronologyScope,
    release_artifact_id: str | None = None,
    release_artifact_sha256: str | None = None,
) -> DurableChronologyAttempt:
    """Persist one external-time challenge for the exact current runtime occurrence."""

    _selected_store_identity(store)
    recovery = _require_exact_recovery(recovery)
    with journal_sender_gate(store):
        occurrence = _snapshot_occurrence(
            store=store,
            occurrence=runtime_occurrence,
        )
        challenge = prepare_chronology_challenge(
            store=store,
            recovery=recovery,
            source_sha=source_sha,
            scope=scope,
            release_artifact_id=release_artifact_id,
            release_artifact_sha256=release_artifact_sha256,
        )
        _require_runtime_binding(challenge=challenge, occurrence=occurrence)
        started_monotonic_ns = time.monotonic_ns()
        started_wall_utc_ns = time.time_ns()
        attempt = DurableChronologyAttempt(
            challenge=challenge,
            runtime_occurrence=occurrence,
            prepared_event_id=_prepared_event_id(challenge.challenge_digest),
            prepared_journal_sequence=0,
            started_monotonic_ns=started_monotonic_ns,
            started_wall_utc_ns=started_wall_utc_ns,
        )
        payload = _prepared_payload(attempt)
        event = {
            "event_id": attempt.prepared_event_id,
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
            command_id=attempt.prepared_event_id,
            actor="trusted-chronology",
            environment=challenge.runtime_environment,
            idempotency_key=attempt.prepared_event_id,
            request={
                "challenge_digest": challenge.challenge_digest,
                "runtime_occurrence_id": occurrence.runtime_occurrence_id,
            },
            result={"status": "PREPARED"},
            state_version=1,
            events=[(event, None)],
            expected_journal_sequence=challenge.journal_sequence,
        )
        events = _load_events(store, challenge.challenge_digest)
        if len(events) != 1:
            raise TrustedChronologyError("trusted chronology prepare did not round-trip")
        _validate_prepared_event(events[0])
        prepared_sequence = events[0].get("journal_sequence")
        if type(prepared_sequence) is not int or prepared_sequence <= 0:
            raise TrustedChronologyError("trusted chronology prepared sequence is invalid")
        return DurableChronologyAttempt(
            challenge=challenge,
            runtime_occurrence=occurrence,
            prepared_event_id=attempt.prepared_event_id,
            prepared_journal_sequence=prepared_sequence,
            started_monotonic_ns=started_monotonic_ns,
            started_wall_utc_ns=started_wall_utc_ns,
        )


def chronology_measurement_requirement(
    attempt: DurableChronologyAttempt,
    measurement_bytes: bytes,
) -> str:
    """Return the exact signed requirement id for this runtime/time observation."""

    if type(attempt) is not DurableChronologyAttempt:
        raise TypeError("attempt must be exact DurableChronologyAttempt")
    if type(measurement_bytes) is not bytes:
        raise TypeError("measurement_bytes must be exact bytes")
    transcript = parse_challenge_bound_measurement(
        measurement_bytes,
        challenge=attempt.challenge,
    )
    digest = "sha256:" + sha256(measurement_bytes).hexdigest()
    subject = {
        "account_id": attempt.runtime_occurrence.account_id,
        "challenge_digest": attempt.challenge.challenge_digest,
        "external_authority_id": transcript.authority_id,
        "external_protocol_id": transcript.protocol_id,
        "external_protocol_version": transcript.protocol_version,
        "external_response_id": transcript.response_id,
        "host_id": attempt.runtime_occurrence.host_id,
        "limits": {
            "max_external_local_offset_us": str(_MAX_EXTERNAL_LOCAL_OFFSET_US),
            "max_external_uncertainty_us": str(_MAX_EXTERNAL_UNCERTAINTY_US),
            "max_local_rate_divergence_ns": str(_MAX_LOCAL_RATE_DIVERGENCE_NS),
            "max_request_elapsed_ns": str(_MAX_REQUEST_ELAPSED_NS),
        },
        "measurement_sha256": digest,
        "release_artifact_id": attempt.challenge.release_artifact_id,
        "release_artifact_sha256": attempt.challenge.release_artifact_sha256,
        "runtime_occurrence_id": attempt.runtime_occurrence.runtime_occurrence_id,
        "scope": attempt.challenge.scope.value,
        "source_sha": attempt.challenge.source_sha,
        "utc_lower_bound": transcript.utc_lower_bound,
        "utc_upper_bound": transcript.utc_upper_bound,
    }
    return "trusted-chronology:" + sha256(_canonical_json_bytes(subject)).hexdigest()


def _require_current_attempt(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    attempt: DurableChronologyAttempt,
) -> dict[str, object]:
    if type(attempt) is not DurableChronologyAttempt:
        raise TypeError("attempt must be exact DurableChronologyAttempt")
    recovery = _require_exact_recovery(recovery)
    identity = _selected_store_identity(store)
    if (
        journal_store_identity_digest(identity)
        != attempt.challenge.store_identity_digest
    ):
        raise PermissionError("trusted chronology JournalStore identity changed")
    if recovery.durable_owner_store_identity != identity:
        raise PermissionError("trusted chronology recovery store changed")
    if recovery.clock_trusted is not True:
        raise PermissionError("trusted chronology local clock health is not trusted")
    owner = recovery.owner
    if (
        owner is None
        or owner.owner_id != attempt.challenge.owner_id
        or owner.epoch != attempt.challenge.owner_epoch
        or recovery.owner_scope != attempt.challenge.owner_scope
    ):
        raise PermissionError("trusted chronology recovery owner changed")
    if (
        recovery.clock_incident_generation
        != attempt.challenge.clock_incident_generation
    ):
        raise PermissionError("trusted chronology clock incident generation changed")
    occurrence = _snapshot_occurrence(
        store=store,
        occurrence=attempt.runtime_occurrence,
    )
    _require_runtime_binding(challenge=attempt.challenge, occurrence=occurrence)
    events = _load_events(store, attempt.challenge.challenge_digest)
    if not events:
        raise PermissionError("trusted chronology prepared event is missing")
    payload = _validate_prepared_event(events[0], attempt=attempt)
    if events[0].get("journal_sequence") != attempt.prepared_journal_sequence:
        raise PermissionError("trusted chronology prepared journal sequence changed")
    return payload


def _measurement_evidence_ref(
    accepted: AcceptedQualificationAttestation,
    *,
    measurement_sha256: str,
):
    matches = tuple(
        ref
        for ref in accepted.evidence_refs
        if ref.evidence_kind == _MEASUREMENT_KIND
        and ref.media_type == _MEASUREMENT_MEDIA_TYPE
        and ref.sha256 == measurement_sha256
        and ref.source_sha == accepted.source_sha
    )
    if len(matches) != 1:
        raise TrustedChronologyError(
            "accepted chronology attestation must bind exactly one raw measurement"
        )
    return matches[0]


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
    if abs(wall_elapsed - monotonic_elapsed) > _MAX_LOCAL_RATE_DIVERGENCE_NS:
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
        "local_rate_divergence_ns": abs(wall_elapsed - monotonic_elapsed),
        "request_elapsed_ns": monotonic_elapsed,
    }


def _accepted_cut_payload(
    *,
    attempt: DurableChronologyAttempt,
    transcript: ChronologyMeasurementTranscript,
    measurement_artifact_id: str,
    measurement_sha256: str,
    accepted: AcceptedQualificationAttestation,
    accepted_sequence: int,
    metrics: dict[str, int],
) -> dict[str, object]:
    challenge = attempt.challenge
    occurrence = attempt.runtime_occurrence
    cut_id = _cut_id(challenge.challenge_digest, accepted.attestation_id)
    payload: dict[str, object] = {
        "accepted_attestation_digest": accepted.attestation_digest,
        "accepted_attestation_id": accepted.attestation_id,
        "accepted_policy_id": accepted.policy_id,
        "accepted_trust_root_id": accepted.trust_root_id,
        "account_id": occurrence.account_id,
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
        "host_id": occurrence.host_id,
        "local_rate_divergence_ns": str(metrics["local_rate_divergence_ns"]),
        "max_external_local_offset_us": str(_MAX_EXTERNAL_LOCAL_OFFSET_US),
        "max_external_uncertainty_us": str(_MAX_EXTERNAL_UNCERTAINTY_US),
        "max_local_rate_divergence_ns": str(_MAX_LOCAL_RATE_DIVERGENCE_NS),
        "max_request_elapsed_ns": str(_MAX_REQUEST_ELAPSED_NS),
        "measurement_artifact_id": measurement_artifact_id,
        "measurement_sha256": measurement_sha256,
        "owner_epoch": str(challenge.owner_epoch),
        "owner_id": challenge.owner_id,
        "owner_scope": challenge.owner_scope,
        "release_artifact_id": challenge.release_artifact_id,
        "release_artifact_sha256": challenge.release_artifact_sha256,
        "request_elapsed_ns": str(metrics["request_elapsed_ns"]),
        "runtime_environment": challenge.runtime_environment,
        "runtime_occurrence_id": occurrence.runtime_occurrence_id,
        "schema_version": _SCHEMA_VERSION,
        "scope": challenge.scope.value,
        "source_sha": challenge.source_sha,
        "store_identity_digest": challenge.store_identity_digest,
        "utc_lower_bound": transcript.utc_lower_bound,
        "utc_upper_bound": transcript.utc_upper_bound,
    }
    # accepted_journal_sequence is excluded from the durable payload because it
    # does not exist until SQLite assigns the row. It is reconstructed from the
    # journal envelope when the immutable cut is read.
    payload["cut_digest"] = _cut_digest(payload)
    return payload


def _validate_accepted_event(event: object) -> tuple[dict[str, object], int]:
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
        "account_id",
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
        "host_id",
        "local_rate_divergence_ns",
        "max_external_local_offset_us",
        "max_external_uncertainty_us",
        "max_local_rate_divergence_ns",
        "max_request_elapsed_ns",
        "measurement_artifact_id",
        "measurement_sha256",
        "owner_epoch",
        "owner_id",
        "owner_scope",
        "release_artifact_id",
        "release_artifact_sha256",
        "request_elapsed_ns",
        "runtime_environment",
        "runtime_occurrence_id",
        "schema_version",
        "scope",
        "source_sha",
        "store_identity_digest",
        "utc_lower_bound",
        "utc_upper_bound",
    }
    if set(payload) != expected or payload.get("schema_version") != _SCHEMA_VERSION:
        raise TrustedChronologyError("trusted chronology accepted payload schema is invalid")
    if event.get("aggregate_id") != payload.get("challenge_digest"):
        raise TrustedChronologyError("trusted chronology cut aggregate binding is invalid")
    if payload.get("cut_digest") != _cut_digest(payload):
        raise TrustedChronologyError("trusted chronology cut digest mismatch")
    if event.get("event_id") != payload.get("cut_id"):
        raise TrustedChronologyError("trusted chronology cut event id is invalid")
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise TrustedChronologyError("trusted chronology accepted sequence is invalid")
    return payload, sequence


def _cut_from_event(event: object) -> TrustedChronologyCut:
    payload, sequence = _validate_accepted_event(event)
    try:
        scope = ChronologyScope(payload["scope"])
    except (ValueError, TypeError) as error:
        raise TrustedChronologyError("trusted chronology cut scope is invalid") from error
    return TrustedChronologyCut(
        cut_id=_uuid_text(payload["cut_id"], name="cut_id"),
        scope=scope,
        source_sha=_token(payload["source_sha"], name="source_sha"),
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
        runtime_environment=_token(
            payload["runtime_environment"],
            name="runtime_environment",
        ),
        runtime_occurrence_id=_uuid_text(
            payload["runtime_occurrence_id"],
            name="runtime_occurrence_id",
        ),
        host_id=_token(payload["host_id"], name="host_id"),
        account_id=_token(payload["account_id"], name="account_id"),
        release_artifact_id=payload["release_artifact_id"],
        release_artifact_sha256=payload["release_artifact_sha256"],
        covered_utc=_token(payload["covered_utc"], name="covered_utc"),
        utc_lower_bound=_token(payload["utc_lower_bound"], name="utc_lower_bound"),
        utc_upper_bound=_token(payload["utc_upper_bound"], name="utc_upper_bound"),
        external_authority_id=_token(
            payload["external_authority_id"], name="external_authority_id"
        ),
        external_protocol_id=_token(
            payload["external_protocol_id"], name="external_protocol_id"
        ),
        external_protocol_version=_token(
            payload["external_protocol_version"],
            name="external_protocol_version",
        ),
        external_response_id=_token(
            payload["external_response_id"], name="external_response_id"
        ),
        measurement_artifact_id=_uuid_text(
            payload["measurement_artifact_id"],
            name="measurement_artifact_id",
        ),
        measurement_sha256=_digest(
            payload["measurement_sha256"], name="measurement_sha256"
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
            payload["accepted_policy_id"], name="accepted_policy_id"
        ),
        accepted_trust_root_id=_digest(
            payload["accepted_trust_root_id"], name="accepted_trust_root_id"
        ),
        accepted_journal_sequence=sequence,
        cut_digest=_digest(payload["cut_digest"], name="cut_digest"),
    )


def accept_trusted_chronology_cut(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    attempt: DurableChronologyAttempt,
    measurement_bytes: bytes,
    receipt: SignedQualificationAttestation,
    evidence_store: ArtifactStore,
    evidence_root: str,
) -> TrustedChronologyCut:
    """Verify and atomically persist one conservative occurrence-bound UTC cut."""

    if type(measurement_bytes) is not bytes:
        raise TypeError("measurement_bytes must be exact bytes")
    if type(receipt) is not SignedQualificationAttestation:
        raise TypeError("receipt must be exact SignedQualificationAttestation")
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")

    recovery = _require_exact_recovery(recovery)
    with journal_sender_gate(store):
        _require_current_attempt(store=store, recovery=recovery, attempt=attempt)
        transcript = parse_challenge_bound_measurement(
            measurement_bytes,
            challenge=attempt.challenge,
        )
        finished_monotonic_ns = time.monotonic_ns()
        finished_wall_utc_ns = time.time_ns()
        metrics = _check_measurement_limits(
            attempt=attempt,
            transcript=transcript,
            finished_monotonic_ns=finished_monotonic_ns,
            finished_wall_utc_ns=finished_wall_utc_ns,
        )
        requirement_id = chronology_measurement_requirement(
            attempt,
            measurement_bytes,
        )
        measurement_sha256 = "sha256:" + sha256(measurement_bytes).hexdigest()

        # Freeze the durable frontier before invoking trust verification. Any
        # owner/incident/runtime or unrelated durable write racing the verifier
        # invalidates the atomic accept via expected_journal_sequence below.
        journal_cut = _current_sequence(store)
        accepted = verify_canonical_qualification_attestation(
            receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=attempt.challenge.source_sha,
            expected_domain=_QUALIFICATION_DOMAIN,
            expected_gate=_QUALIFICATION_GATE,
            expected_package_id=_QUALIFICATION_PACKAGE,
            expected_protocol_id=_QUALIFICATION_PROTOCOL,
            expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
            expected_requirement_id=requirement_id,
            expected_release_artifact_id=attempt.challenge.release_artifact_id,
            expected_release_artifact_sha256=(
                attempt.challenge.release_artifact_sha256
            ),
        )
        if type(accepted) is not AcceptedQualificationAttestation:
            raise TrustedChronologyError(
                "canonical chronology verifier returned non-canonical acceptance"
            )
        if accepted.result != "PASS" or accepted.unresolved_limits:
            raise TrustedChronologyError(
                "trusted chronology requires PASS with no unresolved limits"
            )
        if (
            accepted.source_sha != attempt.challenge.source_sha
            or accepted.domain != _QUALIFICATION_DOMAIN
            or accepted.gate != _QUALIFICATION_GATE
            or accepted.package_id != _QUALIFICATION_PACKAGE
            or accepted.protocol_id != _QUALIFICATION_PROTOCOL
            or accepted.protocol_version != _QUALIFICATION_PROTOCOL_VERSION
            or accepted.requirement_id != requirement_id
        ):
            raise TrustedChronologyError(
                "accepted chronology attestation scope differs from requested scope"
            )
        if attempt.challenge.scope is ChronologyScope.SOURCE_QUALIFICATION:
            if (
                accepted.release_artifact_id is not None
                or accepted.release_artifact_sha256 is not None
            ):
                raise TrustedChronologyError(
                    "SOURCE_QUALIFICATION cut cannot consume release-scoped attestation"
                )
        elif (
            accepted.release_artifact_id != attempt.challenge.release_artifact_id
            or accepted.release_artifact_sha256
            != attempt.challenge.release_artifact_sha256
        ):
            raise TrustedChronologyError(
                "RELEASE_RUNTIME attestation release identity mismatch"
            )

        measurement_ref = _measurement_evidence_ref(
            accepted,
            measurement_sha256=measurement_sha256,
        )
        # Recheck all mutable/durable selectors after external verification and
        # immediately before the CAS write.
        _require_current_attempt(store=store, recovery=recovery, attempt=attempt)
        if _current_sequence(store) != journal_cut:
            raise TrustedChronologyError(
                "durable journal changed during chronology verification"
            )

        provisional = _accepted_cut_payload(
            attempt=attempt,
            transcript=transcript,
            measurement_artifact_id=measurement_ref.artifact_id,
            measurement_sha256=measurement_sha256,
            accepted=accepted,
            accepted_sequence=0,
            metrics=metrics,
        )
        cut_id = provisional["cut_id"]
        event = {
            "event_id": cut_id,
            "event_type": _ACCEPTED_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": attempt.challenge.challenge_digest,
            "aggregate_version": "2",
            "payload": provisional,
            "payload_hash": payload_digest(provisional),
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
        _validate_prepared_event(events[0], attempt=attempt)
        cut = _cut_from_event(events[1])
        return cut


def require_current_trusted_chronology_cut(
    *,
    store: JournalStore,
    recovery: RecoveryController,
    runtime_occurrence: ProductionHostRuntimeOccurrence,
    cut: TrustedChronologyCut,
    expected_source_sha: str,
    expected_scope: ChronologyScope,
    expected_release_artifact_id: str | None = None,
    expected_release_artifact_sha256: str | None = None,
) -> TrustedChronologyCut:
    """Re-read one accepted cut and require its runtime/incident scope is current."""

    if type(cut) is not TrustedChronologyCut:
        raise TypeError("cut must be exact TrustedChronologyCut")
    if type(expected_scope) is not ChronologyScope:
        raise TypeError("expected_scope must be exact ChronologyScope")
    recovery = _require_exact_recovery(recovery)
    identity = _selected_store_identity(store)
    if journal_store_identity_digest(identity) != cut.store_identity_digest:
        raise PermissionError("trusted chronology cut store identity changed")
    occurrence = _snapshot_occurrence(
        store=store,
        occurrence=runtime_occurrence,
    )
    if (
        occurrence.runtime_occurrence_id != cut.runtime_occurrence_id
        or occurrence.host_id != cut.host_id
        or occurrence.account_id != cut.account_id
        or occurrence.environment != cut.runtime_environment
    ):
        raise PermissionError("trusted chronology cut runtime occurrence changed")
    if recovery.durable_owner_store_identity != identity or recovery.clock_trusted is not True:
        raise PermissionError("trusted chronology recovery/clock authority changed")
    owner = recovery.owner
    if (
        owner is None
        or owner.owner_id != cut.owner_id
        or owner.epoch != cut.owner_epoch
        or recovery.owner_scope != cut.owner_scope
        or recovery.clock_incident_generation != cut.clock_incident_generation
    ):
        raise PermissionError("trusted chronology owner/incident generation changed")
    if expected_source_sha != cut.source_sha or expected_scope is not cut.scope:
        raise PermissionError("trusted chronology source/scope mismatch")
    if expected_scope is ChronologyScope.SOURCE_QUALIFICATION:
        if (
            expected_release_artifact_id is not None
            or expected_release_artifact_sha256 is not None
            or cut.release_artifact_id is not None
            or cut.release_artifact_sha256 is not None
        ):
            raise PermissionError("source chronology cannot satisfy release scope")
    else:
        if (
            expected_release_artifact_id != cut.release_artifact_id
            or expected_release_artifact_sha256 != cut.release_artifact_sha256
        ):
            raise PermissionError("trusted chronology release identity mismatch")

    # The cut aggregate id is the challenge digest, which is not carried on the
    # public cut because consumers do not use challenge internals. Find the
    # accepted event by exact immutable cut id across this dedicated aggregate
    # family and require one unique match.
    identity = _selected_store_identity(store)
    with journal_store_authority_scope(store, identity):
        all_events = JournalStore.load_events_by_aggregate_type(store, _AGGREGATE_TYPE)
    matching = tuple(
        event
        for event in all_events
        if event.get("event_type") == _ACCEPTED_EVENT
        and event.get("event_id") == cut.cut_id
    )
    if len(matching) != 1:
        raise PermissionError("trusted chronology cut durable event is unavailable")
    durable = _cut_from_event(matching[0])
    if durable != cut:
        raise PermissionError("trusted chronology cut differs from durable authority")
    return durable


def require_chronology_horizon(
    cut: TrustedChronologyCut,
    *claimed_instants: str,
) -> None:
    """Require each terminal-evidence instant to be at/before the conservative cut."""

    if type(cut) is not TrustedChronologyCut:
        raise TypeError("cut must be exact TrustedChronologyCut")
    _covered_text, covered = _instant(cut.covered_utc, name="covered_utc")
    for index, value in enumerate(claimed_instants):
        _text, observed = _instant(value, name=f"claimed_instant[{index}]")
        if observed > covered:
            raise PermissionError(
                "claimed terminal evidence horizon is not covered by trusted chronology"
            )
