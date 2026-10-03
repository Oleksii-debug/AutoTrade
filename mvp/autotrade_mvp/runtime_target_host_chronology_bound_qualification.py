"""Fail-closed WP-65 facade over the accepted runtime chronology consumer.

The predecessor implementation remains byte-for-byte in the private impl module.
This facade composes WP-65 terminal receipt horizons into each WP-48 current-cut
revalidation so no standalone caller-owned chronology horizon can authorize PASS.
Production trust dependencies are captured once; focused tests use a private
factory rather than rebinding the production authority path.
"""

from __future__ import annotations

import sys
from types import MappingProxyType

from . import _runtime_target_host_chronology_bound_qualification_impl as _impl
from . import runtime_target_host_plan_bound_qualification as _plan_bound


_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_ACCEPTED_TEXT_FIELDS = (
    "attestation_id",
    "attestation_digest",
    "source_sha",
    "scenario_id",
    "spec_digest",
    "configuration_hash",
    "host_fingerprint",
    "workload_profile_hash",
    "journal_store_identity_digest",
    "release_artifact_id",
    "release_artifact_sha256",
    "binding_artifact_id",
    "binding_sha256",
)
_ACCEPTED_MAPPING_FIELDS = (
    "evidence_sha256_by_kind",
    "payload_artifact_id_by_kind",
    "payload_sha256_by_kind",
    "collector_by_kind",
)


def _snapshot_inert_text_map(value: object, *, name: str) -> dict[str, str]:
    """Detach one constructor-owned mapping without invoking caller callbacks."""

    if type(value) is not _MAPPING_PROXY_TYPE:
        raise _impl.RuntimeTargetHostChronologyBindingError(
            f"{name} must remain exact immutable mapping state"
        )
    detached = dict(value)
    for key, item in detached.items():
        if type(key) is not str or not key or type(item) is not str or not item:
            raise _impl.RuntimeTargetHostChronologyBindingError(
                f"{name} must contain only exact non-empty text pairs"
            )
    return detached


def _snapshot_terminal_qualification(
    value: _impl.AcceptedComposedRuntimeTargetHostQualification,
) -> _impl.AcceptedComposedRuntimeTargetHostQualification:
    """Detach verifier-owned terminal authority before returning it to consumers."""

    if type(value) is not _impl.AcceptedComposedRuntimeTargetHostQualification:
        raise _impl.RuntimeTargetHostChronologyBindingError(
            "WP-65 verifier returned non-canonical composed acceptance"
        )
    accepted = value.qualification
    if type(accepted) is not _impl.AcceptedRuntimeTargetHostQualification:
        raise _impl.RuntimeTargetHostChronologyBindingError(
            "WP-65 verifier returned non-canonical signed acceptance"
        )

    accepted_text = {
        field: _impl._exact_text(
            getattr(accepted, field),
            name=f"signed WP-65 {field}",
        )
        for field in _ACCEPTED_TEXT_FIELDS
    }
    accepted_maps = {
        field: _snapshot_inert_text_map(
            getattr(accepted, field),
            name=f"signed WP-65 {field}",
        )
        for field in _ACCEPTED_MAPPING_FIELDS
    }
    accepted_snapshot = _impl.AcceptedRuntimeTargetHostQualification(
        **accepted_text,
        **accepted_maps,
    )
    return _impl.AcceptedComposedRuntimeTargetHostQualification(
        qualification=accepted_snapshot,
        target_host_measurement_digest=_impl._exact_text(
            value.target_host_measurement_digest,
            name="signed WP-65 target_host_measurement_digest",
        ),
        durable_financial_binding_digest=_impl._exact_text(
            value.durable_financial_binding_digest,
            name="signed WP-65 durable_financial_binding_digest",
        ),
        projection_sha256_by_kind=_snapshot_inert_text_map(
            value.projection_sha256_by_kind,
            name="signed WP-65 projection_sha256_by_kind",
        ),
    )


def _horizon_observer(_chronology: object, *_claimed_instants: str) -> None:
    """Compatibility/test observer only; it is deliberately not authority."""

    return None


