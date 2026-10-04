"""Inter-process sender authority gate for durable PAPER/LIVE ownership.

The gate serializes the terminal provider-send window with an owner takeover.
It is deliberately not an ownership claim by itself: callers must validate the
journal-backed owner fence while holding the gate. The gate exists so that a
validated old sender cannot cross its durable SubmissionSending barrier, lose
ownership, and only then emit provider bytes.

The lock is scoped to the canonical JournalStore backing file plus recovery
owner scope. Process death releases the OS advisory lock; the durable journal
remains the source of ownership and UNKNOWN-send truth.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
from typing import Iterator

from .persistence import JournalStore, payload_digest


class SenderAuthorityError(PermissionError):
    """Raised when the sender authority gate cannot be established safely."""


_LEASE_FACTORY = object()
_TAKEOVER_AGGREGATE_TYPE = "recovery_takeover"
_TAKEOVER_EVENT_TYPES = (
    "RecoveryTakeoverStarted",
    "RecoveryTakeoverEvidenceIssued",
    "RecoveryTakeoverOwnerCommitted",
)
_TAKEOVER_COMPLETE_EVENT_TYPE = _TAKEOVER_EVENT_TYPES[-1]


@dataclass(frozen=True)
class SenderAuthorityLease:
    """Ephemeral proof that code is executing inside the sender gate window."""

    owner_scope: str
    journal_path: str
    gate_path: str
    _factory: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory is not _LEASE_FACTORY:
            raise SenderAuthorityError(
                "sender authority leases are issued only by the process-shared gate"
            )


def _canonical_scope(owner_scope: object) -> str:
    if type(owner_scope) is not str or not owner_scope.strip():
        raise SenderAuthorityError("sender authority owner scope is required")
    return owner_scope.strip()


def _canonical_store_path(store: JournalStore) -> Path:
    if type(store) is not JournalStore:
        raise SenderAuthorityError("sender authority requires canonical JournalStore")
    state = vars(store)
    if set(state) != {"path", "_store_identity"}:
        raise SenderAuthorityError("sender authority JournalStore state is shadowed")
    path = state.get("path")
    identity = JournalStore.store_identity.__get__(store, JournalStore)
    if getattr(identity, "canonical_path", None) != str(path):
        raise SenderAuthorityError("sender authority JournalStore identity changed")
    return Path(path)


def sender_authority_gate_path(store: JournalStore, *, owner_scope: str) -> Path:
    """Return the deterministic lock path for one durable sender scope."""

    journal_path = _canonical_store_path(store)
    scope = _canonical_scope(owner_scope)
    digest = sha256(
        json.dumps(
            {
                "authority": "AUTOTRADE_SENDER_AUTHORITY_GATE_V1",
                "journal_path": str(journal_path),
                "owner_scope": scope,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return journal_path.with_name(
        f".{journal_path.name}.sender-authority-{digest}.lock"
    )


def _assert_posix_gate_binding(descriptor: int, lock_path: Path) -> None:
    """Prove the held POSIX lock still names the canonical gate pathname."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise SenderAuthorityError(
            "sender authority gate requires no-follow pathname verification"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow
    verification_descriptor = None
    try:
        held = os.fstat(descriptor)
        path_state = os.stat(lock_path, follow_symlinks=False)
        verification_descriptor = os.open(lock_path, flags)
        current = os.fstat(verification_descriptor)
    except OSError as error:
        raise SenderAuthorityError(
            "sender authority gate pathname changed while lock was held"
        ) from error
    finally:
        if verification_descriptor is not None:
            try:
                os.close(verification_descriptor)
            except OSError as error:
                raise SenderAuthorityError(
                    "sender authority gate verification handle could not close"
                ) from error

    for observed in (held, path_state, current):
        if not stat.S_ISREG(observed.st_mode) or int(observed.st_nlink) != 1:
            raise SenderAuthorityError(
                "sender authority gate must retain one ordinary pathname"
            )
    if (
        int(held.st_dev) != int(path_state.st_dev)
        or int(held.st_ino) != int(path_state.st_ino)
        or int(held.st_dev) != int(current.st_dev)
        or int(held.st_ino) != int(current.st_ino)
    ):
        raise SenderAuthorityError(
            "sender authority gate pathname changed while lock was held"
        )


