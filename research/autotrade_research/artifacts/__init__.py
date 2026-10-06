"""Compatibility facade for the neutral installed ArtifactStore authority.

Production owns the implementation under autotrade_runtime. Research names
remain import-compatible aliases to the same module and class objects; they must
never load a second copy of the security-critical wrapper stack.
"""

from __future__ import annotations

from importlib import import_module
import sys

_runtime = import_module("autotrade_runtime.artifacts")

ArtifactAudit = _runtime.ArtifactAudit
ArtifactConflict = _runtime.ArtifactConflict
ArtifactIntegrityError = _runtime.ArtifactIntegrityError
ArtifactStore = _runtime.ArtifactStore
CANONICAL_ARTIFACT_STORE_MODULE = _runtime.CANONICAL_ARTIFACT_STORE_MODULE
trusted_authenticated_reader = _runtime.trusted_authenticated_reader

_CANONICAL_SUBMODULES = (
    "store",
    "durable_publish",
    "_namespace_guard",
    "_manifest_descriptor_io",
    "_windows_descriptor_bridge",
    "_race_regressions",
    "_retained_namespace",
    "_retained_path_compat",
    "_retained_namespace_hardening",
    "_stable_posix_capabilities",
    "_crash_atomic_manifest",
    "_retained_recovery_hardening",
    "_retained_publication",
    "_retained_publication_hardening",
    "_posix_retained_object_move_fix",
    "_windows_retained_publication",
    "_windows_retained_publication_hardening",
    "_windows_retained_rename_fix",
    "_publication_transaction_fix",
    "_publication_contract_compat",
    "_crash_atomic_publication",
    "_retained_recovery_final",
    "_retained_coordination",
    "_generation_bound_read",
    "_root_authority",
    "_root_authority_failure_fix",
)
for _name in _CANONICAL_SUBMODULES:
    sys.modules[f"{__name__}.{_name}"] = import_module(
        f"autotrade_runtime.artifacts.{_name}"
    )

# Historical artifact-local ResourceLock imports remain supported, but resolve to
# the single neutral runtime module rather than loading the old research source.
sys.modules[f"{__name__}.resource_lock"] = import_module(
    "autotrade_runtime.resource_lock"
)

del _name
del _runtime

__all__ = [
    "ArtifactAudit",
    "ArtifactConflict",
    "ArtifactIntegrityError",
    "ArtifactStore",
    "CANONICAL_ARTIFACT_STORE_MODULE",
    "trusted_authenticated_reader",
]
