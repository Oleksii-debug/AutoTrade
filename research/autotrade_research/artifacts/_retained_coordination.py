from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import importlib
import os
import sys
import threading
from typing import Iterator

from . import _namespace_guard as _guard
from .resource_lock import ResourceLockBusyError, ResourceLockError

_store = importlib.import_module(f"{__package__}.store")

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


def _windows_mutex_name(store) -> str:
    root_handle = getattr(store, "_namespace_root_handle", None)
    if not root_handle:
        raise ResourceLockError(
            "retained artifact-store root handle unavailable for coordination"
        )
    information = _guard._windows_handle_information(
        root_handle,
        subject="artifact store coordination root",
    )
    return (
        "Global\\AutoTrade-ArtifactStore-"
        f"{information.dwVolumeSerialNumber:08x}-"
        f"{information.nFileIndexHigh:08x}{information.nFileIndexLow:08x}"
    )


@contextmanager
def _windows_root_mutex(store) -> Iterator[None]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    wait.restype = ctypes.c_uint32
    release = kernel32.ReleaseMutex
    release.argtypes = (ctypes.c_void_p,)
    release.restype = ctypes.c_int
    close = kernel32.CloseHandle
    close.argtypes = (ctypes.c_void_p,)
    close.restype = ctypes.c_int

    handle = create_mutex(None, 0, _windows_mutex_name(store))
    if not handle:
        raise ResourceLockError(
            "cannot create retained artifact-store coordination mutex"
        ) from ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = wait(handle, 0)
        if result == _WAIT_TIMEOUT:
            raise ResourceLockBusyError(
                "retained artifact-store coordination lock is busy"
            )
        if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            raise ResourceLockError(
                "cannot acquire retained artifact-store coordination mutex"
            )
        acquired = True
        yield
    finally:
        primary_error = sys.exc_info()[1]
        release_error = None
        if acquired and not release(handle):
            release_error = ctypes.WinError(ctypes.get_last_error())
        close_error = None
        if not close(handle):
            close_error = ctypes.WinError(ctypes.get_last_error())
        if primary_error is None:
            if release_error is not None:
                raise ResourceLockError(
                    "cannot release retained artifact-store coordination mutex"
                ) from release_error
            if close_error is not None:
                raise ResourceLockError(
                    "cannot close retained artifact-store coordination mutex"
                ) from close_error
        else:
            for label, error in (
                ("coordination mutex release also failed", release_error),
                ("coordination mutex close also failed", close_error),
            ):
                if error is not None:
                    try:
                        primary_error.add_note(f"{label}: {error}")
                    except BaseException:
                        pass


@contextmanager
def _posix_root_lock(store) -> Iterator[None]:
    import fcntl

    root_fd = getattr(store, "_namespace_root_fd", None)
    if root_fd is None:
        raise ResourceLockError(
            "retained artifact-store root descriptor unavailable for coordination"
        )
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise ResourceLockBusyError(
                "retained artifact-store coordination lock is busy"
            ) from error
        raise ResourceLockError(
            "cannot acquire retained artifact-store root coordination lock"
        ) from error
    try:
        yield
    finally:
        primary_error = sys.exc_info()[1]
        try:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
        except OSError as error:
            if primary_error is None:
                raise ResourceLockError(
                    "cannot release retained artifact-store root coordination lock"
                ) from error
            try:
                primary_error.add_note(
                    f"artifact-store root coordination unlock also failed: {error}"
                )
            except BaseException:
                pass


@contextmanager
def artifact_store_coordination(store) -> Iterator[None]:
    """Serialize mutations against the retained root filesystem identity.

    Coordination is reentrant within one thread. Public mutation wrappers and
    lower retained-authority implementations can therefore share this one lock
    without reopening the legacy lexical ``.artifact-store.lock`` path or
    prematurely releasing the OS-level root lock on a nested exit.
    """

    thread_lock = getattr(store, "_artifact_store_thread_lock", None)
    if thread_lock is None:
        raise ResourceLockError(
            "artifact-store coordination was not initialized"
        )

    current_thread = threading.get_ident()
    owner = getattr(store, "_artifact_store_coordination_owner", None)
    depth = getattr(store, "_artifact_store_coordination_depth", 0)
    if owner == current_thread and depth > 0:
        store._artifact_store_coordination_depth = depth + 1
        try:
            yield
        finally:
            store._artifact_store_coordination_depth -= 1
        return

    if not thread_lock.acquire(blocking=False):
        raise ResourceLockBusyError(
            "retained artifact-store coordination lock is busy"
        )
    try:
        store._artifact_store_coordination_owner = current_thread
        store._artifact_store_coordination_depth = 1
        try:
            if sys.platform == "win32":
                with _windows_root_mutex(store):
                    yield
            else:
                with _posix_root_lock(store):
                    yield
        finally:
            store._artifact_store_coordination_depth = 0
            store._artifact_store_coordination_owner = None
    finally:
        thread_lock.release()


def install_retained_coordination() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_retained_coordination_installed", False):
        return

    previous_init = artifact_store.__init__
    previous_publish = artifact_store.publish_bytes
    previous_recover = artifact_store.recover_orphans

    def coordinated_init(self, *args, **kwargs):
        previous_init(self, *args, **kwargs)
        self._artifact_store_thread_lock = threading.RLock()
        self._artifact_store_coordination_owner = None
        self._artifact_store_coordination_depth = 0

    def coordinated_publish(self, *args, **kwargs):
        with artifact_store_coordination(self):
            return previous_publish(self, *args, **kwargs)

    def coordinated_recover(self, *args, **kwargs):
        with artifact_store_coordination(self):
            return previous_recover(self, *args, **kwargs)

    artifact_store.__init__ = coordinated_init
    artifact_store.publish_bytes = coordinated_publish
    artifact_store.recover_orphans = coordinated_recover
    artifact_store._retained_coordination_installed = True
