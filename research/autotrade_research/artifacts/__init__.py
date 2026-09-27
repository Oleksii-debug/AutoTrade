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

from ._retained_recovery_hardening import install_retained_recovery_hardening

install_retained_recovery_hardening()
del install_retained_recovery_hardening

from ._retained_publication import install_retained_publication_authority

install_retained_publication_authority()
del install_retained_publication_authority

from ._retained_publication_hardening import install_retained_publication_hardening

install_retained_publication_hardening()
del install_retained_publication_hardening

from ._windows_retained_publication import install_windows_retained_publication

install_windows_retained_publication()
del install_windows_retained_publication

from ._windows_retained_publication_hardening import (
    install_windows_retained_publication_hardening,
)

install_windows_retained_publication_hardening()
del install_windows_retained_publication_hardening

from ._retained_coordination import install_retained_coordination

install_retained_coordination()
del install_retained_coordination

from ._root_authority import install_root_authority

install_root_authority()
del install_root_authority

CANONICAL_ARTIFACT_STORE_MODULE = "autotrade_research.artifacts.store"

__all__ = [
    "ArtifactAudit",
    "ArtifactConflict",
    "ArtifactIntegrityError",
    "ArtifactStore",
    "CANONICAL_ARTIFACT_STORE_MODULE",
]
