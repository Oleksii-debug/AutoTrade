from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
import threading
import stat
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCK_LOCAL = threading.local()


class DurablePublishLockError(RuntimeError):
    """Raised when the publication lock path is unsafe or changes identity."""


def _validate_existing_lock_path(lock_path: Path) -> None:
    try:
        path_stat = os.stat(lock_path, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DurablePublishLockError("cannot inspect publication lock path") from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise DurablePublishLockError(
            "publication lock path must be a regular non-symlink file"
        )
    if path_stat.st_nlink != 1:
        raise DurablePublishLockError(
            "publication lock path must not have hard-link aliases"
        )


def _validate_lock_handle_identity(lock_path: Path, handle) -> None:
    try:
        opened = os.fstat(handle.fileno())
        path_stat = os.stat(lock_path, follow_symlinks=False)
    except OSError as exc:
        raise DurablePublishLockError(
            "publication lock path changed during acquisition"
        ) from exc
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise DurablePublishLockError(
            "publication lock path must be a regular non-symlink file"
        )
    if opened.st_nlink != 1 or path_stat.st_nlink != 1:
        raise DurablePublishLockError(
            "publication lock path must not have hard-link aliases"
        )
    if not os.path.samestat(opened, path_stat):
        raise DurablePublishLockError(
            "publication lock path changed during acquisition"
        )


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
        _validate_existing_lock_path(lock_path)
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            raise DurablePublishLockError(
                "cannot open publication lock path"
            ) from exc
        try:
            _validate_lock_handle_identity(lock_path, handle)
            _lock_handle(handle)
            _validate_lock_handle_identity(lock_path, handle)
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


def _sync_parent_directory(path: Path) -> None:
    """Compatibility fault-injection seam for atomic_write_json."""
    sync_parent_directory(path)


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
            _sync_parent_directory(destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
