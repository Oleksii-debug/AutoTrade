from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat

from . import _namespace_guard as _guard

_store = importlib.import_module(f"{__package__}.store")


# Capability is a property of the interpreter/platform, not of a temporary
# instrumentation wrapper around os.unlink/os.stat in a regression test.
# Snapshot it while the real stdlib functions are installed.
_DESCRIPTOR_RELATIVE_CLEANUP_SUPPORTED = bool(
    os.name != "nt"
    and getattr(os, "O_DIRECTORY", 0)
    and getattr(os, "O_NOFOLLOW", 0)
    and os.stat in getattr(os, "supports_dir_fd", set())
    and os.unlink in getattr(os, "supports_dir_fd", set())
    and os.listdir in getattr(os, "supports_fd", set())
)


def _stable_descriptor_relative_cleanup_support() -> bool:
    return _DESCRIPTOR_RELATIVE_CLEANUP_SUPPORTED


def _open_manifest_descriptor_fail_closed(self, manifest_path: Path):
    before = self._validate_manifest_entry(manifest_path)
    descriptor, opened = _guard._open_bound_file(
        self,
        _guard._manifest_parts(self, manifest_path),
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
            # Refresh the pathname observation for diagnostics/state convergence,
            # but never accept an ABA return to the original inode.  The read
            # attempt has already crossed an identity change and must fail.
            try:
                self._validate_manifest_entry(manifest_path)
            except (OSError, _store.ArtifactIntegrityError):
                pass
            raise _store.ArtifactIntegrityError(
                "artifact manifest changed during read"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _revalidate_object_descriptor_with_path_diagnostic(
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
    self._reject_reparse_point(held, subject="artifact object descriptor")
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_size != expected_bytes
        or not self._same_filesystem_entry(opened, held)
    ):
        raise _store.ArtifactIntegrityError(
            "artifact object changed during descriptor read"
        )
    if held.st_nlink != 1:
        # A held inode dropping to zero links is the normal POSIX signature of
        # pathname replacement while the descriptor still protects old bytes;
        # extra links are likewise a namespace/alias change.  Fail closed with
        # the path-specific diagnostic expected by the authority contract.
        raise _store.ArtifactIntegrityError(
            "artifact object path changed during descriptor read"
        )

    current_hint = self._validate_object_entry(object_path)
    current_descriptor, current = _guard._open_bound_file(
        self,
        _guard._object_parts(self, object_path),
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


def install_artifact_store_race_regressions() -> None:
    artifact_store = _store.ArtifactStore
    artifact_store._supports_descriptor_relative_cleanup = staticmethod(
        _stable_descriptor_relative_cleanup_support
    )
    artifact_store._open_manifest_descriptor = _open_manifest_descriptor_fail_closed
    artifact_store._revalidate_object_descriptor = (
        _revalidate_object_descriptor_with_path_diagnostic
    )
