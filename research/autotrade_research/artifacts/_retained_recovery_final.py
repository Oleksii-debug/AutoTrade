from __future__ import annotations

import sys

from . import _retained_namespace as _retained
from . import _retained_recovery_hardening as _recovery

_store = _recovery._store


def _recover_orphans_retained_only(self):
    # Public recovery is serialized by retained/root coordination wrappers.
    # Destructive work below stays bound to retained descriptors/HANDLEs and
    # never creates/opens lexical .artifact-store.lock compatibility metadata.
    self._validate_staging_namespace()
    probe = self.manifests / "00000000-0000-0000-0000-000000000000.json"
    self._validate_manifest_namespace(probe)
    _retained._assert_all_continuity(self)

    if sys.platform == "win32" or not self._supports_descriptor_relative_cleanup():
        return self.audit()

    before = self.audit()
    referenced, object_digests, corrupt = _retained._trusted_recovery_plan(self)
    _retained._assert_all_continuity(self)

    deleted_digests: set[str] = set()
    if not corrupt:
        for digest in sorted(object_digests - referenced):
            if _recovery._unlink_bound_object(self, digest):
                deleted_digests.add(digest)

    _recovery._cleanup_bound_staging(self)
    try:
        _retained._assert_all_continuity(self)
    except _store.ArtifactIntegrityError:
        return _recovery._fallback_audit_after_detachment(
            before,
            deleted_digests,
        )
    return self.audit()


def install_retained_recovery_final() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_retained_recovery_final", False):
        return
    artifact_store.recover_orphans = _recover_orphans_retained_only
    artifact_store._retained_recovery_final = True
