"""Captured release-bound durable-financial authority for terminal WP-65.

The public durable-financial facade remains an explicit focused-test seam. Product
composed qualification uses the verifier constructed here so rebinding measurement
snapshotting, delivered-release validation, parent target-host evidence collection,
error classes, or lower durable mechanics after import cannot retarget terminal PASS.

This module does not create provider truth, chronology, signer policy, release
authority, trading authority, profitability, or economic edge.
"""

from __future__ import annotations

from uuid import UUID

from .performance_qualification import RuntimeBudgetError, RuntimeBudgetSpec
from .persistence import JournalStore
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_durable_financial import (
    DurableTargetHostFinancialBinding,
    RuntimeTargetHostDurableFinancialError,
    bind_durable_financial_latency_to_target_host_measurement,
)
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    snapshot_target_host_measurement,
)
from .runtime_target_host_measurement_authority import (
    collect_release_bound_target_host_evidence,
)


def _build_release_bound_durable_financial_authority(
    *,
    measurement_type,
    measurement_snapshotter,
    uuid_type,
    durable_error_type,
    parent_collector,
    parent_error_types,
    durable_binder,
):
    """Build one release-bound binder with immutable direct dependency captures."""

    def canonical_uuid(value: object, *, name: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise durable_error_type(f"{name} must be a canonical UUID")
        try:
            canonical = str(uuid_type(value))
        except (ValueError, TypeError, AttributeError) as error:
            raise durable_error_type(f"{name} must be a canonical UUID") from error
        if canonical != value:
            raise durable_error_type(f"{name} must be a canonical UUID")
        return canonical

    def canonical_digest(value: object, *, name: str) -> str:
        if (
            type(value) is not str
            or len(value) != 71
            or not value.startswith("sha256:")
            or value != value.lower()
            or any(char not in "0123456789abcdef" for char in value[7:])
        ):
            raise durable_error_type(f"{name} must be canonical sha256:<64 hex>")
        return value

    def bind(
        *,
        store: JournalStore,
        spec: RuntimeBudgetSpec,
        campaign_plan: RuntimeCampaignPlan,
        campaign_cut: RuntimeCampaignCut,
        declared_plan_id: str,
        measurement: TargetHostMeasurementArtifact,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
    ) -> DurableTargetHostFinancialBinding:
        if type(measurement) is not measurement_type:
            raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
        measurement_authority = measurement_snapshotter(measurement)
        frozen_release_artifact_id = canonical_uuid(
            expected_release_artifact_id,
            name="expected_release_artifact_id",
        )
        frozen_release_artifact_sha256 = canonical_digest(
            expected_release_artifact_sha256,
            name="expected_release_artifact_sha256",
        )
        if measurement_authority.release_artifact_id != frozen_release_artifact_id:
            raise durable_error_type(
                "target-host measurement belongs to another delivered release artifact"
            )
        if (
            measurement_authority.release_artifact_sha256
            != frozen_release_artifact_sha256
        ):
            raise durable_error_type(
                "target-host measurement belongs to another delivered release digest"
            )

        try:
            parent_collector(
                journal=store,
                spec=spec,
                plan=campaign_plan,
                cut=campaign_cut,
                measurement=measurement_authority,
                expected_release_artifact_id=frozen_release_artifact_id,
                expected_release_artifact_sha256=frozen_release_artifact_sha256,
            )
        except parent_error_types as error:
            raise durable_error_type(str(error)) from error

        return durable_binder(
            store,
            spec,
            declared_plan_id=declared_plan_id,
            measurement=measurement_authority,
        )

    return bind


bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement = (
    _build_release_bound_durable_financial_authority(
        measurement_type=TargetHostMeasurementArtifact,
        measurement_snapshotter=snapshot_target_host_measurement,
        uuid_type=UUID,
        durable_error_type=RuntimeTargetHostDurableFinancialError,
        parent_collector=collect_release_bound_target_host_evidence,
        parent_error_types=(RuntimeTargetHostMeasurementError, RuntimeBudgetError),
        durable_binder=bind_durable_financial_latency_to_target_host_measurement,
    )
)
