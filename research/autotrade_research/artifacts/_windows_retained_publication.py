from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import ctypes
import importlib
import os
import sys
from typing import Any
from uuid import uuid4

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from . import _retained_publication as _publication

_store = importlib.import_module(f"{__package__}.store")

_FILE_ADD_FILE = 0x00000002
_FILE_ADD_SUBDIRECTORY = 0x00000004
_FILE_DELETE_CHILD = 0x00000040
_FILE_WRITE_DATA = 0x00000002
_FILE_WRITE_ATTRIBUTES = 0x00000100
_DELETE = 0x00010000
_FILE_CREATE = 0x00000002
_FILE_OPEN = 0x00000001
_FILE_OPEN_IF = 0x00000003
_FILE_RENAME_INFO_CLASS = 3
_FILE_DISPOSITION_INFO_CLASS = 4


def _raise_ntstatus(status: int) -> None:
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    rtl_error = ntdll.RtlNtStatusToDosError
    rtl_error.argtypes = (ctypes.c_long,)
    rtl_error.restype = ctypes.c_ulong
    raise ctypes.WinError(rtl_error(status))


def _nt_create_relative(
    parent_handle: int,
    name: str,
    *,
    directory: bool,
    disposition: int,
    desired_access: int,
    subject: str,
) -> int:
    _guard._validate_component(name, subject=subject)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(_guard._ObjectAttributes),
        ctypes.POINTER(_guard._IoStatusBlock),
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
    unicode_name = _guard._UnicodeString(
        Length=encoded_length,
        MaximumLength=encoded_length + 2,
        Buffer=ctypes.cast(name_buffer, ctypes.c_wchar_p),
    )
    attributes = _guard._ObjectAttributes(
        Length=ctypes.sizeof(_guard._ObjectAttributes),
        RootDirectory=ctypes.c_void_p(parent_handle),
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=_guard._NT_OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = _guard._IoStatusBlock()
    result = ctypes.c_void_p()

    options = (
        _guard._NT_FILE_SYNCHRONOUS_IO_NONALERT
        | _guard._NT_FILE_OPEN_REPARSE_POINT
    )
    if directory:
        options |= (
            _guard._NT_FILE_DIRECTORY_FILE
            | _guard._NT_FILE_OPEN_FOR_BACKUP_INTENT
        )
    else:
        options |= _guard._NT_FILE_NON_DIRECTORY_FILE

    status = nt_create_file(
        ctypes.byref(result),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        _guard._WINDOWS_FILE_SHARE_READ
        | _guard._WINDOWS_FILE_SHARE_WRITE
        | _guard._WINDOWS_FILE_SHARE_DELETE,
        disposition,
        options,
        None,
        0,
    )
    if status < 0:
        _raise_ntstatus(status)
    handle = int(result.value)
    try:
        information = _guard._windows_handle_information(handle, subject=subject)
        is_directory = bool(
            information.dwFileAttributes
            & _guard._WINDOWS_FILE_ATTRIBUTE_DIRECTORY
        )
        if is_directory != directory:
            expected = "directory" if directory else "regular file"
            raise _store.ArtifactIntegrityError(
                f"{subject} must be a {expected}"
            )
    except Exception:
        _guard._close_windows_handle(handle)
        raise
    return handle


def _open_mutation_directory(
    self,
    parts: tuple[str, ...],
    *,
    retained_name: str,
) -> int:
    root_handle = getattr(self, "_namespace_root_handle", None)
    if not root_handle:
        raise _store.ArtifactIntegrityError(
            "held artifact-store root handle unavailable for publication"
        )
    parent = root_handle
    owned_parent = None
    desired = (
        _guard._NT_FILE_LIST_DIRECTORY
        | _FILE_ADD_FILE
        | _FILE_ADD_SUBDIRECTORY
        | _FILE_DELETE_CHILD
        | _guard._NT_FILE_READ_ATTRIBUTES
        | _guard._NT_SYNCHRONIZE
    )
    try:
        for component in parts:
            child = _nt_create_relative(
                parent,
                component,
                directory=True,
                disposition=_FILE_OPEN,
                desired_access=desired,
                subject=f"artifact {retained_name} publication namespace",
            )
            if owned_parent is not None:
                _guard._close_windows_handle(owned_parent)
            owned_parent = child
            parent = child
        current_identity = _guard._windows_handle_information(
            owned_parent,
            subject=f"artifact {retained_name} publication namespace",
        )
        expected_identity = getattr(self, f"_retained_{retained_name}_identity")
        if not _retained._same_windows_identity(
            expected_identity,
            current_identity,
        ):
            raise _store.ArtifactIntegrityError(
                f"artifact {retained_name} namespace changed after store initialization"
            )
        result = owned_parent
        owned_parent = None
        return result
    finally:
        if owned_parent is not None:
            _guard._close_windows_handle(owned_parent)


def _open_or_create_prefix(parent: int, prefix: str) -> int:
    desired = (
        _guard._NT_FILE_LIST_DIRECTORY
        | _FILE_ADD_FILE
        | _FILE_DELETE_CHILD
        | _guard._NT_FILE_READ_ATTRIBUTES
        | _guard._NT_SYNCHRONIZE
    )
    return _nt_create_relative(
        parent,
        prefix,
        directory=True,
        disposition=_FILE_OPEN_IF,
        desired_access=desired,
        subject="artifact object prefix",
    )


def _create_temp_fd(parent: int, *, prefix: str) -> tuple[str, int]:
    import msvcrt

    desired = (
        _guard._NT_FILE_READ_DATA
        | _FILE_WRITE_DATA
        | _guard._NT_FILE_READ_ATTRIBUTES
        | _FILE_WRITE_ATTRIBUTES
        | _DELETE
        | _guard._NT_SYNCHRONIZE
    )
    for _attempt in range(32):
        name = f".{prefix}-{uuid4().hex}.tmp"
        try:
            handle = _nt_create_relative(
                parent,
                name,
                directory=False,
                disposition=_FILE_CREATE,
                desired_access=desired,
                subject="artifact publication temporary file",
            )
        except FileExistsError:
            continue
        try:
            descriptor = msvcrt.open_osfhandle(
                handle,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            _guard._close_windows_handle(handle)
            raise
        return name, descriptor
    raise _store.ArtifactIntegrityError(
        "artifact publication could not allocate a unique temporary name"
    )


def _rename_fd(
    descriptor: int,
    target_parent: int,
    target_name: str,
    *,
    replace: bool,
) -> None:
    import msvcrt

    _guard._validate_component(
        target_name,
        subject="artifact publication target",
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_info.restype = ctypes.c_int

    class _RenameHeader(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_int),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_uint32),
        ]

    encoded = target_name.encode("utf-16-le")
    file_name_offset = _RenameHeader.FileNameLength.offset + ctypes.sizeof(
        ctypes.c_uint32
    )
    buffer = ctypes.create_string_buffer(file_name_offset + len(encoded))
    header = _RenameHeader.from_buffer(buffer)
    header.ReplaceIfExists = int(replace)
    header.RootDirectory = ctypes.c_void_p(target_parent)
    header.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + file_name_offset,
        encoded,
        len(encoded),
    )
    handle = msvcrt.get_osfhandle(descriptor)
    if not set_info(
        ctypes.c_void_p(handle),
        _FILE_RENAME_INFO_CLASS,
        ctypes.byref(buffer),
        len(buffer),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _delete_fd_on_close(descriptor: int) -> None:
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_info.restype = ctypes.c_int

    class _Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_int)]

    disposition = _Disposition(DeleteFile=1)
    handle = msvcrt.get_osfhandle(descriptor)
    if not set_info(
        ctypes.c_void_p(handle),
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _publish_temp_fd(
    descriptor: int,
    *,
    target_parent: int,
    target_name: str,
    replace: bool,
) -> None:
    try:
        _rename_fd(
            descriptor,
            target_parent,
            target_name,
            replace=replace,
        )
    except BaseException:
        try:
            _delete_fd_on_close(descriptor)
        except BaseException:
            pass
        raise
    finally:
        os.close(descriptor)


def _publish_object_windows(self, *, digest: str, data: bytes) -> None:
    objects = _open_mutation_directory(
        self,
        ("objects", "sha256"),
        retained_name="objects",
    )
    staging = _open_mutation_directory(
        self,
        ("staging",),
        retained_name="staging",
    )
    prefix = None
    descriptor = None
    try:
        prefix = _open_or_create_prefix(objects, digest[:2])
        try:
            existing = _guard._nt_open_relative_handle(
                prefix,
                digest,
                directory=False,
                subject="artifact object",
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            _guard._close_windows_handle(existing)
            self._verify_manifest_object(
                {"sha256": f"sha256:{digest}", "bytes": len(data)}
            )
            return

        _name, descriptor = _create_temp_fd(staging, prefix="artifact")
        _publication._write_all(descriptor, data)
        _publication._verify_staged_descriptor(
            descriptor,
            expected_digest=digest,
            expected_bytes=len(data),
        )
        _retained._assert_directory_continuity(self, ("staging",), "staging")
        _retained._assert_directory_continuity(
            self,
            ("objects", "sha256"),
            "objects",
        )
        try:
            _publish_temp_fd(
                descriptor,
                target_parent=prefix,
                target_name=digest,
                replace=False,
            )
            descriptor = None
        except OSError:
            descriptor = None
            self._verify_manifest_object(
                {"sha256": f"sha256:{digest}", "bytes": len(data)}
            )
            return

        self._verify_manifest_object(
            {"sha256": f"sha256:{digest}", "bytes": len(data)}
        )
    finally:
        if descriptor is not None:
            try:
                _delete_fd_on_close(descriptor)
            except BaseException:
                pass
            os.close(descriptor)
        if prefix is not None:
            _guard._close_windows_handle(prefix)
        _guard._close_windows_handle(staging)
        _guard._close_windows_handle(objects)


def _publish_manifest_windows(
    self,
    *,
    manifest: dict[str, Any],
    replace_existing: bool,
) -> None:
    manifests = _open_mutation_directory(
        self,
        ("manifests",),
        retained_name="manifests",
    )
    descriptor = None
    try:
        payload = _publication._json_bytes(manifest)
        _name, descriptor = _create_temp_fd(
            manifests,
            prefix=f"manifest-{manifest['artifact_id']}",
        )
        _publication._write_all(descriptor, payload)
        _publication._verify_staged_descriptor(
            descriptor,
            expected_digest=sha256(payload).hexdigest(),
            expected_bytes=len(payload),
        )
        _retained._assert_directory_continuity(
            self,
            ("manifests",),
            "manifests",
        )
        _publish_temp_fd(
            descriptor,
            target_parent=manifests,
            target_name=f"{manifest['artifact_id']}.json",
            replace=replace_existing,
        )
        descriptor = None
    finally:
        if descriptor is not None:
            try:
                _delete_fd_on_close(descriptor)
            except BaseException:
                pass
            os.close(descriptor)
        _guard._close_windows_handle(manifests)


def _publish_bytes_windows(
    self,
    *,
    artifact_id: str,
    data: bytes,
    media_type: str,
    rights: dict[str, Any],
    source_refs: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_id = self._artifact_id(artifact_id)
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if not isinstance(media_type, str) or not media_type.strip():
        raise ValueError("media_type is required")
    normalized_rights = self._validate_rights(rights)
    sources = list(source_refs or [])
    if not all(isinstance(item, str) and item for item in sources):
        raise ValueError("source_refs must contain non-empty strings")
    meta = dict(metadata or {})
    digest = sha256(data).hexdigest()

    with _store.ResourceLock(self.lock_path):
        manifest_path = self._manifest_path(normalized_id)
        self._validate_manifest_namespace(manifest_path)
        self._validate_staging_namespace()
        _retained._assert_all_continuity(self)

        try:
            existing = self.load_manifest(normalized_id)
        except FileNotFoundError:
            existing = None

        immutable = {
            "artifact_id": normalized_id,
            "sha256": f"sha256:{digest}",
            "bytes": len(data),
            "media_type": media_type,
            "rights": normalized_rights,
            "source_refs": sources,
            "metadata": meta,
        }
        if existing is not None:
            if any(existing.get(key) != value for key, value in immutable.items()):
                raise _store.ArtifactConflict(
                    "artifact_id is already committed with different content or metadata"
                )
            self._verify_manifest_object(existing)
            if not _store._verify_manifest_integrity(existing, required=False):
                rebound = {
                    "schema_version": self.SCHEMA_VERSION,
                    **immutable,
                    "created_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
                rebound["manifest_hash"] = _store._manifest_integrity_hash(rebound)
                _publish_manifest_windows(
                    self,
                    manifest=rebound,
                    replace_existing=True,
                )
                _retained._assert_all_continuity(self)
                return rebound
            return existing

        _publish_object_windows(self, digest=digest, data=data)
        _retained._assert_all_continuity(self)

        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            **immutable,
            "created_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        manifest["manifest_hash"] = _store._manifest_integrity_hash(manifest)
        _publish_manifest_windows(
            self,
            manifest=manifest,
            replace_existing=False,
        )
        _retained._assert_all_continuity(self)
        return manifest


def install_windows_retained_publication() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_retained_publication_windows_bound", False):
        return
    previous_publish = artifact_store.publish_bytes

    def cross_platform_publish(self, *args, **kwargs):
        if sys.platform == "win32":
            return _publish_bytes_windows(self, *args, **kwargs)
        return previous_publish(self, *args, **kwargs)

    artifact_store.publish_bytes = cross_platform_publish
    artifact_store._retained_publication_windows_bound = True
