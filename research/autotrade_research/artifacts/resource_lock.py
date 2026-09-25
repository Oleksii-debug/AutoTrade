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
    """Attach cleanup evidence without replacing the primary failure."""

    try:
        secondary_text = f"{type(secondary).__name__}: {secondary}"
    except BaseException:
        secondary_text = "secondary exception details unavailable"
    try:
        primary.add_note(f"{prefix}: {secondary_text}")
    except BaseException:
        return


def _open_read_only_descriptor(path: Path) -> int:
    """Open a verification descriptor without following the final path alias."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            raise OSError(
                errno.ENOTSUP,
                "platform lacks no-follow verification open support",
                str(path),
            )
        return os.open(path, flags | no_follow)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel_handle = create_file(
        str(path),
        generic_read,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if kernel_handle == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    try:
        return msvcrt.open_osfhandle(kernel_handle, flags)
    except BaseException:
        close_handle(kernel_handle)
        raise

def _reject_known_remote_lock_path(path: Path) -> None:
    """Fail closed for Windows UNC and mapped remote-drive lock paths."""

    if os.name != "nt":
        return

    raw_path = os.fspath(path)
    if raw_path.startswith(("\\\\", "//")):
        raise ResourceLockError(
            "resource lock path must be on a qualified local filesystem"
        )

    absolute = os.path.abspath(raw_path)
    drive, _ = os.path.splitdrive(absolute)
    if not drive:
        raise ResourceLockError(
            "resource lock path has no qualified local Windows drive"
        )

    import ctypes
    from ctypes import wintypes

    get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT

    drive_type = get_drive_type(drive + "\\")
    # DRIVE_REMOVABLE=2, DRIVE_FIXED=3, DRIVE_CDROM=5, DRIVE_RAMDISK=6.
    # DRIVE_UNKNOWN=0, DRIVE_NO_ROOT_DIR=1 and DRIVE_REMOTE=4 are not
    # qualified for this local advisory-lock contract.
    if drive_type not in {2, 3, 5, 6}:
        raise ResourceLockError(
            "resource lock path must be on a qualified local filesystem"
        )


class ResourceLock:
    """Crash-releasing advisory lock for cooperating local workers.

    The lock file is persistent metadata. Cooperating writers must not unlink,
    rename, replace, symlink, or hard-link it while coordinating. Acquisition
    rejects unsafe aliases and verifies that the opened descriptor still names the
    current canonical path before and after the operating-system lock is acquired.

    This is a local-filesystem coordination primitive. It is not a distributed
    execution fence and must never authorize financial order submission.
    """

    def __init__(self, lock_path: str | Path, *, blocking: bool = False) -> None:
        if type(blocking) is not bool:
            raise TypeError("blocking must be bool")
        self.path = Path(lock_path)
        self.blocking = blocking
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise ResourceLockError("resource lock is already held by this lock object")
        _reject_known_remote_lock_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._open_lock_handle()
        try:
            self._validate_handle_identity(handle)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            self._acquire_os_lock(handle)
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
                    "resource lock handle close also failed after unlock failure",
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

    def _open_lock_handle(self) -> BinaryIO:
        """Create/open the canonical lock without create-through-alias races."""

        try:
            return self._open_new_lock_handle()
        except FileExistsError:
            pass
        except OSError as exc:
            raise ResourceLockError("cannot create resource lock path") from exc

        self._validate_existing_lock_path()
        try:
            return self.path.open("r+b")
        except FileNotFoundError as exc:
            raise ResourceLockError(
                "resource lock path changed during acquisition"
            ) from exc
        except OSError as exc:
            raise ResourceLockError("cannot open resource lock path") from exc

    def _open_new_lock_handle(self) -> BinaryIO:
        """Exclusively create the lock without following Windows reparse points."""

        if os.name != "nt":
            return self.path.open("x+b")

        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE

        generic_read = 0x80000000
        generic_write = 0x40000000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        create_new = 1
        file_attribute_normal = 0x00000080
        file_flag_open_reparse_point = 0x00200000
        invalid_handle_value = ctypes.c_void_p(-1).value

        kernel_handle = create_file(
            str(self.path),
            generic_read | generic_write,
            file_share_read | file_share_write | file_share_delete,
            None,
            create_new,
            file_attribute_normal | file_flag_open_reparse_point,
            None,
        )
        if kernel_handle == invalid_handle_value:
            error_code = ctypes.get_last_error()
            if error_code in (80, 183):
                raise FileExistsError(
                    error_code,
                    "resource lock path already exists",
                    str(self.path),
                )
            raise ctypes.WinError(error_code)

        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        try:
            descriptor = msvcrt.open_osfhandle(
                kernel_handle,
                os.O_RDWR | os.O_BINARY,
            )
        except BaseException:
            close_handle(kernel_handle)
            raise
        try:
            return os.fdopen(descriptor, "r+b", closefd=True)
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_existing_lock_path(self) -> None:
        try:
            path_stat = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ResourceLockError("cannot inspect resource lock path") from exc
        self._require_regular_file(path_stat)
        self._require_single_link(path_stat)

    def _validate_handle_identity(self, handle: BinaryIO) -> None:
        """Prove the opened handle is the current canonical single-link file."""

        try:
            opened_before = os.fstat(handle.fileno())
            path_before = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise ResourceLockError(
                "resource lock path changed during acquisition"
            ) from exc
        self._require_regular_file(opened_before)
        self._require_regular_file(path_before)

        try:
            verification_descriptor = _open_read_only_descriptor(self.path)
        except OSError as exc:
            raise ResourceLockError(
                "resource lock path changed during acquisition"
            ) from exc

        final_verification_descriptor: int | None = None
        validation_error: BaseException | None = None
        try:
            try:
                verification_stat = os.fstat(verification_descriptor)
                opened_after = os.fstat(handle.fileno())
                same_open_file = os.path.sameopenfile(
                    handle.fileno(),
                    verification_descriptor,
                )
                path_after = os.stat(self.path, follow_symlinks=False)
                final_verification_descriptor = _open_read_only_descriptor(self.path)
                final_verification_stat = os.fstat(final_verification_descriptor)
                same_final_open_file = os.path.sameopenfile(
                    handle.fileno(),
                    final_verification_descriptor,
                )
            except OSError as exc:
                raise ResourceLockError(
                    "resource lock path changed during acquisition"
                ) from exc

            self._require_regular_file(verification_stat)
            self._require_regular_file(opened_after)
            self._require_regular_file(path_after)
            self._require_regular_file(final_verification_stat)

            if not same_open_file or not same_final_open_file:
                raise ResourceLockError(
                    "resource lock path changed during acquisition"
                )

            self._require_single_link(opened_before)
            self._require_single_link(path_before)
            self._require_single_link(verification_stat)
            self._require_single_link(opened_after)
            self._require_single_link(path_after)
            self._require_single_link(final_verification_stat)
        except BaseException as exc:
            validation_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            for descriptor in (final_verification_descriptor, verification_descriptor):
                if descriptor is None:
                    continue
                try:
                    os.close(descriptor)
                except BaseException as close_error:
                    if validation_error is not None:
                        _add_secondary_failure_note(
                            validation_error,
                            "resource lock verification handle close also failed",
                            close_error,
                        )
                    elif cleanup_error is None:
                        cleanup_error = close_error
                    else:
                        _add_secondary_failure_note(
                            cleanup_error,
                            "another resource lock verification handle close also failed",
                            close_error,
                        )
            if validation_error is None and cleanup_error is not None:
                raise ResourceLockError(
                    "cannot close resource lock verification handle"
                ) from cleanup_error

    @staticmethod
    def _require_regular_file(path_stat: os.stat_result) -> None:
        if not stat.S_ISREG(path_stat.st_mode):
            raise ResourceLockError(
                "resource lock path must be a regular non-symlink file"
            )

    @staticmethod
    def _require_single_link(path_stat: os.stat_result) -> None:
        if path_stat.st_nlink != 1:
            raise ResourceLockError(
                "resource lock path must not have hard-link aliases"
            )

    def _acquire_os_lock(self, handle: BinaryIO) -> None:
        if self.blocking:
            self._lock_handle_blocking(handle)
        else:
            self._lock_handle(handle)

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ResourceLockBusyError(
                        "another process owns the resource lock"
                    ) from exc
                raise ResourceLockError("cannot acquire resource lock") from exc
            return

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ResourceLockBusyError(
                    "another process owns the resource lock"
                ) from exc
            raise ResourceLockError("cannot acquire resource lock") from exc

    @staticmethod
    def _lock_handle_blocking(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise ResourceLockError("cannot acquire resource lock") from exc
            return

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
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
