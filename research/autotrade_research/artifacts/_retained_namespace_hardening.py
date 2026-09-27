from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat
import sys

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained

_store = importlib.import_module(f"{__package__}.store")


def _path_without_flavour_switch(path: Path) -> Path:
    # Some portability regressions deliberately patch os.name. Reconstructing
    # pathlib.Path while that patch is active asks pathlib for a WindowsPath on
    # POSIX. Existing concrete paths already carry the correct host flavour.
    return path if isinstance(path, Path) else Path(path)


def _open_posix_relative_file(
    parent_fd: int,
    name: str,
    *,
    subject: str,
) -> tuple[int, os.stat_result]:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow or os.open not in getattr(os, "supports_dir_fd", set()):
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


def _open_manifest_descriptor(self, manifest_path: Path):
    path = _path_without_flavour_switch(manifest_path)
    name = _retained._validate_manifest_name(self, path)
    before = self._validate_manifest_entry(path)
    _retained._assert_directory_continuity(
        self,
        ("manifests",),
        "manifests",
    )

    if sys.platform == "win32":
        parent = getattr(self, "_retained_manifests_handle", None)
        if not parent:
            raise _store.ArtifactIntegrityError(
                "retained manifest namespace handle is unavailable"
            )
        handle = _guard._nt_open_relative_handle(
            parent,
            name,
            directory=False,
            subject="artifact manifest",
        )
        descriptor = _guard._windows_file_handle_to_descriptor(handle)
        try:
            opened = os.fstat(descriptor)
            self._reject_reparse_point(
                opened,
                subject="artifact manifest descriptor",
            )
            if not stat.S_ISREG(opened.st_mode):
                raise _store.ArtifactIntegrityError(
                    "artifact manifest descriptor must be a regular file"
                )
            if opened.st_nlink != 1:
                raise _store.ArtifactIntegrityError(
                    "artifact manifest descriptor must not have hard-link aliases"
                )
            if (
                not self._same_filesystem_entry(before, opened)
                or before.st_size != opened.st_size
            ):
                try:
                    self._validate_manifest_entry(path)
                except (OSError, _store.ArtifactIntegrityError):
                    pass
                raise _store.ArtifactIntegrityError(
                    "artifact manifest changed during read"
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened

    descriptor, opened = _open_posix_relative_file(
        self._retained_manifests_fd,
        name,
        subject="artifact manifest",
    )
    try:
        if (
            not self._same_filesystem_entry(before, opened)
            or before.st_size != opened.st_size
        ):
            try:
                self._validate_manifest_entry(path)
            except (OSError, _store.ArtifactIntegrityError):
                pass
            raise _store.ArtifactIntegrityError(
                "artifact manifest changed during read"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _open_object_descriptor(
    self,
    object_path: Path,
    *,
    expected_bytes: int,
):
    path = _path_without_flavour_switch(object_path)
    prefix, digest = _retained._validate_object_name(self, path)
    before = self._validate_object_entry(path)
    if before.st_size != expected_bytes:
        raise _store.ArtifactIntegrityError("artifact object size mismatch")

    _retained._assert_directory_continuity(
        self,
        ("objects", "sha256"),
        "objects",
    )

    if os.name == "nt" and sys.platform != "win32":
        descriptor = _store._open_read_only_descriptor(path)
        try:
            opened = os.fstat(descriptor)
            self._reject_reparse_point(
                opened,
                subject="artifact object descriptor",
            )
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
            if not self._same_filesystem_entry(before, opened):
                raise _store.ArtifactIntegrityError(
                    "artifact object changed before descriptor read"
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened

    if sys.platform == "win32":
        parent = getattr(self, "_retained_objects_handle", None)
        if not parent:
            raise _store.ArtifactIntegrityError(
                "retained object namespace handle is unavailable"
            )
        prefix_handle = _guard._nt_open_relative_handle(
            parent,
            prefix,
            directory=True,
            subject="artifact object prefix",
        )
        bound_descriptor = None
        descriptor = None
        try:
            bound_handle = _guard._nt_open_relative_handle(
                prefix_handle,
                digest,
                directory=False,
                subject="artifact object",
            )
            bound_descriptor = _guard._windows_file_handle_to_descriptor(
                bound_handle
            )
            bound = os.fstat(bound_descriptor)
            self._reject_reparse_point(
                bound,
                subject="artifact object descriptor",
            )
            if (
                not stat.S_ISREG(bound.st_mode)
                or bound.st_nlink != 1
                or bound.st_size != expected_bytes
                or not self._same_filesystem_entry(before, bound)
            ):
                raise _store.ArtifactIntegrityError(
                    "artifact object changed before descriptor read"
                )

            descriptor = _store._open_read_only_descriptor(path)
            opened = os.fstat(descriptor)
            self._reject_reparse_point(
                opened,
                subject="artifact object descriptor",
            )
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
            if not self._same_filesystem_entry(bound, opened):
                raise _store.ArtifactIntegrityError(
                    "artifact object changed before descriptor read"
                )
            return descriptor, opened
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            if bound_descriptor is not None:
                os.close(bound_descriptor)
            _guard._close_windows_handle(prefix_handle)

    prefix_fd = _guard._open_posix_directory_component(
        self,
        self._retained_objects_fd,
        prefix,
        subject="artifact object prefix",
    )
    try:
        descriptor, opened = _open_posix_relative_file(
            prefix_fd,
            digest,
            subject="artifact object",
        )
    finally:
        os.close(prefix_fd)
    try:
        if opened.st_size != expected_bytes:
            raise _store.ArtifactIntegrityError("artifact object size mismatch")
        if not self._same_filesystem_entry(before, opened):
            raise _store.ArtifactIntegrityError(
                "artifact object changed before descriptor read"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _revalidate_manifest_descriptor(
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
    _retained._assert_directory_continuity(
        self,
        ("manifests",),
        "manifests",
    )
    current_descriptor, current = self._open_manifest_descriptor(
        manifest_path
    )
    try:
        if (
            not self._same_filesystem_entry(held, current)
            or held.st_size != current.st_size
        ):
            raise _store.ArtifactIntegrityError(
                "artifact manifest changed during read"
            )
    finally:
        os.close(current_descriptor)


def _revalidate_object_descriptor(
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
        or held.st_size != expected_bytes
        or not self._same_filesystem_entry(opened, held)
    ):
        raise _store.ArtifactIntegrityError(
            "artifact object changed during descriptor read"
        )
    if held.st_nlink != 1:
        raise _store.ArtifactIntegrityError(
            "artifact object path changed during descriptor read"
        )

    _retained._assert_directory_continuity(
        self,
        ("objects", "sha256"),
        "objects",
    )
    current_descriptor, current = self._open_object_descriptor(
        object_path,
        expected_bytes=expected_bytes,
    )
    try:
        if not self._same_filesystem_entry(held, current):
            raise _store.ArtifactIntegrityError(
                "artifact object path changed during descriptor read"
            )
    finally:
        os.close(current_descriptor)


def _recover_orphans(self):
    with _store.ResourceLock(self.lock_path):
        self._validate_staging_namespace()
        probe = self.manifests / "00000000-0000-0000-0000-000000000000.json"
        self._validate_manifest_namespace(probe)
        _retained._assert_all_continuity(self)

        if (
            sys.platform == "win32"
            or not self._supports_descriptor_relative_cleanup()
        ):
            return self.audit()

        referenced, object_digests, corrupt = (
            _retained._trusted_recovery_plan(self)
        )
        _retained._assert_all_continuity(self)
        if not corrupt:
            for digest in sorted(object_digests - referenced):
                _retained._assert_directory_continuity(
                    self,
                    ("manifests",),
                    "manifests",
                )
                _retained._unlink_retained_object(self, digest)

        _retained._cleanup_retained_staging(self)
        _retained._assert_all_continuity(self)
        return self.audit()


def install_retained_namespace_hardening() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_retained_namespace_hardening", False):
        return
    artifact_store._open_manifest_descriptor = _open_manifest_descriptor
    artifact_store._revalidate_manifest_descriptor = (
        _revalidate_manifest_descriptor
    )
    artifact_store._open_object_descriptor = _open_object_descriptor
    artifact_store._revalidate_object_descriptor = (
        _revalidate_object_descriptor
    )
    artifact_store.recover_orphans = _recover_orphans
    artifact_store._retained_namespace_hardening = True
