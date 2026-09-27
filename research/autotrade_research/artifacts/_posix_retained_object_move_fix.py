from __future__ import annotations

import ctypes
import errno
import os
import sys

from . import _retained_namespace as _retained
from . import _retained_publication as _publication
from . import _retained_publication_hardening as _hardening

_store = _hardening._store
_RENAME_NOREPLACE = 1


def _rename_noreplace_between_dirs_posix(
    source_parent_fd: int,
    source: str,
    target_parent_fd: int,
    target: str,
) -> bool:
    """Atomically move a staged file into a retained prefix without replacement."""
    if not sys.platform.startswith("linux"):
        raise _store.ArtifactIntegrityError(
            "artifact object publication lacks crash-safe cross-directory no-replace rename support"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise _store.ArtifactIntegrityError(
            "artifact object publication lacks crash-safe cross-directory no-replace rename support"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source),
        target_parent_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        return False
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EXDEV}:
        raise _store.ArtifactIntegrityError(
            "artifact object publication lacks crash-safe cross-directory no-replace rename support"
        )
    raise OSError(error_number, os.strerror(error_number), target)


def _publish_object_posix_atomic(
    self,
    *,
    digest: str,
    data: bytes,
):
    prefix = digest[:2]
    prefix_fd = _publication._ensure_object_prefix_posix(self, prefix)
    prefix_identity = os.fstat(prefix_fd)
    try:
        existing = _publication._entry_stat(prefix_fd, digest)
        if existing is not None:
            _hardening._verify_bound_object_bytes(
                self,
                prefix_fd,
                digest,
                expected_bytes=len(data),
            )
            _hardening._assert_prefix_identity(self, prefix, prefix_identity)
            return prefix, prefix_identity

        _retained._assert_directory_continuity(self, ("staging",), "staging")
        temporary_name = None
        descriptor = None
        try:
            temporary_name, descriptor = _publication._open_temp_posix(
                self._retained_staging_fd,
                prefix="artifact",
            )
            _publication._write_all(descriptor, data)
            _publication._verify_staged_descriptor(
                descriptor,
                expected_digest=digest,
                expected_bytes=len(data),
            )
            os.close(descriptor)
            descriptor = None

            _retained._assert_directory_continuity(
                self,
                ("staging",),
                "staging",
            )
            _hardening._assert_prefix_identity(self, prefix, prefix_identity)
            moved_new_object = _rename_noreplace_between_dirs_posix(
                self._retained_staging_fd,
                temporary_name,
                prefix_fd,
                digest,
            )
            if moved_new_object:
                temporary_name = None
                # These retained descriptor fsyncs are the durability authority
                # for the cross-directory rename. They cover both the target
                # insertion and source removal without re-resolving namespace.
                _publication._sync_directory_fd(prefix_fd)
                _publication._sync_directory_fd(self._retained_staging_fd)
            else:
                _publication._safe_unlink(
                    self._retained_staging_fd,
                    temporary_name,
                )
                temporary_name = None
                _publication._sync_directory_fd(self._retained_staging_fd)

            _hardening._verify_bound_object_bytes(
                self,
                prefix_fd,
                digest,
                expected_bytes=len(data),
            )
            if moved_new_object:
                # Compatibility/qualification probe only. The retained fsyncs
                # above already established durability. If a namespace race has
                # detached the lexical prefix, do not let that stale pathname
                # veto or redirect retained authority. Other I/O errors still
                # propagate so durability fault-injection remains effective.
                try:
                    _store.sync_parent_directory(self._object_path(digest))
                except FileNotFoundError:
                    pass
            _hardening._assert_prefix_identity(self, prefix, prefix_identity)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name is not None:
                _publication._safe_unlink(
                    self._retained_staging_fd,
                    temporary_name,
                )
    finally:
        os.close(prefix_fd)
    return prefix, prefix_identity


def install_posix_retained_object_move_fix() -> None:
    if getattr(_store.ArtifactStore, "_posix_retained_object_move_fix", False):
        return
    _hardening._publish_object_posix = _publish_object_posix_atomic
    _publication._publish_object_posix = _publish_object_posix_atomic
    _store.ArtifactStore._posix_retained_object_move_fix = True
