from __future__ import annotations

import ctypes
import importlib
import os
from pathlib import Path
import stat
import sys
from typing import Any
import weakref

_store = importlib.import_module(f"{__package__}.store")


_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_NT_FILE_READ_DATA = 0x00000001
_NT_FILE_LIST_DIRECTORY = 0x00000001
_NT_FILE_READ_ATTRIBUTES = 0x00000080
_NT_SYNCHRONIZE = 0x00100000
_NT_FILE_OPEN = 0x00000001
_NT_FILE_DIRECTORY_FILE = 0x00000001
_NT_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_NT_FILE_NON_DIRECTORY_FILE = 0x00000040
_NT_FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
_NT_FILE_OPEN_REPARSE_POINT = 0x00200000
_NT_OBJ_CASE_INSENSITIVE = 0x00000040


class _WindowsByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTimeLow", ctypes.c_uint32),
        ("ftCreationTimeHigh", ctypes.c_uint32),
        ("ftLastAccessTimeLow", ctypes.c_uint32),
        ("ftLastAccessTimeHigh", ctypes.c_uint32),
        ("ftLastWriteTimeLow", ctypes.c_uint32),
        ("ftLastWriteTimeHigh", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusUnion(ctypes.Union):
    _fields_ = [
        ("Status", ctypes.c_long),
        ("Pointer", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("u", _IoStatusUnion),
        ("Information", ctypes.c_size_t),
    ]


def _validate_component(name: str, *, subject: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise _store.ArtifactIntegrityError(
            f"{subject} contains an unsafe namespace component"
        )
    return name


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _close_windows_handle(handle: int) -> None:
    if not handle or sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


def _windows_handle_information(
    handle: int,
    *,
    subject: str,
) -> _WindowsByHandleFileInformation:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsByHandleFileInformation),
    )
    get_info.restype = ctypes.c_int
    information = _WindowsByHandleFileInformation()
    if not get_info(ctypes.c_void_p(handle), ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    if information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        raise _store.ArtifactIntegrityError(
            f"{subject} must not be a reparse point"
        )
    return information


def _open_windows_root_directory(path: Path) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        _NT_FILE_LIST_DIRECTORY | _NT_FILE_READ_ATTRIBUTES | _NT_SYNCHRONIZE,
        _WINDOWS_FILE_SHARE_READ
        | _WINDOWS_FILE_SHARE_WRITE
        | _WINDOWS_FILE_SHARE_DELETE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
        | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == invalid_handle or handle is None:
        raise ctypes.WinError(ctypes.get_last_error())
    handle_value = int(handle)
    try:
        information = _windows_handle_information(
            handle_value,
            subject="artifact store root",
        )
        if not information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise _store.ArtifactIntegrityError(
                "artifact store root must be a directory"
            )
    except Exception:
        _close_windows_handle(handle_value)
        raise
    return handle_value


def _nt_open_relative_handle(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
    subject: str,
) -> int:
    _validate_component(name, subject=subject)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    )
    nt_create_file.restype = ctypes.c_long

    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        Length=encoded_length,
        MaximumLength=encoded_length + 2,
        Buffer=ctypes.cast(name_buffer, ctypes.c_wchar_p),
    )
    attributes = _ObjectAttributes(
        Length=ctypes.sizeof(_ObjectAttributes),
        RootDirectory=ctypes.c_void_p(parent_handle),
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=_NT_OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = _IoStatusBlock()
    result = ctypes.c_void_p()
    desired_access = _NT_FILE_READ_ATTRIBUTES | _NT_SYNCHRONIZE
    if directory:
        desired_access |= _NT_FILE_LIST_DIRECTORY
        options = (
            _NT_FILE_DIRECTORY_FILE
            | _NT_FILE_SYNCHRONOUS_IO_NONALERT
            | _NT_FILE_OPEN_REPARSE_POINT
            | _NT_FILE_OPEN_FOR_BACKUP_INTENT
        )
    else:
        desired_access |= _NT_FILE_READ_DATA
        options = (
            _NT_FILE_NON_DIRECTORY_FILE
            | _NT_FILE_SYNCHRONOUS_IO_NONALERT
            | _NT_FILE_OPEN_REPARSE_POINT
        )
    status = nt_create_file(
        ctypes.byref(result),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        _WINDOWS_FILE_SHARE_READ
        | _WINDOWS_FILE_SHARE_WRITE
        | _WINDOWS_FILE_SHARE_DELETE,
        _NT_FILE_OPEN,
        options,
        None,
        0,
    )
    if status < 0:
        rtl_error = ntdll.RtlNtStatusToDosError
        rtl_error.argtypes = (ctypes.c_long,)
        rtl_error.restype = ctypes.c_ulong
        raise ctypes.WinError(rtl_error(status))
    handle = int(result.value)
    try:
        information = _windows_handle_information(handle, subject=subject)
        is_directory = bool(
            information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
        )
        if is_directory != directory:
            expected = "directory" if directory else "regular file"
            raise _store.ArtifactIntegrityError(
                f"{subject} must be a {expected}"
            )
    except Exception:
        _close_windows_handle(handle)
        raise
    return handle


def _windows_file_handle_to_descriptor(handle: int) -> int:
    import msvcrt

    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        _close_windows_handle(handle)
        raise


def _bind_namespace_root(store: Any) -> None:
    if sys.platform == "win32":
        handle = _open_windows_root_directory(store.root)
        store._namespace_root_handle = handle
        store._namespace_root_finalizer = weakref.finalize(
            store,
            _close_windows_handle,
            handle,
        )
        return

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_flag:
        store._namespace_root_fd = None
        store._namespace_root_finalizer = None
        return
    try:
        before = os.stat(store.root, follow_symlinks=False)
        store._reject_reparse_point(before, subject="artifact store root")
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise _store.ArtifactIntegrityError(
                "artifact store root must be a canonical directory"
            )
        descriptor = os.open(
            store.root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | directory_flag
            | no_follow,
        )
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact store root could not be bound safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        current = os.stat(store.root, follow_symlinks=False)
        store._reject_reparse_point(
            opened,
            subject="artifact store root descriptor",
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not store._same_filesystem_entry(before, opened)
            or not store._same_filesystem_entry(opened, current)
        ):
            raise _store.ArtifactIntegrityError(
                "artifact store root changed during namespace binding"
            )
    except Exception:
        os.close(descriptor)
        raise
    store._namespace_root_fd = descriptor
    store._namespace_root_finalizer = weakref.finalize(
        store,
        _close_fd,
        descriptor,
    )


def _open_posix_directory_component(
    store: Any,
    parent_fd: int,
    name: str,
    *,
    subject: str,
) -> int:
    _validate_component(name, subject=subject)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    if not no_follow or not directory_flag or os.open not in supports_dir_fd:
        raise _store.ArtifactIntegrityError(
            f"{subject} could not be opened safely: "
            "root-anchored namespace support unavailable"
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | directory_flag
            | no_follow,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            f"{subject} could not be opened safely inside the bound namespace"
        ) from error
    try:
        opened = os.fstat(descriptor)
        store._reject_reparse_point(
            opened,
            subject=f"{subject} namespace component",
        )
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISDIR(opened.st_mode):
            raise _store.ArtifactIntegrityError(
                f"{subject} namespace component must be a directory"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_bound_directory(
    store: Any,
    parts: tuple[str, ...],
    *,
    subject: str,
):
    if not parts:
        raise _store.ArtifactIntegrityError(
            f"{subject} has no bound namespace components"
        )
    if sys.platform == "win32":
        parent_handle = getattr(store, "_namespace_root_handle", None)
        if not parent_handle:
            raise _store.ArtifactIntegrityError(
                f"{subject} could not be opened safely: "
                "held artifact-store root handle unavailable"
            )
        owned_parent: int | None = None
        try:
            for component in parts:
                child = _nt_open_relative_handle(
                    parent_handle,
                    component,
                    directory=True,
                    subject=subject,
                )
                if owned_parent is not None:
                    _close_windows_handle(owned_parent)
                owned_parent = child
                parent_handle = child
            information = _windows_handle_information(
                owned_parent,
                subject=subject,
            )
            return owned_parent, information
        except Exception:
            if owned_parent is not None:
                _close_windows_handle(owned_parent)
            raise

    root_fd = getattr(store, "_namespace_root_fd", None)
    if root_fd is None:
        raise _store.ArtifactIntegrityError(
            f"{subject} could not be opened safely: "
            "held artifact-store root descriptor unavailable"
        )
    parent_fd = root_fd
    owned_parent: int | None = None
    try:
        for component in parts:
            child = _open_posix_directory_component(
                store,
                parent_fd,
                component,
                subject=subject,
            )
            if owned_parent is not None:
                os.close(owned_parent)
            owned_parent = child
            parent_fd = child
        opened = os.fstat(owned_parent)
        return owned_parent, opened
    except Exception:
        if owned_parent is not None:
            os.close(owned_parent)
        raise


def _open_bound_file(
    store: Any,
    parts: tuple[str, ...],
    *,
    subject: str,
):
    if len(parts) < 2:
        raise _store.ArtifactIntegrityError(
            f"{subject} has an incomplete bound namespace"
        )
    parent_parts = parts[:-1]
    name = _validate_component(parts[-1], subject=subject)
    parent, _opened_parent = _open_bound_directory(
        store,
        parent_parts,
        subject=subject,
    )
    if sys.platform == "win32":
        try:
            handle = _nt_open_relative_handle(
                parent,
                name,
                directory=False,
                subject=subject,
            )
        finally:
            _close_windows_handle(parent)
        descriptor = _windows_file_handle_to_descriptor(handle)
        try:
            opened = os.fstat(descriptor)
            store._reject_reparse_point(
                opened,
                subject=f"{subject} descriptor",
            )
            if not stat.S_ISREG(opened.st_mode):
                raise _store.ArtifactIntegrityError(
                    f"{subject} descriptor must be a regular file"
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    if not no_follow or os.open not in supports_dir_fd:
        os.close(parent)
        raise _store.ArtifactIntegrityError(
            f"{subject} could not be opened safely: "
            "root-anchored namespace support unavailable"
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | no_follow
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            f"{subject} could not be opened safely inside the bound namespace"
        ) from error
    finally:
        os.close(parent)
    try:
        opened = os.fstat(descriptor)
        store._reject_reparse_point(
            opened,
            subject=f"{subject} descriptor",
        )
        if not stat.S_ISREG(opened.st_mode):
            raise _store.ArtifactIntegrityError(
                f"{subject} descriptor must be a regular file"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _manifest_parts(store: Any, path: Path) -> tuple[str, str]:
    path = Path(path)
    if path.parent != store.manifests:
        raise _store.ArtifactIntegrityError(
            "artifact manifest path escapes store namespace"
        )
    return (
        "manifests",
        _validate_component(path.name, subject="artifact manifest"),
    )


def _object_parts(store: Any, path: Path) -> tuple[str, str, str, str]:
    path = Path(path)
    digest = path.name
    prefix = path.parent.name
    if (
        path.parent.parent != store.objects
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
        or prefix != digest[:2]
    ):
        raise _store.ArtifactIntegrityError(
            "content-addressed object path escapes store namespace"
        )
    return ("objects", "sha256", prefix, digest)


def _cleanup_directory_parts(
    store: Any,
    directory: Path,
) -> tuple[str, ...]:
    directory = Path(directory)
    if directory == store.staging:
        return ("staging",)
    if directory.parent == store.objects:
        prefix = directory.name
        if len(prefix) == 2 and all(
            ch in "0123456789abcdef" for ch in prefix
        ):
            return ("objects", "sha256", prefix)
    raise _store.ArtifactIntegrityError(
        "artifact cleanup directory escapes store namespace"
    )


def _compatibility_fake_windows_object_open(
    store: Any,
    object_path: Path,
    expected_bytes: int,
):
    descriptor = _store._open_read_only_descriptor(object_path)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _store.ArtifactIntegrityError(
                "artifact object descriptor must be a regular file"
            )
        if opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "artifact object descriptor must not have hard-link aliases"
            )
        if opened.st_size != expected_bytes:
            raise _store.ArtifactIntegrityError(
                "artifact object size mismatch"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _hardened_open_object_descriptor(
    self,
    object_path: Path,
    *,
    expected_bytes: int,
):
    if os.name == "nt" and sys.platform != "win32":
        return _compatibility_fake_windows_object_open(
            self,
            object_path,
            expected_bytes,
        )
    before = self._validate_object_entry(object_path)
    if before.st_size != expected_bytes:
        raise _store.ArtifactIntegrityError(
            "artifact object size mismatch"
        )
    descriptor, opened = _open_bound_file(
        self,
        _object_parts(self, object_path),
        subject="artifact object",
    )
    try:
        if opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "artifact object descriptor must not have hard-link aliases"
            )
        if opened.st_size != expected_bytes:
            raise _store.ArtifactIntegrityError(
                "artifact object size mismatch"
            )
        if not self._same_filesystem_entry(before, opened):
            raise _store.ArtifactIntegrityError(
                "artifact object changed before descriptor read"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _hardened_revalidate_object_descriptor(
    self,
    object_path: Path,
    descriptor: int,
    opened: os.stat_result,
    *,
    expected_bytes: int,
) -> None:
    try:
        held = os.fstat(descriptor)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact object descriptor could not be revalidated"
        ) from error
    self._reject_reparse_point(
        held,
        subject="artifact object descriptor",
    )
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_nlink != 1
        or held.st_size != expected_bytes
        or not self._same_filesystem_entry(opened, held)
    ):
        raise _store.ArtifactIntegrityError(
            "artifact object changed during descriptor read"
        )
    current_hint = self._validate_object_entry(object_path)
    current_descriptor, current = _open_bound_file(
        self,
        _object_parts(self, object_path),
        subject="artifact object",
    )
    try:
        if (
            not self._same_filesystem_entry(held, current_hint)
            or not self._same_filesystem_entry(held, current)
        ):
            raise _store.ArtifactIntegrityError(
                "artifact object path changed during descriptor read"
            )
    finally:
        os.close(current_descriptor)


def _hardened_open_manifest_descriptor(
    self,
    manifest_path: Path,
):
    before = self._validate_manifest_entry(manifest_path)
    descriptor, opened = _open_bound_file(
        self,
        _manifest_parts(self, manifest_path),
        subject="artifact manifest",
    )
    try:
        if opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "artifact manifest descriptor must not have hard-link aliases"
            )
        if (
            not self._same_filesystem_entry(before, opened)
            or before.st_size != opened.st_size
        ):
            raise _store.ArtifactIntegrityError(
                "artifact manifest changed during read"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _hardened_revalidate_manifest_descriptor(
    self,
    manifest_path: Path,
    descriptor: int,
    opened: os.stat_result,
) -> None:
    try:
        held = os.fstat(descriptor)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact manifest descriptor could not be revalidated"
        ) from error
    self._reject_reparse_point(
        held,
        subject="artifact manifest descriptor",
    )
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_nlink != 1
        or not self._same_filesystem_entry(opened, held)
        or self._manifest_descriptor_snapshot(held)
        != self._manifest_descriptor_snapshot(opened)
    ):
        raise _store.ArtifactIntegrityError(
            "artifact manifest changed during read"
        )
    current_hint = self._validate_manifest_entry(manifest_path)
    current_descriptor, current = _open_bound_file(
        self,
        _manifest_parts(self, manifest_path),
        subject="artifact manifest",
    )
    try:
        if (
            not self._same_filesystem_entry(held, current_hint)
            or not self._same_filesystem_entry(held, current)
            or held.st_size != current.st_size
        ):
            raise _store.ArtifactIntegrityError(
                "artifact manifest changed during read"
            )
    finally:
        os.close(current_descriptor)