def _lease(
    *, owner_scope: str, journal_path: Path, gate_path: Path
) -> SenderAuthorityLease:
    return SenderAuthorityLease(
        owner_scope=owner_scope,
        journal_path=str(journal_path),
        gate_path=str(gate_path),
        _factory=_LEASE_FACTORY,
    )


def _validate_completed_takeover(
    store: JournalStore,
    *,
    owner_scope: str,
    aggregate_id: str,
    events: list[dict[str, object]],
) -> None:
    """Prove a completed takeover really advanced the durable owner chain.

    The normal sender path does not possess the credential-vault protector and
    therefore cannot independently reverify the sealed credential evidence.
    It can, however, require the exact takeover journal sequence and the exact
    RecoveryOwnerChanged event named by the completion. That is sufficient for
    sender safety: a stale source sender will fail its mandatory durable owner
    validation before it can append SubmissionSending or emit provider bytes.
    """

    if len(events) != len(_TAKEOVER_EVENT_TYPES):
        raise SenderAuthorityError(
            "sender authority is suspended by incomplete durable takeover"
        )
    for version, (event, expected_type) in enumerate(
        zip(events, _TAKEOVER_EVENT_TYPES),
        start=1,
    ):
        payload = event.get("payload")
        if (
            event.get("aggregate_id") != aggregate_id
            or event.get("event_type") != expected_type
            or event.get("aggregate_version") != version
            or type(payload) is not dict
            or payload.get("owner_scope") != owner_scope
            or payload_digest(payload) != event.get("payload_hash")
        ):
            raise SenderAuthorityError("durable takeover journal sequence is invalid")

    started, evidence, completed = events
    started_payload = started["payload"]
    evidence_payload = evidence["payload"]
    completion_payload = completed["payload"]
    identity_fields = (
        "takeover_id",
        "source_owner_id",
        "source_owner_epoch",
        "target_owner_id",
        "target_owner_epoch",
    )
    for field_name in identity_fields:
        if (
            evidence_payload.get(field_name) != started_payload.get(field_name)
            or completion_payload.get(field_name) != started_payload.get(field_name)
        ):
            raise SenderAuthorityError("durable takeover identity changed across events")
    if completion_payload.get("takeover_evidence_event_id") != evidence.get("event_id"):
        raise SenderAuthorityError("durable takeover completion evidence link is invalid")
    if (
        completion_payload.get("credential_transition_receipt_id")
        != evidence_payload.get("credential_transition_receipt_id")
    ):
        raise SenderAuthorityError(
            "durable takeover completion credential link is invalid"
        )

    target_owner_id = completion_payload.get("target_owner_id")
    target_owner_epoch = completion_payload.get("target_owner_epoch")
    owner_event_id = completion_payload.get("recovery_owner_event_id")
    owner_payload_hash = completion_payload.get("recovery_owner_payload_hash")
    owner_journal_sequence = completion_payload.get("recovery_owner_journal_sequence")
    if (
        type(target_owner_id) is not str
        or not target_owner_id
        or type(target_owner_epoch) is not int
        or target_owner_epoch < 1
        or type(owner_event_id) is not str
        or not owner_event_id
        or type(owner_payload_hash) is not str
        or not owner_payload_hash
        or type(owner_journal_sequence) is not int
        or owner_journal_sequence < 1
    ):
        raise SenderAuthorityError("durable takeover completion owner identity is invalid")

    owner_events = JournalStore.load_events(store, "recovery_owner", owner_scope)
    if len(owner_events) < target_owner_epoch:
        raise SenderAuthorityError("durable takeover target owner event is missing")
    owner_event = owner_events[target_owner_epoch - 1]
    owner_payload = owner_event.get("payload")
    if (
        owner_event.get("event_id") != owner_event_id
        or owner_event.get("event_type") != "RecoveryOwnerChanged"
        or owner_event.get("aggregate_version") != target_owner_epoch
        or owner_event.get("journal_sequence") != owner_journal_sequence
        or type(owner_payload) is not dict
        or owner_payload.get("owner_id") != target_owner_id
        or owner_payload.get("owner_epoch") != str(target_owner_epoch)
        or payload_digest(owner_payload) != owner_payload_hash
        or owner_event.get("payload_hash") != owner_payload_hash
    ):
        raise SenderAuthorityError(
            "durable takeover completion does not match committed recovery owner"
        )


