"""Canonical artifact authority for AutoTrade research and evidence.

New production code imports ArtifactStore from this package. The older
content_store module remains isolated only for compatibility/characterization
until its on-disk format is explicitly migrated.
"""

from .store import (
    ArtifactAudit,
    ArtifactConflict,
    ArtifactIntegrityError,
    ArtifactStore,
)
from ._namespace_guard import install_artifact_store_namespace_guards

install_artifact_store_namespace_guards()
del install_artifact_store_namespace_guards

from ._manifest_descriptor_io import install_manifest_descriptor_reader

install_manifest_descriptor_reader()
del install_manifest_descriptor_reader

from ._windows_descriptor_bridge import install_windows_object_descriptor_bridge

install_windows_object_descriptor_bridge()
del install_windows_object_descriptor_bridge

from ._race_regressions import install_artifact_store_race_regressions

install_artifact_store_race_regressions()
del install_artifact_store_race_regressions

from ._retained_namespace import install_retained_namespace_authority

install_retained_namespace_authority()
del install_retained_namespace_authority

from ._retained_path_compat import install_retained_path_compatibility

install_retained_path_compatibility()
del install_retained_path_compatibility

from ._retained_namespace_hardening import install_retained_namespace_hardening

install_retained_namespace_hardening()
del install_retained_namespace_hardening

from ._stable_posix_capabilities import install_stable_posix_capabilities

install_stable_posix_capabilities()
del install_stable_posix_capabilities

from ._crash_atomic_manifest import install_crash_atomic_manifest_contract

install_crash_atomic_manifest_contract()
del install_crash_atomic_manifest_contract

from ._retained_recovery_hardening import install_retained_recovery_hardening

install_retained_recovery_hardening()
del install_retained_recovery_hardening

from ._retained_publication import install_retained_publication_authority

install_retained_publication_authority()
del install_retained_publication_authority

from ._retained_publication_hardening import install_retained_publication_hardening

install_retained_publication_hardening()
del install_retained_publication_hardening

from ._posix_retained_object_move_fix import install_posix_retained_object_move_fix

install_posix_retained_object_move_fix()
del install_posix_retained_object_move_fix

from ._windows_retained_publication import install_windows_retained_publication

install_windows_retained_publication()
del install_windows_retained_publication

from ._windows_retained_publication_hardening import (
    install_windows_retained_publication_hardening,
)

install_windows_retained_publication_hardening()
del install_windows_retained_publication_hardening

from ._windows_retained_rename_fix import install_windows_retained_rename_fix

install_windows_retained_rename_fix()
del install_windows_retained_rename_fix

from ._publication_transaction_fix import install_publication_transaction_fix

install_publication_transaction_fix()
del install_publication_transaction_fix

from ._publication_contract_compat import install_publication_contract_compatibility

install_publication_contract_compatibility()
del install_publication_contract_compatibility

from ._crash_atomic_publication import install_crash_atomic_publication

install_crash_atomic_publication()
del install_crash_atomic_publication

from ._retained_recovery_final import install_retained_recovery_final

install_retained_recovery_final()
del install_retained_recovery_final

from ._retained_coordination import install_retained_coordination

install_retained_coordination()
del install_retained_coordination

from ._generation_bound_read import install_generation_bound_reads

install_generation_bound_reads()
del install_generation_bound_reads

from ._root_authority import (
    install_root_authority,
    require_product_trusted_authenticated_reader,
    require_trusted_authenticated_reader,
    trusted_authenticated_reader,
)

install_root_authority()
del install_root_authority

from ._root_authority_failure_fix import install_root_authority_failure_fix

install_root_authority_failure_fix()
del install_root_authority_failure_fix

CANONICAL_ARTIFACT_STORE_MODULE = "autotrade_runtime.artifacts.store"

__all__ = [
    "ArtifactAudit",
    "ArtifactConflict",
    "ArtifactIntegrityError",
    "ArtifactStore",
    "CANONICAL_ARTIFACT_STORE_MODULE",
    "require_product_trusted_authenticated_reader",
    "require_trusted_authenticated_reader",
    "trusted_authenticated_reader",
]
