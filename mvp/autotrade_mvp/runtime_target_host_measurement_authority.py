"""Terminal binding facade for WP-65 raw target-host measurement evidence.

The underlying :mod:`runtime_target_host_measurement` evidence-minting function
owns the mandatory external delivered-artifact UUID and campaign-window guards.
This module keeps the explicit terminal-facing facade and performs the same
checks before delegation as defense in depth. It additionally freezes the exact
delivered-artifact SHA-256 at the terminal boundary so caller-owned plan and raw
measurement state cannot mutually self-assert a favorable artifact digest.
It does not create another evaluator, measurement collector, signer, release
authority, or trading authority.
"""

from __future__ import annotations

import re
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
    snapshot_target_host_measurement,
)


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def _canonical_sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


def collect_release_bound_target_host_evidence(
    *,
    journal: JournalStore,
    spec: RuntimeBudgetSpec,
    plan: RuntimeCampaignPlan,
    cut: RuntimeCampaignCut,
    measurement: TargetHostMeasurementArtifact,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
    max_events: int = 100_000,
) -> RuntimeCampaignEvidence:
    """Produce campaign evidence only for an externally frozen delivered artifact.

    The expected UUID and SHA-256 must come from the release/qualification boundary;
    reading either value back from ``measurement`` or accepting the plan's digest
    alone would merely trust caller-owned state. The raw mechanics repeat their own
    UUID/SHA campaign binding after this terminal authority check.

    Staleness source timestamps may legitimately precede campaign start (that is
    what makes state stale), but the *observation* used to qualify the campaign
    must occur inside the campaign's monotonic interval.
    """

    if type(measurement) is not TargetHostMeasurementArtifact:
        raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
    if type(plan) is not RuntimeCampaignPlan:
        raise TypeError("plan must be exact RuntimeCampaignPlan")
    if type(cut) is not RuntimeCampaignCut:
        raise TypeError("cut must be exact RuntimeCampaignCut")

    # Snapshot caller-owned state before applying external authority checks. This
    # prevents object.__setattr__/concurrent mutation from changing the raw bundle
    # between release/window validation and the downstream campaign mechanics.
    measurement = snapshot_target_host_measurement(measurement)

    frozen_release_artifact_id = _canonical_uuid(
        expected_release_artifact_id,
        name="expected_release_artifact_id",
    )
    frozen_release_artifact_sha256 = _canonical_sha256(
        expected_release_artifact_sha256,
        name="expected_release_artifact_sha256",
    )
    if measurement.release_artifact_id != frozen_release_artifact_id:
        raise RuntimeTargetHostMeasurementError(
            "target-host measurement belongs to another delivered release artifact"
        )
    if (
        measurement.release_artifact_sha256 != frozen_release_artifact_sha256
        or plan.release_artifact_sha256 != frozen_release_artifact_sha256
    ):
        raise RuntimeTargetHostMeasurementError(
            "target-host measurement or plan belongs to another delivered release digest"
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
        expected_release_artifact_id=frozen_release_artifact_id,
        max_events=max_events,
    )