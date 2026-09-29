from __future__ import annotations

from contextlib import contextmanager
import ctypes
from hashlib import sha256
import importlib
import os
from pathlib import Path
import stat
import sys
from typing import Iterator

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from .resource_lock import ResourceLockBusyError, ResourceLockError

_store = importlib.import_module(f"{__package__}.store")

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_HOST_FSTAT = os.fstat


def _configured_root_key(path: Path) -> str:
    # Identity-independent key: renaming/replacing the directory at this lexical
    # configured path must not create a second coordination domain.
    absolute = os.path.abspath(os.fspath(path))
    if sys.platform == "win32":
        absolute = os.path.normcase(absolute)
    return absolute


def _assert_root_continuity(self) -> None:
    """Prove the configured lexical root still names the retained root identity."""

    if sys.platform == "win32":
        retained = getattr(self, "_namespace_root_handle", None)
        if not retained:
            raise _store.ArtifactIntegrityError(
                "retained artifact store root handle is unavailable"
            )
        expected = _guard._windows_handle_information(
            retained,
            subject="retained artifact store root",
        )
        try:
            current_handle = _guard._open_windows_root_directory(self.root)
        except (OSError, _store.ArtifactIntegrityError) as error:
            raise _store.ArtifactIntegrityError(
                "configured artifact store root is no longer canonical"
            ) from error
        try:
            current = _guard._windows_handle_information(
                current_handle,
                subject="configured artifact store root",
            )
            if not _retained._same_windows_identity(expected, current):
                raise _store.ArtifactIntegrityError(
                    "configured artifact store root changed after initialization"
                )
        finally:
            _guard._close_windows_handle(current_handle)
        return

    retained_fd = getattr(self, "_namespace_root_fd", None)
    if retained_fd is None:
        raise _store.ArtifactIntegrityError(
            "retained artifact store root descriptor is unavailable"
        )
    try:
        # Keep the root identity probe independent from lower-level descriptor
        # read fault injection. Tests and callers may instrument os.fstat around
        # a manifest/object read; that must not silently change which capability
        # proves the retained root generation.
        expected = _HOST_FSTAT(retained_fd)
        current = os.stat(self.root, follow_symlinks=False)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "configured artifact store root is no longer canonical"
        ) from error
    self._reject_reparse_point(current, subject="configured artifact store root")
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise _store.ArtifactIntegrityError(
            "configured artifact store root must remain a canonical directory"
        )
    if not self._same_filesystem_entry(expected, current):
        raise _store.ArtifactIntegrityError(
            "configured artifact store root changed after initialization"
        )


def _canonical_authoritative_root(root: str | Path) -> Path:
    if not isinstance(root, (str, Path)):
        raise TypeError("trusted artifact root must be a string or Path")
    if isinstance(root, str) and not root.strip():
        raise ValueError("trusted artifact root must be non-empty")
    return Path(os.path.abspath(os.fspath(root)))


def _assert_same_root_generation(
    publication_store: object,
    private_store: object,
) -> None:
    if type(publication_store) is not _store.ArtifactStore:
        raise TypeError(
            "publication_store must be the canonical ArtifactStore"
        )
    if type(private_store) is not _store.ArtifactStore:
        raise TypeError("private trusted reader must use canonical ArtifactStore")

    if sys.platform == "win32":
        publication_handle = getattr(
            publication_store,
            "_namespace_root_handle",
            None,
        )
        private_handle = getattr(private_store, "_namespace_root_handle", None)
        if not publication_handle or not private_handle:
            raise _store.ArtifactIntegrityError(
                "artifact root generation handles are unavailable"
            )
        publication_identity = _guard._windows_handle_information(
            publication_handle,
            subject="publication artifact store root",
        )
        private_identity = _guard._windows_handle_information(
            private_handle,
            subject="trusted artifact store root",
        )
        if not _retained._same_windows_identity(
            publication_identity,
            private_identity,
        ):
            raise _store.ArtifactIntegrityError(
                "publication store does not match trusted artifact root"
            )
        return

    publication_fd = getattr(publication_store, "_namespace_root_fd", None)
    private_fd = getattr(private_store, "_namespace_root_fd", None)
    if publication_fd is None or private_fd is None:
        raise _store.ArtifactIntegrityError(
            "artifact root generation descriptors are unavailable"
        )
    try:
        publication_identity = _HOST_FSTAT(publication_fd)
        private_identity = _HOST_FSTAT(private_fd)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact root generation cannot be inspected"
        ) from error
    if (
        publication_identity.st_dev != private_identity.st_dev
        or publication_identity.st_ino != private_identity.st_ino
    ):
        raise _store.ArtifactIntegrityError(
            "publication store does not match trusted artifact root"
        )


def trusted_authenticated_reader(
    authoritative_root: str | Path,
    *,
    publication_store: object | None = None,
):
    """Build a private reader from an independently selected artifact root.

    The root value is the trust input. A publication/convenience ArtifactStore,
    when supplied, is used only to prove that its retained root generation is
    the same generation selected by that independent root. Its mutable path
    attributes and helper methods never select or execute trusted reads.
    """

    root = _canonical_authoritative_root(authoritative_root)
    private_store = _store.ArtifactStore(root)
    if publication_store is not None:
        _assert_same_root_generation(publication_store, private_store)

    canonical_read = _store.ArtifactStore.read_authenticated_snapshot

    def read_snapshot(artifact_id: str):
        return canonical_read(private_store, artifact_id)

    return read_snapshot


