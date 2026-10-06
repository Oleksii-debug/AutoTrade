"""Neutral ArtifactStore retention for current-stack WP-65 raw measurements.

Publication is storage, not qualification.  The ArtifactStore manifest timestamp
is retention metadata only and is never treated as target-host chronology,
provider/source-clock freshness, signed acceptance, or release authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from autotrade_runtime.artifacts import ArtifactStore

from .runtime_target_host_measurement import RuntimeTargetHostMeasurementArtifact


RAW_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_RAW_MEASUREMENT_CURRENT"
JSON_MEDIA_TYPE = "application/json"


class RuntimeTargetHostMeasurementPublicationError(ValueError):
    """Raised when raw target-host evidence cannot be retained exactly."""


def _artifact_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostMeasurementPublicationError(
            "artifact_id must be a canonical UUID"
        )
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise RuntimeTargetHostMeasurementPublicationError(
            "artifact_id must be a canonical UUID"
        ) from error
    if canonical != value:
        raise RuntimeTargetHostMeasurementPublicationError(
            "artifact_id must be a canonical UUID"
        )
    return value


@dataclass(frozen=True, slots=True)
class PublishedRuntimeTargetHostMeasurement:
    artifact_id: str
    payload_sha256: str
    authority_id: str
    authority_digest: str
    release_artifact_id: str
    release_artifact_sha256: str
    resource_evidence_status: str


def publish_runtime_target_host_measurement(
    evidence_store: ArtifactStore,
    *,
    artifact_id: str,
    artifact: RuntimeTargetHostMeasurementArtifact,
) -> PublishedRuntimeTargetHostMeasurement:
    """Retain exact canonical raw bytes in the neutral ArtifactStore.

    The caller cannot publish an arbitrary look-alike object: only an exact
    collector-issued ``RuntimeTargetHostMeasurementArtifact`` is accepted.  The
    returned receipt confirms storage identity only; it does not make the raw
    evidence terminally qualifying.
    """

    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    if type(artifact) is not RuntimeTargetHostMeasurementArtifact:
        raise TypeError(
            "artifact must be exact RuntimeTargetHostMeasurementArtifact"
        )
    normalized_artifact_id = _artifact_id(artifact_id)
    raw = artifact.canonical_bytes()
    digest = artifact.digest

    manifest = ArtifactStore.publish_bytes(
        evidence_store,
        artifact_id=normalized_artifact_id,
        data=raw,
        media_type=JSON_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=[f"git:{artifact.source_sha}"],
        metadata={
            "evidence_kind": RAW_EVIDENCE_KIND,
            "schema_version": artifact.schema_version,
            "authority_id": artifact.authority_id,
            "authority_digest": artifact.authority_digest,
            "release_artifact_id": artifact.release_artifact_id,
            "release_artifact_sha256": artifact.release_artifact_sha256,
            "store_identity_digest": artifact.store_identity_digest,
            "journal_taxonomy_digest": artifact.journal_taxonomy_digest,
            "resource_evidence_status": artifact.resource_evidence_status,
        },
    )
    if manifest.get("artifact_id") != normalized_artifact_id:
        raise RuntimeTargetHostMeasurementPublicationError(
            "retained measurement artifact identity changed during publication"
        )
    if manifest.get("sha256") != digest:
        raise RuntimeTargetHostMeasurementPublicationError(
            "retained measurement digest changed during publication"
        )
    if manifest.get("media_type") != JSON_MEDIA_TYPE:
        raise RuntimeTargetHostMeasurementPublicationError(
            "retained measurement media type changed during publication"
        )

    return PublishedRuntimeTargetHostMeasurement(
        artifact_id=normalized_artifact_id,
        payload_sha256=digest,
        authority_id=artifact.authority_id,
        authority_digest=artifact.authority_digest,
        release_artifact_id=artifact.release_artifact_id,
        release_artifact_sha256=artifact.release_artifact_sha256,
        resource_evidence_status=artifact.resource_evidence_status,
    )
