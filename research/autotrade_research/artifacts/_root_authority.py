from __future__ import annotations

from contextlib import contextmanager
import ctypes
from hashlib import sha256
import importlib
import os
from pathlib import Path
import stat
import sys
import threading
from typing import Iterator
import weakref

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


def _identity_scalars(identity: object, *, subject: str) -> tuple[int, ...]:
    if sys.platform == "win32":
        required = (
            "dwVolumeSerialNumber",
            "nFileIndexHigh",
            "nFileIndexLow",
        )
        if any(not hasattr(identity, field) for field in required):
            raise _store.ArtifactIntegrityError(
                f"{subject} identity is unavailable"
            )
        return tuple(int(getattr(identity, field)) for field in required)
    if not hasattr(identity, "st_dev") or not hasattr(identity, "st_ino"):
        raise _store.ArtifactIntegrityError(f"{subject} identity is unavailable")
    return (int(identity.st_dev), int(identity.st_ino))


def _immutable_store_generation(store: object) -> tuple[int, ...]:
    """Snapshot root and retained child namespaces into immutable scalars."""

    if type(store) is not _store.ArtifactStore:
        raise TypeError("trusted generation requires canonical ArtifactStore")

    if sys.platform == "win32":
        root_handle = getattr(store, "_namespace_root_handle", None)
        if not root_handle:
            raise _store.ArtifactIntegrityError(
                "retained artifact store root handle is unavailable"
            )
        root_identity = _guard._windows_handle_information(
            root_handle,
            subject="trusted artifact store root",
        )
    else:
        root_descriptor = getattr(store, "_namespace_root_fd", None)
        if root_descriptor is None:
            raise _store.ArtifactIntegrityError(
                "retained artifact store root descriptor is unavailable"
            )
        try:
            root_identity = _HOST_FSTAT(root_descriptor)
        except OSError as error:
            raise _store.ArtifactIntegrityError(
                "trusted artifact root generation cannot be inspected"
            ) from error

    generations = list(
        _identity_scalars(root_identity, subject="trusted artifact root")
    )
    for name in ("manifests", "objects", "staging"):
        identity = getattr(store, f"_retained_{name}_identity", None)
        if identity is None:
            raise _store.ArtifactIntegrityError(
                f"retained artifact {name} namespace identity is unavailable"
            )
        generations.extend(
            _identity_scalars(
                identity,
                subject=f"trusted artifact {name} namespace",
            )
        )
    return tuple(generations)


def _path_directory_identity(path: Path, *, subject: str) -> tuple[int, ...]:
    """Read one lexical directory identity without creating filesystem state."""

    if sys.platform == "win32":
        try:
            handle = _guard._open_windows_root_directory(path)
        except (OSError, _store.ArtifactIntegrityError) as error:
            raise _store.ArtifactIntegrityError(
                f"{subject} is unavailable"
            ) from error
        try:
            identity = _guard._windows_handle_information(
                handle,
                subject=subject,
            )
            return _identity_scalars(identity, subject=subject)
        finally:
            _guard._close_windows_handle(handle)

    try:
        identity = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            f"{subject} is unavailable"
        ) from error
    _store.ArtifactStore._reject_reparse_point(identity, subject=subject)
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
        raise _store.ArtifactIntegrityError(
            f"{subject} must remain a canonical directory"
        )
    return _identity_scalars(identity, subject=subject)


def _immutable_configured_generation(root_key: str) -> tuple[int, ...]:
    root = Path(root_key)
    namespaces = (
        ("root", root),
        ("manifests", root / "manifests"),
        ("objects", root / "objects" / "sha256"),
        ("staging", root / "staging"),
    )
    generations: list[int] = []
    for name, path in namespaces:
        generations.extend(
            _path_directory_identity(
                path,
                subject=f"configured artifact {name} namespace",
            )
        )
    return tuple(generations)


def _assert_expected_generation(
    observed: tuple[int, ...],
    expected: tuple[int, ...],
) -> None:
    if observed != expected:
        raise _store.ArtifactIntegrityError(
            "configured artifact namespace generation changed after trusted reader construction"
        )


_READER_CAPABILITY_LOCK = threading.RLock()
_READER_CAPABILITIES: dict[int, tuple[str, tuple[int, ...]]] = {}
_READER_PIN_NAMES = ("root", "manifests", "objects", "staging")


