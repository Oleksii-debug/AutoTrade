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

from ._retained_namespace_hardening import install_retained_namespace_hardening

install_retained_namespace_hardening()
del install_retained_namespace_hardening

CANONICAL_ARTIFACT_STORE_MODULE = "autotrade_research.artifacts.store"

__all__ = [
    "ArtifactAudit",
    "ArtifactConflict",
    "ArtifactIntegrityError",
    "ArtifactStore",
    "CANONICAL_ARTIFACT_STORE_MODULE",
]
