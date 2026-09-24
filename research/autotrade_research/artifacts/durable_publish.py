from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCK_LOCAL = threading.local()


def _resolved_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _thread_lock_for(path: Path) -> threading.RLock:
    key = _resolved_key(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _lock_handle(handle) -> None:
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def durable_path_lock(path: str | Path) -> Iterator[None]:
    """Serialize cooperating cross-process publication for one durable path."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = _resolved_key(destination)
    thread_lock = _thread_lock_for(destination)

    with thread_lock:
        held = getattr(_PATH_LOCK_LOCAL, "held", None)
        if held is None:
            held = {}
            _PATH_LOCK_LOCAL.held = held
        current = held.get(key)
        if current is not None:
            current[0] += 1
            try:
                yield
            finally:
                current[0] -= 1
            return

        lock_path = destination.with_name(f".{destination.name}.lock")
        handle = lock_path.open("a+b")
        try:
            _lock_handle(handle)
            held[key] = [1, handle]
            try:
                yield
            finally:
                del held[key]
                _unlock_handle(handle)
        finally:
            handle.close()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_durable_file(path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("ab") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def sync_parent_directory(path: str | Path) -> None:
    """Durably publish a directory-entry change where the platform supports it."""
    destination = Path(path)
    if os.name == "nt":
        return
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Durably publish one whole JSON object for cooperating writers.

    This is artifact/config publication only. It is not a multi-record database
    transaction and must never be used as AutoTrade's financial ledger.
    """
    if type(payload) is not dict:
        raise TypeError("payload must be a dict")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        with durable_path_lock(destination):
            os.replace(temporary, destination)
            temporary = None
            sync_parent_directory(destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
