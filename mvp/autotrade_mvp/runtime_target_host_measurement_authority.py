"""Terminal binding guard for WP-65 raw target-host measurement evidence.

The underlying :mod:`runtime_target_host_measurement` module owns canonical raw
measurement mechanics. This module adds only the external/frozen authority facts
that the raw artifact cannot self-assert: the exact delivered artifact UUID and
campaign-window observation boundary. It does not create another evaluator,
measurement collector, signer, release authority, or trading authority.
"""

from __future__ import annotations

from uuid import UUID

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore
from .runtime_load_qualification import (
    RuntimeCampaignCut,
    RuntimeCampaignEvidence,
    RuntimeCampaignPlan,
)
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    collect_runtime_campaign_evidence_from_measurement_artifact,
)


def _canonical_uuid(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a canonical UUID"
        )
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a canonical UUID"
        ) from error
    if canonical != value:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a canonical UUID"
        )
    return canonical


def collect_release_bound_target_host_evidence(
    *,
    journal: JournalStore,
    spec: RuntimeBudgetSpec,
    plan: RuntimeCampaignPlan,
    cut: RuntimeCampaignCut,
    measurement: TargetHostMeasurementArtifact,
    expected_release_artifact_id: str,
    max_events: int = 100_000,
) -> RuntimeCampaignEvidence:
    """Produce campaign evidence only for an externally frozen delivered artifact.

    The expected UUID must come from the release/qualification boundary; reading
    the UUID back from ``measurement`` would merely trust the artifact's own
    assertion. SHA-256 remains cross-bound by ``RuntimeCampaignPlan`` and the raw
    artifact mechanics.

    Staleness source timestamps may legitimately precede campaign start (that is
    what makes state stale), but the *observation* used to qualify the campaign
    must occur inside the campaign's monotonic interval.
    """

    if type(measurement) is not TargetHostMeasurementArtifact:
        raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
    if type(cut) is not RuntimeCampaignCut:
        raise TypeError("cut must be exact RuntimeCampaignCut")

    # Snapshot caller-owned state before applying external authority checks. This
    # prevents object.__setattr__/concurrent mutation from changing the raw bundle
    # between UUID/window validation and the downstream campaign mechanics.
    measurement = TargetHostMeasurementArtifact.parse(measurement.canonical_bytes())

    frozen_release_artifact_id = _canonical_uuid(
        expected_release_artifact_id,
        name="expected_release_artifact_id",
    )
    if measurement.release_artifact_id != frozen_release_artifact_id:
        raise RuntimeTargetHostMeasurementError(
            "target-host measurement belongs to another delivered release artifact"
        )

    for sample in measurement.financial_samples:
        if sample.staleness_observed_monotonic_ns < cut.started_monotonic_ns:
            raise RuntimeTargetHostMeasurementError(
                "financial staleness observation occurs before campaign monotonic cut"
            )

    return collect_runtime_campaign_evidence_from_measurement_artifact(
        journal=journal,
        spec=spec,
        plan=plan,
        cut=cut,
        measurement=measurement,
        max_events=max_events,
    )
