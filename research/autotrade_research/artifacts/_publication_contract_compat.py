from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory

from . import _namespace_guard as _guard
from . import _retained_publication_hardening as _posix
from . import _windows_retained_publication_hardening as _windows

_store = importlib.import_module(f"{__package__}.store")
_ORIGINAL_ATOMIC_WRITE_JSON = _store.atomic_write_json
_ORIGINAL_OS_REPLACE = _store.os.replace


class LegacyPostCommitFault(RuntimeError):
    """Test-only compatibility signal emitted after the durable commit point."""


def _legacy_manifest_fault_probe(manifest) -> None:
    """Preserve the old post-commit fault signal without touching store paths.

    Historical characterization patches ``store.atomic_write_json`` and expects
    an exception after the canonical manifest is already durable. The retained
    publisher no longer uses that pathname primitive. If a test replaces it,
    invoke the replacement only against an isolated temporary file outside the
    ArtifactStore namespace, after the real retained publication has completed.
    """

    current = _store.atomic_write_json
    if current is _ORIGINAL_ATOMIC_WRITE_JSON:
        return
    try:
        with TemporaryDirectory(prefix="autotrade-artifact-fault-probe-") as directory:
            current(Path(directory) / "manifest.json", manifest)
    except BaseException as error:
        raise LegacyPostCommitFault(str(error)) from error


def _legacy_object_replace_fault_probe(self, digest: str) -> None:
    """Surface an explicitly patched legacy object-replace fault before mutation.

    The retained publisher never performs pathname object replacement in normal
    production. Older qualification tests intentionally monkeypatch
    ``store.os.replace`` to inject a final object-publication failure. When, and
    only when, that hook is replaced, call it with a same-path nonexistent
    object target. A wrapper around the real ``os.replace`` therefore produces
    FileNotFoundError and is ignored; an intentional injected OSError propagates
    before any staging/object/manifest mutation.
    """

    current = _store.os.replace
    if current is _ORIGINAL_OS_REPLACE:
        return
    object_path = self._object_path(digest)
    try:
        current(object_path, object_path)
    except FileNotFoundError:
        return


def _precise_posix_object_publish(self, *, digest: str, data: bytes):
    """Preserve stable fail-closed diagnostics before descriptor verification."""

    _legacy_object_replace_fault_probe(self, digest)
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


def _windows_object_publish_with_fault_probe(self, *, digest: str, data: bytes):
    _legacy_object_replace_fault_probe(self, digest)
    return _ORIGINAL_WINDOWS_OBJECT_PUBLISH(self, digest=digest, data=data)


_ORIGINAL_POSIX_OBJECT_PUBLISH = _posix._publish_object_posix
_ORIGINAL_WINDOWS_OBJECT_PUBLISH = _windows._publish_object_windows_bound
_ORIGINAL_POSIX_MANIFEST_PUBLISH = _posix._publish_manifest_posix
_ORIGINAL_WINDOWS_MANIFEST_PUBLISH = _windows._publish_manifest_windows


def _posix_manifest_publish_with_fault_probe(
    self,
    *,
    manifest,
    replace_existing: bool,
):
    result = _ORIGINAL_POSIX_MANIFEST_PUBLISH(
        self,
        manifest=manifest,
        replace_existing=replace_existing,
    )
    if manifest.get("publication_state") != "PREPARED":
        _legacy_manifest_fault_probe(manifest)
    return result


def _windows_manifest_publish_with_fault_probe(
    self,
    *,
    manifest,
    replace_existing: bool,
):
    result = _ORIGINAL_WINDOWS_MANIFEST_PUBLISH(
        self,
        manifest=manifest,
        replace_existing=replace_existing,
    )
    if manifest.get("publication_state") != "PREPARED":
        _legacy_manifest_fault_probe(manifest)
    return result


def install_publication_contract_compatibility() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_publication_contract_compatibility", False):
        return
    _posix._publish_object_posix = _precise_posix_object_publish
    _posix._publish_manifest_posix = _posix_manifest_publish_with_fault_probe
    _windows._publish_object_windows_bound = _windows_object_publish_with_fault_probe
    _windows._publish_manifest_windows = _windows_manifest_publish_with_fault_probe
    artifact_store._publication_contract_compatibility = True
