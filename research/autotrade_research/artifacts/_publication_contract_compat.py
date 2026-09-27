from __future__ import annotations

import importlib
import os
import stat

from . import _namespace_guard as _guard
from . import _retained_publication_hardening as _posix
from . import _windows_retained_publication_hardening as _windows

_store = importlib.import_module(f"{__package__}.store")
_ORIGINAL_ATOMIC_WRITE_JSON = _store.atomic_write_json


def _legacy_manifest_fault_probe(self, manifest) -> None:
    """Preserve the pre-WP-06 crash-injection seam without using it normally.

    Existing qualification tests monkeypatch ``store.atomic_write_json`` to
    inject a process death immediately before, or immediately after, manifest
    durability. Production keeps the retained publisher; only an explicitly
    replaced hook is invoked here. A post-commit injector may durably write the
    same manifest and then raise, exactly matching the historical crash test.
    """

    current = _store.atomic_write_json
    if current is _ORIGINAL_ATOMIC_WRITE_JSON:
        return
    current(self._manifest_path(manifest["artifact_id"]), manifest)


def _precise_posix_object_publish(self, *, digest: str, data: bytes):
    """Preserve stable fail-closed diagnostics before descriptor verification."""

    prefix = digest[:2]
    try:
        prefix_entry = os.stat(
            prefix,
            dir_fd=self._retained_objects_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        prefix_entry = None
    if prefix_entry is not None:
        if stat.S_ISLNK(prefix_entry.st_mode) or not stat.S_ISDIR(prefix_entry.st_mode):
            raise _store.ArtifactIntegrityError(
                "content-addressed object path escapes store namespace"
            )
        prefix_fd = _guard._open_posix_directory_component(
            self,
            self._retained_objects_fd,
            prefix,
            subject="artifact object prefix",
        )
        try:
            try:
                entry = os.stat(
                    digest,
                    dir_fd=prefix_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                entry = None
            if entry is not None:
                self._reject_reparse_point(entry, subject="artifact object")
                if stat.S_ISLNK(entry.st_mode):
                    raise _store.ArtifactIntegrityError(
                        "content-addressed object must not be a symlink"
                    )
                if not stat.S_ISREG(entry.st_mode):
                    raise _store.ArtifactIntegrityError(
                        "content-addressed object must be a regular file"
                    )
                if entry.st_nlink != 1:
                    raise _store.ArtifactIntegrityError(
                        "content-addressed object must not have hard-link aliases"
                    )
        finally:
            os.close(prefix_fd)
    return _ORIGINAL_POSIX_OBJECT_PUBLISH(self, digest=digest, data=data)


_ORIGINAL_POSIX_OBJECT_PUBLISH = _posix._publish_object_posix
_ORIGINAL_POSIX_MANIFEST_PUBLISH = _posix._publish_manifest_posix
_ORIGINAL_WINDOWS_MANIFEST_PUBLISH = _windows._publish_manifest_windows


def _posix_manifest_publish_with_fault_probe(
    self,
    *,
    manifest,
    replace_existing: bool,
):
    _legacy_manifest_fault_probe(self, manifest)
    return _ORIGINAL_POSIX_MANIFEST_PUBLISH(
        self,
        manifest=manifest,
        replace_existing=replace_existing,
    )


def _windows_manifest_publish_with_fault_probe(
    self,
    *,
    manifest,
    replace_existing: bool,
):
    _legacy_manifest_fault_probe(self, manifest)
    return _ORIGINAL_WINDOWS_MANIFEST_PUBLISH(
        self,
        manifest=manifest,
        replace_existing=replace_existing,
    )


def install_publication_contract_compatibility() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_publication_contract_compatibility", False):
        return
    _posix._publish_object_posix = _precise_posix_object_publish
    _posix._publish_manifest_posix = _posix_manifest_publish_with_fault_probe
    _windows._publish_manifest_windows = _windows_manifest_publish_with_fault_probe
    artifact_store._publication_contract_compatibility = True