def _build_terminal_verifier(
    *,
    receipt_snapshotter,
    measurement_snapshotter,
    exact_text,
    current_cut_verifier,
    pre_binding_checker,
    plan_verifier,
    terminal_snapshotter,
    cross_binding_checker,
    horizon_observer,
    accepted_result_type,
    trusted_cut_type,
    release_runtime_scope,
    binding_error_type,
):
    """Build one verifier whose trust dependencies cannot be retargeted later."""

    def verify_chronology_bound_runtime_target_host_qualification(
        receipt: _impl.SignedQualificationAttestation,
        *,
        evidence_store: _impl.ArtifactStore,
        evidence_root: str,
        journal_store: _impl.JournalStore,
        recovery: _impl.RecoveryController,
        runtime: _impl.ProductionHostRuntime,
        chronology_cut: _impl.TrustedChronologyCut,
        plan_id: str,
        spec: _impl.RuntimeBudgetSpec,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
        campaign_plan: _impl.RuntimeCampaignPlan,
        campaign_cut: _impl.RuntimeCampaignCut,
        measurement: _impl.TargetHostMeasurementArtifact,
    ) -> _impl.AcceptedChronologyBoundRuntimeTargetHostQualification:
        receipt_authority = receipt_snapshotter(receipt)
        receipt_for_verifier = receipt_snapshotter(receipt_authority)
        measurement_authority = measurement_snapshotter(measurement)
        measurement_for_verifier = measurement_snapshotter(measurement_authority)

        receipt_attestation_id = exact_text(
            receipt_authority.attestation.attestation_id,
            name="signed qualification attestation_id",
        )
        receipt_attestation_digest = exact_text(
            receipt_authority.attestation.content_digest,
            name="signed qualification attestation digest",
        )
        completed_at = exact_text(
            receipt_authority.attestation.completed_at,
            name="signed qualification completed_at",
        )
        signed_at = exact_text(
            receipt_authority.attestation.signed_at,
            name="signed qualification signed_at",
        )
        source_sha = exact_text(
            measurement_authority.source_sha,
            name="target-host measurement source_sha",
        )
        journal_store_identity_digest = exact_text(
            measurement_authority.journal_store_identity_digest,
            name="target-host measurement JournalStore identity",
        )
        measurement_digest = exact_text(
            measurement_authority.digest,
            name="target-host measurement digest",
        )

        if type(chronology_cut) is not trusted_cut_type:
            raise TypeError("chronology_cut must be exact TrustedChronologyCut")
        if chronology_cut.scope is not release_runtime_scope:
            raise binding_error_type(
                "terminal WP-65 requires RELEASE_RUNTIME trusted chronology"
            )

        claimed_instants = (completed_at, signed_at)
        chronology = current_cut_verifier(
            store=journal_store,
            recovery=recovery,
            cut=chronology_cut,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=source_sha,
            expected_scope=release_runtime_scope,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
            runtime=runtime,
            claimed_instants=claimed_instants,
        )
        pre_binding_checker(
            chronology,
            source_sha=source_sha,
            journal_store_identity_digest=journal_store_identity_digest,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
        horizon_observer(chronology, completed_at, signed_at)

        qualification = plan_verifier(
            receipt_for_verifier,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            journal_store=journal_store,
            plan_id=plan_id,
            spec=spec,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
            campaign_plan=campaign_plan,
            campaign_cut=campaign_cut,
            measurement=measurement_for_verifier,
        )
        qualification = terminal_snapshotter(qualification)
        cross_binding_checker(
            qualification,
            chronology,
            receipt_attestation_id=receipt_attestation_id,
            receipt_attestation_digest=receipt_attestation_digest,
            source_sha=source_sha,
            journal_store_identity_digest=journal_store_identity_digest,
            measurement_digest=measurement_digest,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )

        final_chronology = current_cut_verifier(
            store=journal_store,
            recovery=recovery,
            cut=chronology,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=source_sha,
            expected_scope=release_runtime_scope,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
            runtime=runtime,
            claimed_instants=claimed_instants,
        )
        if final_chronology != chronology:
            raise binding_error_type(
                "trusted runtime chronology changed during terminal WP-65 verification"
            )
        horizon_observer(final_chronology, completed_at, signed_at)
        cross_binding_checker(
            qualification,
            final_chronology,
            receipt_attestation_id=receipt_attestation_id,
            receipt_attestation_digest=receipt_attestation_digest,
            source_sha=source_sha,
            journal_store_identity_digest=journal_store_identity_digest,
            measurement_digest=measurement_digest,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )
        return accepted_result_type(
            qualification=qualification,
            chronology_cut=final_chronology,
        )

    return verify_chronology_bound_runtime_target_host_qualification


def _build_terminal_verifier_for_tests():
    """Build an explicitly injected verifier from current test seams only."""

    return _build_terminal_verifier(
        receipt_snapshotter=_impl._snapshot_signed_receipt,
        measurement_snapshotter=_impl._snapshot_measurement,
        exact_text=_impl._exact_text,
        current_cut_verifier=_impl.require_current_trusted_chronology_cut,
        pre_binding_checker=_impl._require_pre_binding,
        plan_verifier=_impl.verify_declared_plan_runtime_target_host_qualification,
        terminal_snapshotter=_impl._snapshot_terminal_qualification,
        cross_binding_checker=_impl._require_cross_binding,
        horizon_observer=_impl.require_chronology_horizon,
        accepted_result_type=_impl.AcceptedChronologyBoundRuntimeTargetHostQualification,
        trusted_cut_type=_impl.TrustedChronologyCut,
        release_runtime_scope=_impl.ChronologyScope.RELEASE_RUNTIME,
        binding_error_type=_impl.RuntimeTargetHostChronologyBindingError,
    )


def _build_product_dispatcher(*, terminal_verifier, lower_verifier):
    """Capture the canonical terminal dispatcher for the product-facing entry."""

    signed_receipt_type = _impl.SignedQualificationAttestation
    composition_error_type = _plan_bound.RuntimeTargetHostCompositionError

    def verify_declared_plan_runtime_target_host_qualification(
        receipt,
        *,
        evidence_store,
        evidence_root,
        journal_store,
        plan_id,
        spec,
        expected_release_artifact_id,
        expected_release_artifact_sha256,
        campaign_plan=None,
        campaign_cut=None,
        measurement=None,
        recovery=None,
        runtime=None,
        chronology_cut=None,
    ):
        chronology_values = (recovery, runtime, chronology_cut)
        chronology_supplied = any(value is not None for value in chronology_values)
        if chronology_supplied and any(value is None for value in chronology_values):
            raise composition_error_type(
                "terminal WP-65 qualification requires complete RELEASE_RUNTIME chronology authority"
            )
        if chronology_supplied:
            return terminal_verifier(
                receipt,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
                journal_store=journal_store,
                recovery=recovery,
                runtime=runtime,
                chronology_cut=chronology_cut,
                plan_id=plan_id,
                spec=spec,
                expected_release_artifact_id=expected_release_artifact_id,
                expected_release_artifact_sha256=expected_release_artifact_sha256,
                campaign_plan=campaign_plan,
                campaign_cut=campaign_cut,
                measurement=measurement,
            )
        if type(receipt) is signed_receipt_type:
            raise composition_error_type(
                "terminal WP-65 qualification requires accepted RELEASE_RUNTIME chronology authority"
            )
        return lower_verifier(
            receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            journal_store=journal_store,
            plan_id=plan_id,
            spec=spec,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
            campaign_plan=campaign_plan,
            campaign_cut=campaign_cut,
            measurement=measurement,
        )

    return verify_declared_plan_runtime_target_host_qualification


# Horizon authority already lives inside current-cut verification via claimed_instants.
# Keep this symbol as a test observer, never as a production trust dependency.
_impl.require_chronology_horizon = _horizon_observer
_impl._snapshot_terminal_qualification = _snapshot_terminal_qualification
_impl._build_terminal_verifier = _build_terminal_verifier
_impl._build_terminal_verifier_for_tests = _build_terminal_verifier_for_tests
_impl._build_product_dispatcher = _build_product_dispatcher

_PRODUCTION_TERMINAL_VERIFIER = _build_terminal_verifier(
    receipt_snapshotter=_impl._snapshot_signed_receipt,
    measurement_snapshotter=_impl._snapshot_measurement,
    exact_text=_impl._exact_text,
    current_cut_verifier=_impl.require_current_trusted_chronology_cut,
    pre_binding_checker=_impl._require_pre_binding,
    plan_verifier=_impl.verify_declared_plan_runtime_target_host_qualification,
    terminal_snapshotter=_snapshot_terminal_qualification,
    cross_binding_checker=_impl._require_cross_binding,
    horizon_observer=_horizon_observer,
    accepted_result_type=_impl.AcceptedChronologyBoundRuntimeTargetHostQualification,
    trusted_cut_type=_impl.TrustedChronologyCut,
    release_runtime_scope=_impl.ChronologyScope.RELEASE_RUNTIME,
    binding_error_type=_impl.RuntimeTargetHostChronologyBindingError,
)
_PRODUCTION_PRODUCT_DISPATCHER = _build_product_dispatcher(
    terminal_verifier=_PRODUCTION_TERMINAL_VERIFIER,
    lower_verifier=_plan_bound._verify_declared_plan_runtime_target_host_qualification_without_chronology,
)

_impl.verify_chronology_bound_runtime_target_host_qualification = (
    _PRODUCTION_TERMINAL_VERIFIER
)
_plan_bound.verify_declared_plan_runtime_target_host_qualification = (
    _PRODUCTION_PRODUCT_DISPATCHER
)
sys.modules[__name__] = _impl
