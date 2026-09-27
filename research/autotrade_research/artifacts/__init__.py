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

CANONICAL_ARTIFACT_STORE_MODULE = "autotrade_research.artifacts.store"

__all__ = [
    "ArtifactAudit",
    "ArtifactConflict",
    "ArtifactIntegrityError",
    "ArtifactStore",
    "CANONICAL_ARTIFACT_STORE_MODULE",
]
