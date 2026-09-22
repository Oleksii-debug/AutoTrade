from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import BinaryIO


class ResourceLockError(RuntimeError):
    """Resource lock acquisition, integrity, or teardown failure."""


class ResourceLockBusyError(ResourceLockError):
    """Raised when another process currently owns the advisory lock."""


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


class ResourceLock:
    """Persistent-path crash-releasing advisory lock for cooperating local workers.

    This is a local-filesystem coordination primitive. It is not a distributed
    execution fence and must never authorize financial order submission.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.path = Path(lock_path)
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise ResourceLockError("resource lock is already held by this lock object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_existing_lock_path()
        try:
            handle = self.path.open("a+b")
        except OSError as exc:
            raise ResourceLockError("cannot open resource lock path") from exc
        try:
            self._validate_handle_identity(handle)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            self._lock_handle(handle)
            self._validate_handle_identity(handle)
        except BaseException as acquire_error:
            try:
                handle.close()
            except BaseException as close_error:
                self._handle = handle
                _add_secondary_failure_note(
                    acquire_error,
                    "resource lock close also failed during acquisition cleanup",
                    close_error,
                )
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._unlock_handle(handle)
        except BaseException as unlock_error:
            try:
                handle.close()
            except BaseException as close_error:
                _add_secondary_failure_note(
                    unlock_error,
                    "resource lock close also failed after unlock failure",
                    close_error,
                )
                self._handle = handle
            else:
                self._handle = None
            raise
        try:
            handle.close()
        finally:
            self._handle = None

    def __enter__(self) -> "ResourceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_value is None:
            self.release()
            return
        try:
            self.release()
        except BaseException as release_error:
            _add_secondary_failure_note(
                exc_value,
                "ResourceLock release also failed while propagating the primary error",
                release_error,
            )

    def _validate_existing_lock_path(self) -> None:
        try:
            path_stat = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ResourceLockError("cannot inspect resource lock path") from exc
        if not stat.S_ISREG(path_stat.st_mode):
            raise ResourceLockError("resource lock path must be a regular non-symlink file")
        if path_stat.st_nlink != 1:
            raise ResourceLockError("resource lock path must not have hard-link aliases")

    def _validate_handle_identity(self, handle: BinaryIO) -> None:
        try:
            opened = os.fstat(handle.fileno())
            path_stat = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise ResourceLockError("resource lock path changed during acquisition") from exc
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise ResourceLockError("resource lock path must be a regular non-symlink file")
        if opened.st_nlink != 1 or path_stat.st_nlink != 1:
            raise ResourceLockError("resource lock path must not have hard-link aliases")
        if not os.path.samestat(opened, path_stat):
            raise ResourceLockError("resource lock path changed during acquisition")

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ResourceLockBusyError("another process owns the resource lock") from exc
                raise ResourceLockError("cannot acquire resource lock") from exc
            return

        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ResourceLockBusyError("another process owns the resource lock") from exc
            raise ResourceLockError("cannot acquire resource lock") from exc

    @staticmethod
    def _unlock_handle(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