def _assert_no_pending_takeover(store: JournalStore, *, owner_scope: str) -> None:
    """Fail closed while a crash-resumable takeover is not durably complete."""

    scope = _canonical_scope(owner_scope)
    events = JournalStore.load_events_by_aggregate_type(
        store,
        _TAKEOVER_AGGREGATE_TYPE,
    )
    grouped: dict[str, list[dict[str, object]]] = {}
    for event in events:
        if type(event) is not dict:
            raise SenderAuthorityError("sender takeover journal event is invalid")
        aggregate_id = event.get("aggregate_id")
        payload = event.get("payload")
        event_type = event.get("event_type")
        if (
            type(aggregate_id) is not str
            or not aggregate_id
            or type(payload) is not dict
            or type(event_type) is not str
            or not event_type
        ):
            raise SenderAuthorityError("sender takeover journal event is malformed")
        durable_scope = payload.get("owner_scope")
        if type(durable_scope) is not str or not durable_scope:
            raise SenderAuthorityError("sender takeover journal scope is invalid")
        if durable_scope == scope:
            grouped.setdefault(aggregate_id, []).append(event)

    for aggregate_id, aggregate_events in grouped.items():
        if aggregate_events[-1].get("event_type") != _TAKEOVER_COMPLETE_EVENT_TYPE:
            raise SenderAuthorityError(
                "sender authority is suspended by pending durable takeover"
            )
        _validate_completed_takeover(
            store,
            owner_scope=scope,
            aggregate_id=aggregate_id,
            events=aggregate_events,
        )


@contextmanager
def _sender_authority_window(
    store: JournalStore,
    *,
    owner_scope: str,
    allow_pending_takeover: bool,
) -> Iterator[SenderAuthorityLease]:
    journal_path = _canonical_store_path(store)
    scope = _canonical_scope(owner_scope)
    lock_path = sender_authority_gate_path(store, owner_scope=scope)
    if lock_path.parent != journal_path.parent:
        raise SenderAuthorityError(
            "sender authority gate must share the journal parent"
        )

    def prepare_lease() -> SenderAuthorityLease:
        if _canonical_store_path(store) != journal_path:
            raise SenderAuthorityError(
                "sender authority journal changed while acquiring gate"
            )
        if not allow_pending_takeover:
            _assert_no_pending_takeover(store, owner_scope=scope)
        return _lease(
            owner_scope=scope,
            journal_path=journal_path,
            gate_path=lock_path,
        )

    if sys.platform == "win32":
        from autotrade_foundation.windows_namespace import (
            retain_windows_parent_namespace,
            serialize_windows_directory_publication,
        )

        with retain_windows_parent_namespace(journal_path, create=True) as authority:
            with serialize_windows_directory_publication(
                authority,
                lock_name=lock_path.name,
            ):
                yield prepare_lease()
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise SenderAuthorityError(
            "sender authority gate must be an ordinary local file"
        ) from error
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or int(observed.st_nlink) != 1:
            raise SenderAuthorityError(
                "sender authority gate must have one ordinary pathname"
            )
        with os.fdopen(descriptor, "a+b", closefd=False) as stream:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                _assert_posix_gate_binding(stream.fileno(), lock_path)
                try:
                    yield prepare_lease()
                finally:
                    _assert_posix_gate_binding(stream.fileno(), lock_path)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def sender_authority_window(
    store: JournalStore,
    *,
    owner_scope: str,
) -> Iterator[SenderAuthorityLease]:
    """Hold the terminal send window and reject crash-pending takeovers.

    Lock ordering for WP-49 is sender authority gate first, then any provider
    credential-vault lease. A durable takeover-in-progress blocks this ordinary
    sender path until its owner transition is durably completed.
    """

    with _sender_authority_window(
        store,
        owner_scope=owner_scope,
        allow_pending_takeover=False,
    ) as lease:
        yield lease


@contextmanager
def takeover_authority_window(
    store: JournalStore,
    *,
    owner_scope: str,
) -> Iterator[SenderAuthorityLease]:
    """Hold the same gate for canonical takeover issue/resume processing.

    This path is intentionally separate from ``sender_authority_window`` so a
    previously durable takeover can resume after process death while ordinary
    provider sends remain fenced by that pending transition.
    """

    with _sender_authority_window(
        store,
        owner_scope=owner_scope,
        allow_pending_takeover=True,
    ) as lease:
        yield lease