def _windows_path_mutex_name(self) -> str:
    key = getattr(self, "_configured_artifact_root_key", None)
    if not isinstance(key, str) or not key:
        raise ResourceLockError("configured artifact store path key is unavailable")
    digest = sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"Global\\AutoTrade-ArtifactPath-{digest}"


@contextmanager
def _windows_path_mutex(self) -> Iterator[None]:
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

    handle = create_mutex(None, 0, _windows_path_mutex_name(self))
    if not handle:
        raise ResourceLockError(
            "cannot create configured artifact-store path mutex"
        ) from ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = wait(handle, 0)
        if result == _WAIT_TIMEOUT:
            raise ResourceLockBusyError(
                "configured artifact-store path authority is busy"
            )
        if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            raise ResourceLockError(
                "cannot acquire configured artifact-store path mutex"
            )
        acquired = True
        yield
    finally:
        primary = sys.exc_info()[1]
        release_error = None
        if acquired and not release(handle):
            release_error = ctypes.WinError(ctypes.get_last_error())
        close_error = None
        if not close(handle):
            close_error = ctypes.WinError(ctypes.get_last_error())
        if primary is None:
            if release_error is not None:
                raise ResourceLockError(
                    "cannot release configured artifact-store path mutex"
                ) from release_error
            if close_error is not None:
                raise ResourceLockError(
                    "cannot close configured artifact-store path mutex"
                ) from close_error
        else:
            for label, error in (
                ("path mutex release also failed", release_error),
                ("path mutex close also failed", close_error),
            ):
                if error is not None:
                    try:
                        primary.add_note(f"{label}: {error}")
                    except BaseException:
                        pass


@contextmanager
def _posix_parent_lock(self) -> Iterator[None]:
    import fcntl

    root_key = getattr(self, "_configured_artifact_root_key", None)
    if not isinstance(root_key, str) or not root_key:
        raise ResourceLockError("configured artifact store path key is unavailable")
    parent = Path(root_key).parent
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        raise ResourceLockError(
            "configured artifact-store parent cannot be opened for coordination"
        ) from error
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {11, 13}:
                raise ResourceLockBusyError(
                    "configured artifact-store path authority is busy"
                ) from error
            raise ResourceLockError(
                "cannot acquire configured artifact-store parent lock"
            ) from error
        acquired = True
        yield
    finally:
        primary = sys.exc_info()[1]
        if acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as error:
                if primary is None:
                    os.close(descriptor)
                    raise ResourceLockError(
                        "cannot release configured artifact-store parent lock"
                    ) from error
                try:
                    primary.add_note(
                        f"configured artifact-store parent unlock also failed: {error}"
                    )
                except BaseException:
                    pass
        os.close(descriptor)


@contextmanager
def _configured_path_coordination(self) -> Iterator[None]:
    if sys.platform == "win32":
        with _windows_path_mutex(self):
            yield
    else:
        with _posix_parent_lock(self):
            yield


def _root_fenced_read(method):
    def wrapped(self, *args, **kwargs):
        _assert_root_continuity(self)
        result = method(self, *args, **kwargs)
        _assert_root_continuity(self)
        return result

    wrapped.__name__ = getattr(method, "__name__", "root_fenced_read")
    wrapped.__doc__ = getattr(method, "__doc__", None)
    return wrapped


def _root_fenced_mutation(method):
    def wrapped(self, *args, **kwargs):
        with _configured_path_coordination(self):
            _assert_root_continuity(self)
            result = method(self, *args, **kwargs)
            _assert_root_continuity(self)
            return result

    wrapped.__name__ = getattr(method, "__name__", "root_fenced_mutation")
    wrapped.__doc__ = getattr(method, "__doc__", None)
    return wrapped


def install_root_authority() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_root_authority_installed", False):
        return

    previous_init = artifact_store.__init__

    def root_authority_init(self, *args, **kwargs):
        previous_init(self, *args, **kwargs)
        self._configured_artifact_root_key = _configured_root_key(self.root)
        _assert_root_continuity(self)

    artifact_store.__init__ = root_authority_init
    artifact_store.load_manifest = _root_fenced_read(artifact_store.load_manifest)
    artifact_store.read_authenticated_snapshot = _root_fenced_read(
        artifact_store.read_authenticated_snapshot
    )
    # read_bytes is intentionally not fenced a second time: its canonical
    # implementation delegates to read_authenticated_snapshot, which already
    # holds the root authority before and after the authenticated read. Keeping
    # only that fence also preserves the delegation contract for test doubles.
    artifact_store.export = _root_fenced_read(artifact_store.export)
    artifact_store.audit = _root_fenced_read(artifact_store.audit)
    artifact_store.publish_bytes = _root_fenced_mutation(
        artifact_store.publish_bytes
    )
    artifact_store.recover_orphans = _root_fenced_mutation(
        artifact_store.recover_orphans
    )
    artifact_store._root_authority_installed = True
