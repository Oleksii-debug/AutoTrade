from __future__ import annotations

import os
import stat

from . import _namespace_guard as _guard
from . import _retained_namespace_hardening as _hardened

_store = _hardened._store
_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", set())


def _open_posix_directory_component_stable(
    store,
    parent_fd: int,
    name: str,
    *,
    subject: str,
) -> int:
    _guard._validate_component(name, subject=subject)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_flag or not _OPEN_SUPPORTS_DIR_FD:
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


def _open_posix_relative_file_stable(
    parent_fd: int,
    name: str,
    *,
    subject: str,
):
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow or not _OPEN_SUPPORTS_DIR_FD:
        raise _store.ArtifactIntegrityError(
            f"{subject} could not be opened safely from retained namespace"
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | no_follow
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            f"{subject} could not be opened safely from retained namespace"
        ) from error
    try:
        opened = os.fstat(descriptor)
        _store.ArtifactStore._reject_reparse_point(
            opened,
            subject=f"{subject} descriptor",
        )
        if not stat.S_ISREG(opened.st_mode):
            raise _store.ArtifactIntegrityError(
                f"{subject} descriptor must be a regular file"
            )
        if opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                f"{subject} descriptor must not have hard-link aliases"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def install_stable_posix_capabilities() -> None:
    if getattr(_store.ArtifactStore, "_stable_posix_capabilities", False):
        return
    _guard._open_posix_directory_component = _open_posix_directory_component_stable
    _hardened._open_posix_relative_file = _open_posix_relative_file_stable
    _store.ArtifactStore._stable_posix_capabilities = True
