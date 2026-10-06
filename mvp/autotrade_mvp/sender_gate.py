"""Same-journal local sender/owner exclusion for the initial single-host product.

This is an exclusion primitive, not durable financial evidence and not an
external/cross-host fencing issuer. It serializes recovery-owner mutation with
provider-send execution for one exact canonical JournalStore generation.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
from threading import Lock, local

from .persistence import JournalStore, require_exact_journal_store_authority


_GATE_LOCKS_GUARD = Lock()
_GATE_LOCKS_PID = os.getpid()
_GATE_LOCKS: dict[str, Lock] = {}
_GATE_HELD = local()


def _reset_gate_process_state() -> None:
    global _GATE_LOCKS_GUARD, _GATE_LOCKS_PID, _GATE_LOCKS, _GATE_HELD
    _GATE_LOCKS_GUARD = Lock()
    _GATE_LOCKS_PID = os.getpid()
    _GATE_LOCKS = {}
    _GATE_HELD = local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_gate_process_state)


def _thread_gate(key: str) -> Lock:
    global _GATE_LOCKS_PID, _GATE_LOCKS
    pid = os.getpid()
    with _GATE_LOCKS_GUARD:
        if _GATE_LOCKS_PID != pid:
            # Defensive fallback if this module reaches a new process by a
            # mechanism that did not run the registered at-fork reset.
            _GATE_LOCKS_PID = pid
            _GATE_LOCKS = {}
        lock = _GATE_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _GATE_LOCKS[key] = lock
        return lock


def _held_keys() -> set[str]:
    pid = os.getpid()
    if getattr(_GATE_HELD, "pid", None) != pid:
        _GATE_HELD.pid = pid
        _GATE_HELD.keys = set()
    return _GATE_HELD.keys


def _gate_path(store: JournalStore) -> tuple[Path, object]:
    identity = require_exact_journal_store_authority(
        store,
        subject="sender gate JournalStore",
    )
    path = store.path
    key = str(identity.canonical_path)
    suffix = sha256(key.encode("utf-8")).hexdigest()[:24]
    return path.with_name(path.name + ".sender-gate-" + suffix + ".lock"), identity


@contextmanager
def journal_sender_gate(store: JournalStore | None):
    """Exclude owner mutation and one irreversible send on the same journal.

    None is a deliberate no-op for legacy/non-durable recovery tests. Product
    PAPER/LIVE composition must use one exact durable JournalStore.
    """

    if store is None:
        yield
        return
    if type(store) is not JournalStore:
        raise TypeError("sender gate requires exact JournalStore")

    lock_path, selected_identity = _gate_path(store)
    key = str(selected_identity.canonical_path)
    held_keys = _held_keys()
    if key in held_keys:
        # A recovery/start/takeover callback invoked from inside the protected
        # provider section must fail instead of deadlocking on the process-local
        # non-reentrant lock or silently weakening the fence with RLock.
        raise RuntimeError("sender gate re-entry is forbidden")

    thread_lock = _thread_gate(key)
    with thread_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                held_keys.add(key)
                try:
                    current = require_exact_journal_store_authority(
                        store,
                        subject="sender gate JournalStore",
                    )
                    if current != selected_identity:
                        raise PermissionError(
                            "sender gate JournalStore authority changed"
                        )
                    yield
                finally:
                    held_keys.discard(key)
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                held_keys.add(key)
                try:
                    current = require_exact_journal_store_authority(
                        store,
                        subject="sender gate JournalStore",
                    )
                    if current != selected_identity:
                        raise PermissionError(
                            "sender gate JournalStore authority changed"
                        )
                    yield
                finally:
                    held_keys.discard(key)
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
