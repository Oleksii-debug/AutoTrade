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

from .persistence import JournalStore


class SenderAuthorityError(PermissionError):
    """Raised when the sender authority gate cannot be established safely."""


_LEASE_FACTORY = object()
_TAKEOVER_AGGREGATE_TYPE = "recovery_takeover"
_TAKEOVER_COMPLETE_EVENT_TYPE = "RecoveryTakeoverOwnerCommitted"


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


def _lease(
    *, owner_scope: str, journal_path: Path, gate_path: Path
) -> SenderAuthorityLease:
    return SenderAuthorityLease(
        owner_scope=owner_scope,
        journal_path=str(journal_path),
        gate_path=str(gate_path),
        _factory=_LEASE_FACTORY,
    )


def _assert_no_pending_takeover(store: JournalStore, *, owner_scope: str) -> None:
    """Fail closed while a crash-resumable takeover is not durably complete."""

    scope = _canonical_scope(owner_scope)
    events = JournalStore.load_events_by_aggregate_type(
        store,
        _TAKEOVER_AGGREGATE_TYPE,
    )
    latest_by_aggregate: dict[str, dict[str, object]] = {}
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
            latest_by_aggregate[aggregate_id] = event

    for event in latest_by_aggregate.values():
        if event.get("event_type") != _TAKEOVER_COMPLETE_EVENT_TYPE:
            raise SenderAuthorityError(
                "sender authority is suspended by pending durable takeover"
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
                yield prepare_lease()
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
