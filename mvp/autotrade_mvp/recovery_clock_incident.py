"""Durable recovery clock-incident chronology.

This module records *invalidation generations*, not trusted UTC evidence. A
clock-health loss is a durable financial-control fact that invalidates any
chronology cut bound to an earlier generation. Restoring the local health
boolean never mints trusted chronology; independent WP-48 evidence remains
required for that authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from uuid import NAMESPACE_URL, uuid5

from .persistence import JournalStore, payload_digest
from .sender_gate import journal_sender_gate


_CLOCK_AGGREGATE_TYPE = "recovery_clock_incident"
_CLOCK_LOST_EVENT = "RecoveryClockTrustLost"
_CLOCK_RESTORED_EVENT = "RecoveryClockTrustRestored"
_OWNER_AGGREGATE_TYPE = "recovery_owner"
_OWNER_EVENT_TYPE = "RecoveryOwnerChanged"
_SCHEMA_VERSION = "1"
_REASON_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_EVIDENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


@dataclass(frozen=True)
class ClockIncident:
    generation: int
    incident_id: str
    owner_id: str
    owner_epoch: int
    reason_code: str
    evidence_ref: str | None
    lost_journal_sequence: int
    restored_journal_sequence: int | None = None
    restore_owner_id: str | None = None
    restore_owner_epoch: int | None = None
    restore_reason_code: str | None = None
    restore_evidence_ref: str | None = None

    @property
    def restored(self) -> bool:
        return self.restored_journal_sequence is not None


def _canonical_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeError(f"{name} must be canonical non-empty text")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is int and value > 0:
        return value
    if (
        isinstance(value, str)
        and value.isdigit()
        and value != "0"
        and (len(value) == 1 or not value.startswith("0"))
    ):
        return int(value)
    raise RuntimeError(f"{name} must be a canonical positive integer")


def _optional_evidence(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    text = _canonical_text(value, name=name)
    if _EVIDENCE_RE.fullmatch(text) is None:
        raise RuntimeError(f"{name} is not canonical")
    return text


def _reason(value: object, *, name: str) -> str:
    text = _canonical_text(value, name=name)
    if _REASON_RE.fullmatch(text) is None:
        raise RuntimeError(f"{name} is not canonical")
    return text


def _event_sequence(event: dict[str, object]) -> int:
    return _positive_int(event.get("journal_sequence"), name="journal_sequence")


def _events_at_or_before_cut(
    store: JournalStore,
    aggregate_type: str,
    aggregate_id: str,
    journal_sequence_cut: int | None,
) -> tuple[dict[str, object], ...]:
    events = JournalStore.load_events(store, aggregate_type, aggregate_id)
    if journal_sequence_cut is None:
        return tuple(events)
    if type(journal_sequence_cut) is not int or journal_sequence_cut < 0:
        raise ValueError("journal_sequence_cut must be a non-negative integer")
    selected: list[dict[str, object]] = []
    for event in events:
        sequence = _event_sequence(event)
        if sequence <= journal_sequence_cut:
            selected.append(event)
    return tuple(selected)


def _validated_owner_events(
    store: JournalStore,
    owner_scope: str,
    *,
    journal_sequence_cut: int | None = None,
) -> tuple[tuple[int, str, int], ...]:
    events = _events_at_or_before_cut(
        store,
        _OWNER_AGGREGATE_TYPE,
        owner_scope,
        journal_sequence_cut,
    )
    owners: list[tuple[int, str, int]] = []
    for expected_epoch, event in enumerate(events, start=1):
        if event.get("event_type") != _OWNER_EVENT_TYPE:
            raise RuntimeError("recovery owner journal contains unsupported event type")
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload_digest(payload) != event.get("payload_hash"):
            raise RuntimeError("recovery owner journal payload is invalid")
        owner_id = _canonical_text(payload.get("owner_id"), name="owner_id")
        owner_epoch = _positive_int(payload.get("owner_epoch"), name="owner_epoch")
        aggregate_version = _positive_int(
            event.get("aggregate_version"), name="aggregate_version"
        )
        if aggregate_version != expected_epoch or owner_epoch != expected_epoch:
            raise RuntimeError("recovery owner epoch/version chain is invalid")
        owners.append((_event_sequence(event), owner_id, owner_epoch))
    return tuple(owners)


def _owner_at_sequence(
    owners: tuple[tuple[int, str, int], ...],
    sequence: int,
) -> tuple[str, int]:
    candidates = [item for item in owners if item[0] < sequence]
    if not candidates:
        raise RuntimeError("clock incident has no preceding durable recovery owner")
    _owner_sequence, owner_id, owner_epoch = candidates[-1]
    return owner_id, owner_epoch


def _incident_id(owner_scope: str, generation: int) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://recovery.autotrade.local/clock-incident/"
            f"{owner_scope!r}/{generation}",
        )
    )


def load_clock_incident_chain(
    store: JournalStore,
    *,
    owner_scope: str,
    journal_sequence_cut: int | None = None,
) -> tuple[ClockIncident, ...]:
    """Replay and validate the durable incident chain fail-closed.

    When a cut is supplied, both recovery-owner and incident facts are replayed
    only through that exact global journal frontier. Callers that use the
    bounded replay for mutation authority must re-check the live frontier and
    then CAS the write against the same cut.
    """

    scope = _canonical_text(owner_scope, name="owner_scope")
    owners = _validated_owner_events(
        store,
        scope,
        journal_sequence_cut=journal_sequence_cut,
    )
    events = _events_at_or_before_cut(
        store,
        _CLOCK_AGGREGATE_TYPE,
        scope,
        journal_sequence_cut,
    )
    incidents: list[ClockIncident] = []

    for expected_version, event in enumerate(events, start=1):
        aggregate_version = _positive_int(
            event.get("aggregate_version"), name="aggregate_version"
        )
        if aggregate_version != expected_version:
            raise RuntimeError("clock incident aggregate version chain is invalid")
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload_digest(payload) != event.get("payload_hash"):
            raise RuntimeError("clock incident payload is invalid")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise RuntimeError("clock incident schema version is unsupported")

        sequence = _event_sequence(event)
        owner_id = _canonical_text(payload.get("owner_id"), name="owner_id")
        owner_epoch = _positive_int(payload.get("owner_epoch"), name="owner_epoch")
        if _owner_at_sequence(owners, sequence) != (owner_id, owner_epoch):
            raise RuntimeError("clock incident durable owner binding is invalid")

        generation = _positive_int(payload.get("generation"), name="generation")
        incident_id = _canonical_text(payload.get("incident_id"), name="incident_id")
        if incident_id != _incident_id(scope, generation):
            raise RuntimeError("clock incident identity is invalid")

        event_type = event.get("event_type")
        if event_type == _CLOCK_LOST_EVENT:
            if incidents and not incidents[-1].restored:
                raise RuntimeError("clock incident generation overlaps an unresolved incident")
            if generation != len(incidents) + 1:
                raise RuntimeError("clock incident generation is not monotonic")
            incidents.append(
                ClockIncident(
                    generation=generation,
                    incident_id=incident_id,
                    owner_id=owner_id,
                    owner_epoch=owner_epoch,
                    reason_code=_reason(payload.get("reason_code"), name="reason_code"),
                    evidence_ref=_optional_evidence(
                        payload.get("evidence_ref"), name="evidence_ref"
                    ),
                    lost_journal_sequence=sequence,
                )
            )
            continue

        if event_type == _CLOCK_RESTORED_EVENT:
            if not incidents or incidents[-1].restored:
                raise RuntimeError("clock restoration has no unresolved incident")
            current = incidents[-1]
            if generation != current.generation or incident_id != current.incident_id:
                raise RuntimeError("clock restoration incident binding is invalid")
            incidents[-1] = replace(
                current,
                restored_journal_sequence=sequence,
                restore_owner_id=owner_id,
                restore_owner_epoch=owner_epoch,
                restore_reason_code=_reason(
                    payload.get("reason_code"), name="reason_code"
                ),
                restore_evidence_ref=_optional_evidence(
                    payload.get("evidence_ref"), name="evidence_ref"
                ),
            )
            continue

        raise RuntimeError("clock incident journal contains unsupported event type")

    return tuple(incidents)


def clock_incident_generation(store: JournalStore, *, owner_scope: str) -> int:
    incidents = load_clock_incident_chain(store, owner_scope=owner_scope)
    return incidents[-1].generation if incidents else 0


def clock_trusted_after_replay(store: JournalStore, *, owner_scope: str) -> bool:
    incidents = load_clock_incident_chain(store, owner_scope=owner_scope)
    return not incidents or incidents[-1].restored


def append_clock_trust_transition(
    store: JournalStore,
    *,
    owner_scope: str,
    owner_id: str,
    owner_epoch: int,
    trusted: bool,
    reason_code: str,
    evidence_ref: str | None,
    committed_at: str,
) -> int:
    """Persist one trust transition under the current durable recovery owner.

    The canonical sender gate serializes threads and processes on one host for
    one exact local JournalStore generation. The transition additionally binds
    owner/incident validation to one explicit global journal frontier and uses
    commit_command(expected_journal_sequence=...) as the atomic CAS. Any
    durable write that races validation therefore invalidates the transition.
    The append is replayed before success. This is not a remote/cross-host
    fence and not independent UTC evidence.
    """

    if type(trusted) is not bool:
        raise TypeError("trusted must be a boolean")
    scope = _canonical_text(owner_scope, name="owner_scope")
    normalized_owner = _canonical_text(owner_id, name="owner_id")
    epoch = _positive_int(owner_epoch, name="owner_epoch")
    reason = _reason(reason_code, name="reason_code")
    evidence = _optional_evidence(evidence_ref, name="evidence_ref")
    timestamp = _canonical_text(committed_at, name="committed_at")

    with journal_sender_gate(store):
        # Capture the global durable frontier before reading any owner/incident
        # authority. This prevents a new owner fact from being included in the
        # accepted journal cut while being omitted from a stale owner snapshot.
        journal_cut = JournalStore.current_journal_sequence(store)
        incidents = load_clock_incident_chain(
            store,
            owner_scope=scope,
            journal_sequence_cut=journal_cut,
        )
        currently_trusted = not incidents or incidents[-1].restored
        if trusted == currently_trusted:
            return incidents[-1].generation if incidents else 0

        owners = _validated_owner_events(
            store,
            scope,
            journal_sequence_cut=journal_cut,
        )
        clock_events = _events_at_or_before_cut(
            store,
            _CLOCK_AGGREGATE_TYPE,
            scope,
            journal_cut,
        )
        if JournalStore.current_journal_sequence(store) != journal_cut:
            raise ValueError(
                "journal sequence changed after clock incident validation"
            )
        if not owners or (owners[-1][1], owners[-1][2]) != (normalized_owner, epoch):
            raise PermissionError(
                "clock incident owner is not the current durable recovery owner"
            )

        aggregate_version = len(clock_events) + 1
        if trusted:
            current = incidents[-1]
            generation = current.generation
            incident_id = current.incident_id
            event_type = _CLOCK_RESTORED_EVENT
        else:
            generation = (incidents[-1].generation if incidents else 0) + 1
            incident_id = _incident_id(scope, generation)
            event_type = _CLOCK_LOST_EVENT

        payload = {
            "schema_version": _SCHEMA_VERSION,
            "generation": str(generation),
            "incident_id": incident_id,
            "owner_id": normalized_owner,
            "owner_epoch": str(epoch),
            "reason_code": reason,
            "evidence_ref": evidence,
        }
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://recovery.autotrade.local/clock-incident-event/"
                f"{scope!r}/{aggregate_version}/{event_type}/{incident_id}",
            )
        )
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _CLOCK_AGGREGATE_TYPE,
            "aggregate_id": scope,
            "aggregate_version": str(aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": timestamp,
        }
        runtime_environment = scope.split(":", 1)[0].upper()
        if runtime_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise PermissionError(
                "durable clock incident requires environment-scoped recovery owner"
            )
        JournalStore.commit_command(
            store,
            command_id=event_id,
            actor="recovery-clock-incident",
            environment=runtime_environment,
            idempotency_key=event_id,
            request={
                "owner_scope": scope,
                "owner_id": normalized_owner,
                "owner_epoch": str(epoch),
                "trusted": trusted,
                "reason_code": reason,
                "evidence_ref": evidence,
                "journal_sequence_cut": journal_cut,
            },
            result={
                "generation": generation,
                "incident_id": incident_id,
                "trusted": trusted,
            },
            state_version=aggregate_version,
            events=[(envelope, None)],
            expected_journal_sequence=journal_cut,
        )

        replayed = load_clock_incident_chain(store, owner_scope=scope)
        if not replayed or replayed[-1].generation != generation:
            raise RuntimeError("clock incident append did not round-trip")
        if replayed[-1].restored != trusted:
            raise RuntimeError("clock incident append produced wrong trust state")
        return generation
