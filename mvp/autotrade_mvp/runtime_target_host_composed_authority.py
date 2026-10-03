"""Captured production authority graph for composed WP-65 qualification.

The public composed verifier remains an explicit focused-test seam. Product
admission imports the verifier from this module so rebinding the composed module's
binder, signed verifier, snapshotters, helpers, or result type after import cannot
retarget an already constructed production authority path.

This module creates no chronology source, signer/trust root, release authority,
provider/PAPER/LIVE send authority, broker/exchange authority, trading authority,
profitability claim, or economic-edge claim.
"""

from __future__ import annotations

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore
from .qualification_attestation import SignedQualificationAttestation
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_composed_qualification import (
    AcceptedComposedRuntimeTargetHostQualification,
    RuntimeTargetHostCompositionError,
    _require_durable_plan_workload_match,
    _require_signed_campaign_match,
    _snapshot_campaign_cut,
    _snapshot_campaign_plan,
    _snapshot_measurement,
    _snapshot_spec,
    target_host_measurement_projection_digests,
)
from .runtime_target_host_durable_financial import (
    bind_release_bound_durable_financial_latency_to_target_host_measurement,
)
from .runtime_target_host_measurement import TargetHostMeasurementArtifact
from .runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
    verify_runtime_target_host_qualification,
)


def _build_composed_production_verifier(
    *,
    measurement_snapshotter,
    spec_snapshotter,
    campaign_plan_snapshotter,
    campaign_cut_type,
    campaign_cut_snapshotter,
    durable_binder,
    composition_error_type,
    durable_plan_matcher,
    signed_verifier,
    accepted_type,
    signed_campaign_matcher,
    projection_digest_builder,
    composed_type,
):
    """Build a composed verifier whose direct dependencies are immutable captures."""

    def verify(
        receipt: SignedQualificationAttestation,
        *,
        evidence_store: ArtifactStore,
        evidence_root: str,
        journal_store: JournalStore,
        spec: RuntimeBudgetSpec,
        campaign_plan: RuntimeCampaignPlan,
        campaign_cut: RuntimeCampaignCut,
        declared_plan_id: str,
        measurement: TargetHostMeasurementArtifact,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
    ) -> AcceptedComposedRuntimeTargetHostQualification:
        measurement_authority = measurement_snapshotter(measurement)
        spec_authority = spec_snapshotter(spec)
        campaign_plan_authority = campaign_plan_snapshotter(campaign_plan)
        campaign_cut_authority = campaign_cut
        if type(campaign_cut_authority) is campaign_cut_type:
            campaign_cut_authority = campaign_cut_snapshotter(campaign_cut_authority)

        durable_binding = durable_binder(
            store=journal_store,
            spec=spec_authority,
            campaign_plan=campaign_plan_authority,
            campaign_cut=campaign_cut_authority,
            declared_plan_id=declared_plan_id,
            measurement=measurement_authority,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
        if (
            durable_binding.target_host_measurement_digest
            != measurement_authority.digest
            or durable_binding.source_sha != measurement_authority.source_sha
            or durable_binding.spec_digest != measurement_authority.spec_digest
        ):
            raise composition_error_type(
                "durable financial binding does not bind canonical target-host measurement"
            )
        durable_plan_matcher(durable_binding, measurement_authority)

        accepted = signed_verifier(
            receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=measurement_authority.source_sha,
            expected_scenario_id=measurement_authority.scenario_id,
            expected_spec_digest=measurement_authority.spec_digest,
            expected_configuration_hash=measurement_authority.configuration_hash,
            expected_host_fingerprint=measurement_authority.host_fingerprint,
            expected_workload_profile_hash=measurement_authority.workload_profile_hash,
            expected_journal_store_identity_digest=(
                measurement_authority.journal_store_identity_digest
            ),
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
        if type(accepted) is not accepted_type:
            raise composition_error_type(
                "signed target-host verifier returned non-canonical acceptance"
            )

        signed_campaign_matcher(
            accepted,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            measurement=measurement_authority,
            campaign_plan=campaign_plan_authority,
            spec=spec_authority,
        )

        projection_digests = projection_digest_builder(measurement_authority)
        for kind, expected_digest in projection_digests.items():
            actual_digest = accepted.payload_sha256_by_kind.get(kind)
            if actual_digest != expected_digest:
                raise composition_error_type(
                    f"signed {kind} raw payload does not bind canonical target-host measurement"
                )

        return composed_type(
            qualification=accepted,
            target_host_measurement_digest=measurement_authority.digest,
            durable_financial_binding_digest=durable_binding.digest,
            projection_sha256_by_kind=projection_digests,
        )

    return verify


verify_sealed_composed_runtime_target_host_qualification = (
    _build_composed_production_verifier(
        measurement_snapshotter=_snapshot_measurement,
        spec_snapshotter=_snapshot_spec,
        campaign_plan_snapshotter=_snapshot_campaign_plan,
        campaign_cut_type=RuntimeCampaignCut,
        campaign_cut_snapshotter=_snapshot_campaign_cut,
        durable_binder=(
            bind_release_bound_durable_financial_latency_to_target_host_measurement
        ),
        composition_error_type=RuntimeTargetHostCompositionError,
        durable_plan_matcher=_require_durable_plan_workload_match,
        signed_verifier=verify_runtime_target_host_qualification,
        accepted_type=AcceptedRuntimeTargetHostQualification,
        signed_campaign_matcher=_require_signed_campaign_match,
        projection_digest_builder=target_host_measurement_projection_digests,
        composed_type=AcceptedComposedRuntimeTargetHostQualification,
    )
)