def _duplicate_windows_handle(handle: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = ctypes.c_void_p
    duplicate_handle = kernel32.DuplicateHandle
    duplicate_handle.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    )
    duplicate_handle.restype = ctypes.c_int
    current_process = get_current_process()
    duplicated = ctypes.c_void_p()
    duplicate_same_access = 0x00000002
    if not duplicate_handle(
        current_process,
        ctypes.c_void_p(handle),
        current_process,
        ctypes.byref(duplicated),
        0,
        0,
        duplicate_same_access,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not duplicated.value:
        raise _store.ArtifactIntegrityError(
            "trusted artifact namespace handle duplication failed"
        )
    return int(duplicated.value)


def _close_generation_pins(pins: tuple[int, ...]) -> None:
    for pin in pins:
        try:
            if sys.platform == "win32":
                _guard._close_windows_handle(pin)
            else:
                os.close(pin)
        except OSError:
            pass


def _pinned_generation(pins: tuple[int, ...]) -> tuple[int, ...]:
    if type(pins) is not tuple or len(pins) != len(_READER_PIN_NAMES):
        raise _store.ArtifactIntegrityError(
            "trusted artifact generation capabilities are incomplete"
        )
    generations: list[int] = []
    for name, pin in zip(_READER_PIN_NAMES, pins):
        if type(pin) is not int or pin < 0:
            raise _store.ArtifactIntegrityError(
                f"trusted artifact {name} capability is invalid"
            )
        if sys.platform == "win32":
            identity = _guard._windows_handle_information(
                pin,
                subject=f"pinned trusted artifact {name} namespace",
            )
        else:
            try:
                identity = _HOST_FSTAT(pin)
            except OSError as error:
                raise _store.ArtifactIntegrityError(
                    f"pinned trusted artifact {name} namespace is unavailable"
                ) from error
        generations.extend(
            _identity_scalars(
                identity,
                subject=f"pinned trusted artifact {name} namespace",
            )
        )
    return tuple(generations)


def _duplicate_capability(capability: int, *, subject: str) -> int:
    if type(capability) is not int or capability < 0:
        raise _store.ArtifactIntegrityError(
            f"{subject} capability is unavailable"
        )
    if sys.platform == "win32":
        try:
            return _duplicate_windows_handle(capability)
        except OSError as error:
            raise _store.ArtifactIntegrityError(
                f"{subject} capability cannot be duplicated"
            ) from error

    duplicated: int | None = None
    try:
        duplicated = os.dup(capability)
        os.set_inheritable(duplicated, False)
        return duplicated
    except OSError as error:
        if duplicated is not None:
            try:
                os.close(duplicated)
            except OSError:
                pass
        raise _store.ArtifactIntegrityError(
            f"{subject} capability cannot be duplicated"
        ) from error


def _duplicate_generation_pins(pins: tuple[int, ...]) -> tuple[int, ...]:
    if type(pins) is not tuple or len(pins) != len(_READER_PIN_NAMES):
        raise _store.ArtifactIntegrityError(
            "trusted artifact generation capabilities are incomplete"
        )
    duplicated: list[int] = []
    try:
        for name, capability in zip(_READER_PIN_NAMES, pins):
            duplicated.append(
                _duplicate_capability(
                    capability,
                    subject=f"trusted artifact {name}",
                )
            )
        return tuple(duplicated)
    except BaseException:
        _close_generation_pins(tuple(duplicated))
        raise


def _duplicate_store_generation_pins(store: object) -> tuple[int, ...]:
    if type(store) is not _store.ArtifactStore:
        raise TypeError("trusted generation requires canonical ArtifactStore")
    attributes = (
        "_namespace_root_handle" if sys.platform == "win32" else "_namespace_root_fd",
        "_retained_manifests_handle" if sys.platform == "win32" else "_retained_manifests_fd",
        "_retained_objects_handle" if sys.platform == "win32" else "_retained_objects_fd",
        "_retained_staging_handle" if sys.platform == "win32" else "_retained_staging_fd",
    )
    source: list[int] = []
    for name, attribute in zip(_READER_PIN_NAMES, attributes):
        capability = getattr(store, attribute, None)
        if capability is None or (
            sys.platform == "win32" and not capability
        ):
            raise _store.ArtifactIntegrityError(
                f"retained artifact {name} capability is unavailable"
            )
        source.append(int(capability))

    pinned = _duplicate_generation_pins(tuple(source))
    try:
        _assert_expected_generation(
            _pinned_generation(pinned),
            _immutable_store_generation(store),
        )
        return pinned
    except BaseException:
        _close_generation_pins(pinned)
        raise


def _execution_store_from_pins(
    root_key: str,
    pins: tuple[int, ...],
) -> tuple[object, tuple[int, ...]]:
    """Build one exact read-only ArtifactStore view without running __init__.

    The execution view owns duplicates of the already-authoritative namespace
    capabilities. Constructing it cannot create, rename or populate filesystem
    state, so a namespace swap between lexical preflight and execution binding
    remains fail-before-touch.
    """

    execution_pins = _duplicate_generation_pins(pins)
    try:
        store = object.__new__(_store.ArtifactStore)
        store.root = Path(root_key)
        store._export_authorizer = None
        store.objects = store.root / "objects" / "sha256"
        store.manifests = store.root / "manifests"
        store.staging = store.root / "staging"
        store.lock_path = store.root / ".artifact-store.lock"
        store._configured_artifact_root_key = root_key

        if sys.platform == "win32":
            (
                store._namespace_root_handle,
                store._retained_manifests_handle,
                store._retained_objects_handle,
                store._retained_staging_handle,
            ) = execution_pins
            identities = (
                _guard._windows_handle_information(
                    execution_pins[1],
                    subject="trusted artifact manifests namespace",
                ),
                _guard._windows_handle_information(
                    execution_pins[2],
                    subject="trusted artifact objects namespace",
                ),
                _guard._windows_handle_information(
                    execution_pins[3],
                    subject="trusted artifact staging namespace",
                ),
            )
        else:
            (
                store._namespace_root_fd,
                store._retained_manifests_fd,
                store._retained_objects_fd,
                store._retained_staging_fd,
            ) = execution_pins
            try:
                identities = (
                    _HOST_FSTAT(execution_pins[1]),
                    _HOST_FSTAT(execution_pins[2]),
                    _HOST_FSTAT(execution_pins[3]),
                )
            except OSError as error:
                raise _store.ArtifactIntegrityError(
                    "trusted execution namespace capability is unavailable"
                ) from error

        (
            store._retained_manifests_identity,
            store._retained_objects_identity,
            store._retained_staging_identity,
        ) = identities
        _assert_expected_generation(
            _immutable_store_generation(store),
            _pinned_generation(pins),
        )
        return store, execution_pins
    except BaseException:
        _close_generation_pins(execution_pins)
        raise


def _release_reader_capability(reader_id: int) -> None:
    with _READER_CAPABILITY_LOCK:
        state = _READER_CAPABILITIES.pop(reader_id, None)
    if state is not None:
        _root_key, pins = state
        _close_generation_pins(pins)


def _reader_capability(reader: object) -> tuple[str, tuple[int, ...]]:
    if type(reader) is not _TrustedAuthenticatedReader:
        raise TypeError("trusted reader capability type is invalid")
    with _READER_CAPABILITY_LOCK:
        state = _READER_CAPABILITIES.get(id(reader))
    if state is None:
        raise _store.ArtifactIntegrityError(
            "trusted artifact reader capability is no longer available"
        )
    return state


class _TrustedAuthenticatedReader(str):
    """Immutable token for module-owned retained namespace capabilities."""

    __slots__ = ("__weakref__",)

    def __new__(cls):
        if cls is not _TrustedAuthenticatedReader:
            raise TypeError("trusted reader type is sealed")
        return str.__new__(cls, "autotrade-trusted-artifact-reader")

    def __call__(self, artifact_id: str):
        root_key, pins = _reader_capability(self)
        expected_generation = _pinned_generation(pins)

        configured_generation = _immutable_configured_generation(root_key)
        _assert_expected_generation(configured_generation, expected_generation)

        private_store, execution_pins = _execution_store_from_pins(
            root_key,
            pins,
        )
        try:
            # Close the preflight/binding race without constructing or mutating
            # anything at the configured path. Canonical read wrappers perform
            # their own retained root/child continuity fences after this point.
            configured_generation = _immutable_configured_generation(root_key)
            _assert_expected_generation(
                configured_generation,
                expected_generation,
            )
            canonical_read = _store.ArtifactStore.read_authenticated_snapshot
            return canonical_read(private_store, artifact_id)
        finally:
            _close_generation_pins(execution_pins)


def trusted_authenticated_reader(
    authoritative_root: str | Path,
    *,
    publication_store: object | None = None,
):
    """Build a lifetime-pinned authenticated artifact read capability.

    The returned immutable token exposes neither a mutable ArtifactStore nor raw
    OS namespace capabilities. Duplicated retained root/manifests/objects/staging
    descriptors or handles live in module-owned state until the reader is
    garbage-collected. Those live capabilities prevent generation identity reuse
    while the reader remains authoritative.

    Every read compares the current lexical namespace against identities derived
    from the still-open pins, then builds an exact no-init execution view from
    duplicated pins. No ArtifactStore constructor or filesystem mutation occurs
    between authority checks and authenticated snapshot execution.
    """

    root = _canonical_authoritative_root(authoritative_root)
    initial_store = _store.ArtifactStore(root)
    if publication_store is not None:
        _assert_same_root_generation(publication_store, initial_store)

    root_key = _configured_root_key(root)
    pins = _duplicate_store_generation_pins(initial_store)
    del initial_store

    reader = _TrustedAuthenticatedReader()
    reader_id = id(reader)
    try:
        with _READER_CAPABILITY_LOCK:
            if reader_id in _READER_CAPABILITIES:
                raise _store.ArtifactIntegrityError(
                    "trusted reader capability identity collision"
                )
            _READER_CAPABILITIES[reader_id] = (root_key, pins)
        weakref.finalize(reader, _release_reader_capability, reader_id)
        return reader
    except BaseException:
        with _READER_CAPABILITY_LOCK:
            _READER_CAPABILITIES.pop(reader_id, None)
        _close_generation_pins(pins)
        raise


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
