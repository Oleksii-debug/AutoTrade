from __future__ import annotations

from hashlib import sha256
import importlib
import os
from pathlib import Path
import stat
import sys
from typing import Any
import weakref

from . import _namespace_guard as _guard

_store = importlib.import_module(f"{__package__}.store")


def _same_windows_identity(first: Any, second: Any) -> bool:
    return (
        first.dwVolumeSerialNumber == second.dwVolumeSerialNumber
        and first.nFileIndexHigh == second.nFileIndexHigh
        and first.nFileIndexLow == second.nFileIndexLow
    )


def _retain_directory(store: Any, parts: tuple[str, ...], name: str) -> None:
    opened, identity = _guard._open_bound_directory(
        store,
        parts,
        subject=f"artifact {name} namespace",
    )
    if sys.platform == "win32":
        setattr(store, f"_retained_{name}_handle", opened)
        setattr(store, f"_retained_{name}_identity", identity)
        setattr(
            store,
            f"_retained_{name}_finalizer",
            weakref.finalize(store, _guard._close_windows_handle, opened),
        )
    else:
        setattr(store, f"_retained_{name}_fd", opened)
        setattr(store, f"_retained_{name}_identity", identity)
        setattr(
            store,
            f"_retained_{name}_finalizer",
            weakref.finalize(store, _guard._close_fd, opened),
        )


def _bind_retained_children(store: Any) -> None:
    _retain_directory(store, ("manifests",), "manifests")
    _retain_directory(store, ("objects", "sha256"), "objects")
    _retain_directory(store, ("staging",), "staging")


def _assert_directory_continuity(
    store: Any,
    parts: tuple[str, ...],
    name: str,
) -> None:
    current, current_identity = _guard._open_bound_directory(
        store,
        parts,
        subject=f"artifact {name} namespace continuity",
    )
    try:
        expected_identity = getattr(store, f"_retained_{name}_identity")
        if sys.platform == "win32":
            if not _same_windows_identity(expected_identity, current_identity):
                raise _store.ArtifactIntegrityError(
                    f"artifact {name} namespace changed after store initialization"
                )
        elif not store._same_filesystem_entry(expected_identity, current_identity):
            raise _store.ArtifactIntegrityError(
                f"artifact {name} namespace changed after store initialization"
            )
    finally:
        if sys.platform == "win32":
            _guard._close_windows_handle(current)
        else:
            os.close(current)


def _assert_all_continuity(store: Any) -> None:
    _assert_directory_continuity(store, ("manifests",), "manifests")
    _assert_directory_continuity(store, ("objects", "sha256"), "objects")
    _assert_directory_continuity(store, ("staging",), "staging")


def _validate_manifest_name(store: Any, manifest_path: Path) -> str:
    path = Path(manifest_path)
    if path.parent != store.manifests:
        raise _store.ArtifactIntegrityError(
            "artifact manifest path escapes store namespace"
        )
    return _guard._validate_component(path.name, subject="artifact manifest")


def _validate_object_name(store: Any, object_path: Path) -> tuple[str, str]:
    path = Path(object_path)
    digest = path.name
    prefix = path.parent.name
    if path.parent.parent != store.objects:
        raise _store.ArtifactIntegrityError(
            "content-addressed object path escapes store namespace"
        )
    try:
        canonical = store._object_path(digest)
    except ValueError as error:
        raise _store.ArtifactIntegrityError(
            "content-addressed object digest is invalid"
        ) from error
    if canonical != path or prefix != digest[:2]:
        raise _store.ArtifactIntegrityError(
            "content-addressed object path is not canonical"
        )
    return (
        _guard._validate_component(prefix, subject="artifact object"),
        _guard._validate_component(digest, subject="artifact object"),
    )