def _hardened_open_verified_cleanup_directory(
    self,
    directory: Path,
    *,
    subject: str,
):
    if not self._supports_descriptor_relative_cleanup():
        raise _store.ArtifactIntegrityError(
            f"{subject} cleanup lacks descriptor-relative platform support"
        )
    directory = Path(directory)
    if directory == self.staging:
        self._validate_staging_namespace()
    try:
        before = os.stat(directory, follow_symlinks=False)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            f"{subject} cleanup directory cannot be inspected"
        ) from error
    self._reject_reparse_point(
        before,
        subject=f"{subject} cleanup directory",
    )
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise _store.ArtifactIntegrityError(
            f"{subject} cleanup directory is not a canonical directory"
        )
    descriptor, opened = _open_bound_directory(
        self,
        _cleanup_directory_parts(self, directory),
        subject=f"{subject} cleanup",
    )
    try:
        if not self._same_filesystem_entry(before, opened):
            raise _store.ArtifactIntegrityError(
                f"{subject} cleanup directory changed before destructive use"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _hardened_revalidate_cleanup_directory(
    self,
    directory: Path,
    descriptor: int,
    opened: os.stat_result,
    *,
    subject: str,
) -> None:
    try:
        held = os.fstat(descriptor)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            f"{subject} cleanup directory could not be revalidated"
        ) from error
    self._reject_reparse_point(
        held,
        subject=f"{subject} cleanup directory descriptor",
    )
    if (
        not stat.S_ISDIR(held.st_mode)
        or not self._same_filesystem_entry(opened, held)
    ):
        raise _store.ArtifactIntegrityError(
            f"{subject} cleanup directory changed before destructive use"
        )
    current_descriptor, current = _open_bound_directory(
        self,
        _cleanup_directory_parts(self, Path(directory)),
        subject=f"{subject} cleanup",
    )
    try:
        if not self._same_filesystem_entry(held, current):
            raise _store.ArtifactIntegrityError(
                f"{subject} cleanup directory changed before destructive use"
            )
    finally:
        os.close(current_descriptor)


def install_artifact_store_namespace_guards() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(
        artifact_store,
        "_root_anchored_namespace_guard",
        False,
    ):
        return

    original_init = artifact_store.__init__

    def hardened_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        _bind_namespace_root(self)

    artifact_store.__init__ = hardened_init
    artifact_store._open_object_descriptor = (
        _hardened_open_object_descriptor
    )
    artifact_store._revalidate_object_descriptor = (
        _hardened_revalidate_object_descriptor
    )
    artifact_store._open_manifest_descriptor = (
        _hardened_open_manifest_descriptor
    )
    artifact_store._revalidate_manifest_descriptor = (
        _hardened_revalidate_manifest_descriptor
    )
    artifact_store._open_verified_cleanup_directory = (
        _hardened_open_verified_cleanup_directory
    )
    artifact_store._revalidate_cleanup_directory = (
        _hardened_revalidate_cleanup_directory
    )
    artifact_store._root_anchored_namespace_guard = True
