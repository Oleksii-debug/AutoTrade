from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

from .resource_lock import ResourceLock, ResourceLockError

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCK_POISONS: dict[str, ResourceLock] = {}
_PATH_LOCK_LOCAL = threading.local()


class DurablePublishLockError(RuntimeError):
    """Raised when publication path ownership or teardown cannot be proven safe."""


def _add_secondary_failure_note(
    primary: BaseException,
    prefix: str,
    secondary: BaseException,
) -> None:
    try:
        secondary_text = f"{type(secondary).__name__}: {secondary}"
    except BaseException:
        secondary_text = "secondary exception details unavailable"
    try:
        primary.add_note(f"{prefix}: {secondary_text}")
    except BaseException:
        return


def _resolved_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _validate_publication_destination(path: Path) -> None:
    """Reject final-component aliases before they can split publication identity."""

    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DurablePublishLockError(
            "cannot inspect publication destination"
        ) from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise DurablePublishLockError(
            "publication destination must be a regular non-symlink file"
        )
    if path_stat.st_nlink != 1:
        raise DurablePublishLockError(
            "publication destination must not have hard-link aliases"
        )


def _thread_lock_for(path: Path) -> threading.RLock:
    key = _resolved_key(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _poison_for(key: str) -> ResourceLock | None:
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCK_POISONS.get(key)


def _remember_poison(key: str, lock: ResourceLock) -> None:
    with _PATH_LOCKS_GUARD:
        _PATH_LOCK_POISONS[key] = lock


@contextmanager
def durable_path_lock(path: str | Path) -> Iterator[None]:
    """Serialize cooperating cross-process publication for one durable path.

    The sidecar lock reuses the canonical local ResourceLock implementation in
    blocking mode. Final-component aliases are rejected and parent-directory
    aliases converge on the resolved destination for the sidecar identity.
    Re-entrancy is process/thread-local only. Any teardown path that cannot prove
    the OS handle closed poisons this process/path permanently.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _validate_publication_destination(destination)
    key = _resolved_key(destination)
    canonical_destination = Path(key)
    thread_lock = _thread_lock_for(canonical_destination)

    with thread_lock:
        _validate_publication_destination(destination)
        poisoned = _poison_for(key)
        if poisoned is not None:
            raise DurablePublishLockError(
                "publication lock state is poisoned after an unproven release"
            )

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

        lock_path = canonical_destination.with_name(
            f".{canonical_destination.name}.lock"
        )
        resource_lock = ResourceLock(lock_path, blocking=True)
        try:
            resource_lock.acquire()
        except ResourceLockError as exc:
            if resource_lock._handle is not None:
                _remember_poison(key, resource_lock)
            raise DurablePublishLockError(str(exc)) from exc

        held[key] = [1, resource_lock]
        primary_error: BaseException | None = None
        try:
            try:
                yield
            except BaseException as exc:
                primary_error = exc
                raise
        finally:
            del held[key]
            try:
                resource_lock.release()
            except BaseException as release_error:
                if resource_lock._handle is not None:
                    _remember_poison(key, resource_lock)
                if primary_error is not None:
                    _add_secondary_failure_note(
                        primary_error,
                        "publication lock release also failed",
                        release_error,
                    )
                else:
                    raise DurablePublishLockError(
                        "cannot prove publication lock release"
                    ) from release_error


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


def _sync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(path.parent, os.O_RDONLY)
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
    _validate_publication_destination(destination)
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
            _validate_publication_destination(destination)
            os.replace(temporary, destination)
            temporary = None
            _sync_parent_directory(destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