def _retained_posix_file(
    store: Any,
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
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            f"{subject} cannot be inspected in retained namespace"
        ) from error
    store._reject_reparse_point(before, subject=subject)
    if stat.S_ISLNK(before.st_mode):
        raise _store.ArtifactIntegrityError(f"{subject} must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise _store.ArtifactIntegrityError(f"{subject} must be a regular file")
    if before.st_nlink != 1:
        raise _store.ArtifactIntegrityError(
            f"{subject} must not have hard-link aliases"
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
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not store._same_filesystem_entry(before, opened)
        ):
            raise _store.ArtifactIntegrityError(
                f"{subject} changed before descriptor read"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _open_manifest_descriptor(self, manifest_path: Path):
    name = _validate_manifest_name(self, manifest_path)
    _assert_directory_continuity(self, ("manifests",), "manifests")
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
        opened = os.fstat(descriptor)
        if opened.st_nlink != 1:
            os.close(descriptor)
            raise _store.ArtifactIntegrityError(
                "artifact manifest descriptor must not have hard-link aliases"
            )
        return descriptor, opened
    return _retained_posix_file(
        self,
        self._retained_manifests_fd,
        name,
        subject="artifact manifest",
    )


def _open_object_descriptor(
    self,
    object_path: Path,
    *,
    expected_bytes: int,
):
    prefix, digest = _validate_object_name(self, object_path)
    _assert_directory_continuity(self, ("objects", "sha256"), "objects")
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
        try:
            handle = _guard._nt_open_relative_handle(
                prefix_handle,
                digest,
                directory=False,
                subject="artifact object",
            )
            descriptor = _guard._windows_file_handle_to_descriptor(handle)
        finally:
            _guard._close_windows_handle(prefix_handle)
        opened = os.fstat(descriptor)
        if opened.st_nlink != 1 or opened.st_size != expected_bytes:
            os.close(descriptor)
            raise _store.ArtifactIntegrityError(
                "artifact object size or link identity mismatch"
            )
        return descriptor, opened

    prefix_fd = _guard._open_posix_directory_component(
        self,
        self._retained_objects_fd,
        prefix,
        subject="artifact object prefix",
    )
    try:
        descriptor, opened = _retained_posix_file(
            self,
            prefix_fd,
            digest,
            subject="artifact object",
        )
    finally:
        os.close(prefix_fd)
    if opened.st_size != expected_bytes:
        os.close(descriptor)
        raise _store.ArtifactIntegrityError("artifact object size mismatch")
    return descriptor, opened


def _revalidate_manifest_descriptor(
    self,
    manifest_path: Path,
    descriptor: int,
    opened: os.stat_result,
) -> None:
    held = os.fstat(descriptor)
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_nlink != 1
        or not self._same_filesystem_entry(opened, held)
        or self._manifest_descriptor_snapshot(held)
        != self._manifest_descriptor_snapshot(opened)
    ):
        raise _store.ArtifactIntegrityError("artifact manifest changed during read")
    _assert_directory_continuity(self, ("manifests",), "manifests")
    current_descriptor, current = _open_manifest_descriptor(self, manifest_path)
    try:
        if not self._same_filesystem_entry(held, current):
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
    held = os.fstat(descriptor)
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_nlink != 1
        or held.st_size != expected_bytes
        or not self._same_filesystem_entry(opened, held)
    ):
        raise _store.ArtifactIntegrityError(
            "artifact object path changed during descriptor read"
        )
    _assert_directory_continuity(self, ("objects", "sha256"), "objects")
    current_descriptor, current = _open_object_descriptor(
        self,
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


def _list_retained_objects(self) -> tuple[set[str], list[str]]:
    object_digests: set[str] = set()
    corrupt: list[str] = []
    base_fd = self._retained_objects_fd
    for prefix in tuple(os.listdir(base_fd)):
        if (
            not isinstance(prefix, str)
            or len(prefix) != 2
            or any(ch not in "0123456789abcdef" for ch in prefix)
        ):
            corrupt.append(f"object:objects/sha256/{prefix}")
            continue
        try:
            prefix_fd = _guard._open_posix_directory_component(
                self,
                base_fd,
                prefix,
                subject="artifact object recovery prefix",
            )
        except _store.ArtifactIntegrityError:
            corrupt.append(f"object:objects/sha256/{prefix}")
            continue
        try:
            for digest in tuple(os.listdir(prefix_fd)):
                try:
                    self._object_path(digest)
                except ValueError:
                    corrupt.append(f"object:objects/sha256/{prefix}/{digest}")
                    continue
                if digest[:2] != prefix:
                    corrupt.append(f"object:objects/sha256/{prefix}/{digest}")
                    continue
                try:
                    entry = os.stat(
                        digest,
                        dir_fd=prefix_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    corrupt.append(f"object:objects/sha256/{prefix}/{digest}")
                    continue
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or stat.S_ISLNK(entry.st_mode)
                    or entry.st_nlink != 1
                ):
                    corrupt.append(f"object:objects/sha256/{prefix}/{digest}")
                    continue
                object_digests.add(digest)
        finally:
            os.close(prefix_fd)
    return object_digests, corrupt


def _trusted_recovery_plan(self) -> tuple[set[str], set[str], list[str]]:
    referenced: set[str] = set()
    corrupt: list[str] = []
    for name in tuple(os.listdir(self._retained_manifests_fd)):
        # atomic_write_json retains a per-manifest ResourceLock sidecar in
        # this directory. It is coordination state, never a manifest or an
        # incomplete manifest inventory.
        if name.startswith(".") and name.endswith(".json.lock"):
            identifier = name[1:-len(".json.lock")]
            try:
                if self._artifact_id(identifier) == identifier:
                    continue
            except ValueError:
                pass
        if not isinstance(name, str) or not name.endswith(".json"):
            corrupt.append(f"manifest:{name}")
            continue
        path = self.manifests / name
        try:
            manifest = self._load_manifest_path(path)
            _store._verify_manifest_integrity(manifest, required=True)
            _path, digest, _bytes = self._manifest_object_contract(manifest)
            referenced.add(digest)
            # A missing/corrupt referenced object remains referenced. Its
            # state is reported by audit, but it cannot erase an otherwise
            # authenticated manifest reference from the deletion inventory.
        except (
            _store.ArtifactIntegrityError,
            FileNotFoundError,
            KeyError,
            AttributeError,
            ValueError,
        ):
            corrupt.append(name)
    object_digests, _object_corrupt = _list_retained_objects(self)
    # Noncanonical object names cannot be deletion candidates or valid
    # references; audit reports them, but they need not block a proven orphan.
    return referenced, object_digests, corrupt


def _unlink_retained_object(self, digest: str) -> bool:
    prefix = digest[:2]
    prefix_fd = _guard._open_posix_directory_component(
        self,
        self._retained_objects_fd,
        prefix,
        subject="artifact object recovery prefix",
    )
    try:
        try:
            entry = os.stat(
                digest,
                dir_fd=prefix_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(entry.st_mode)
            or stat.S_ISLNK(entry.st_mode)
            or entry.st_nlink != 1
        ):
            return False
        _assert_directory_continuity(self, ("objects", "sha256"), "objects")
        os.unlink(digest, dir_fd=prefix_fd)
        return True
    finally:
        os.close(prefix_fd)


def _cleanup_retained_staging(self) -> None:
    _assert_directory_continuity(self, ("staging",), "staging")
    descriptor = self._retained_staging_fd
    for name in tuple(os.listdir(descriptor)):
        try:
            entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            stat.S_ISREG(entry.st_mode)
            and not stat.S_ISLNK(entry.st_mode)
            and entry.st_nlink == 1
        ):
            _assert_directory_continuity(self, ("staging",), "staging")
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass


def _recover_orphans(self):
    with _store.ResourceLock(self.lock_path):
        _assert_all_continuity(self)
        if sys.platform == "win32" or not self._supports_descriptor_relative_cleanup():
            return self.audit()
        referenced, object_digests, corrupt = _trusted_recovery_plan(self)
        _assert_all_continuity(self)
        if not corrupt:
            for digest in sorted(object_digests - referenced):
                _assert_directory_continuity(
                    self,
                    ("manifests",),
                    "manifests",
                )
                _unlink_retained_object(self, digest)
        _cleanup_retained_staging(self)
        _assert_all_continuity(self)
        return self.audit()


def install_retained_namespace_authority() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_retained_namespace_authority", False):
        return

    original_init = artifact_store.__init__
    original_publish = artifact_store.publish_bytes

    def retained_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        _bind_retained_children(self)

    def continuity_fenced_publish(self, *args, **kwargs):
        _assert_all_continuity(self)
        result = original_publish(self, *args, **kwargs)
        _assert_all_continuity(self)
        return result

    artifact_store.__init__ = retained_init
    artifact_store._open_manifest_descriptor = _open_manifest_descriptor
    artifact_store._revalidate_manifest_descriptor = _revalidate_manifest_descriptor
    artifact_store._open_object_descriptor = _open_object_descriptor
    artifact_store._revalidate_object_descriptor = _revalidate_object_descriptor
    artifact_store.recover_orphans = _recover_orphans
    artifact_store.publish_bytes = continuity_fenced_publish
    artifact_store._retained_namespace_authority = True
