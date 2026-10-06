from __future__ import annotations

import importlib
import os
import stat
import sys

from . import _namespace_guard as _guard
from . import _retained_coordination as _coordination
from . import _retained_namespace as _retained

_store = importlib.import_module(f"{__package__}.store")


def _unlink_bound_object(self, digest: str) -> bool:
    """Delete only from the already-retained object generation.

    Once recovery has completed its final pre-destructive continuity fence, a
    later lexical rename/replacement must not redirect deletion and must not
    retroactively invalidate deletion through the retained parent descriptor.
    """

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
        try:
            os.unlink(digest, dir_fd=prefix_fd)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(prefix_fd)


def _cleanup_bound_staging(self) -> None:
    """Clean regular single-link temp files only through retained staging fd."""

    descriptor = self._retained_staging_fd
    for name in tuple(os.listdir(descriptor)):
        try:
            entry = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if (
            stat.S_ISREG(entry.st_mode)
            and not stat.S_ISLNK(entry.st_mode)
            and entry.st_nlink == 1
        ):
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass


def _fallback_audit_after_detachment(
    before: _store.ArtifactAudit,
    deleted_digests: set[str],
) -> _store.ArtifactAudit:
    """Report the retained generation without authenticating replacement paths."""

    remaining_unreferenced = tuple(
        digest
        for digest in before.unreferenced_objects
        if digest not in deleted_digests
    )
    return _store.ArtifactAudit(
        manifests=before.manifests,
        objects=max(0, before.objects - len(deleted_digests)),
        unreferenced_objects=remaining_unreferenced,
        missing_objects=before.missing_objects,
        corrupt_objects=before.corrupt_objects,
    )


def _recover_orphans_bound(self):
    with _coordination.artifact_store_coordination(self):
        self._validate_staging_namespace()
        probe = self.manifests / "00000000-0000-0000-0000-000000000000.json"
        self._validate_manifest_namespace(probe)
        _retained._assert_all_continuity(self)

        if (
            sys.platform == "win32"
            or not self._supports_descriptor_relative_cleanup()
        ):
            return self.audit()

        # Capture the public report while all lexical children are still proven
        # to name the retained generation. If a non-cooperating actor detaches a
        # child during the destructive phase, never audit through that replacement
        # merely to manufacture a post-cleanup report.
        before = self.audit()
        referenced, object_digests, corrupt = _retained._trusted_recovery_plan(self)

        # This is the final lexical continuity gate. Destructive operations after
        # it are intentionally descriptor-relative to the retained generation.
        _retained._assert_all_continuity(self)

        deleted_digests: set[str] = set()
        if not corrupt:
            for digest in sorted(object_digests - referenced):
                if _unlink_bound_object(self, digest):
                    deleted_digests.add(digest)

        _cleanup_bound_staging(self)

        try:
            _retained._assert_all_continuity(self)
        except _store.ArtifactIntegrityError:
            return _fallback_audit_after_detachment(before, deleted_digests)
        return self.audit()


def install_retained_recovery_hardening() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_retained_recovery_hardening", False):
        return
    artifact_store.recover_orphans = _recover_orphans_bound
    artifact_store._retained_recovery_hardening = True
