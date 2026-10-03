"""Bind terminal WP-65 target-host qualification to accepted RELEASE_RUNTIME chronology.

WP-65 owns target-host performance evidence. WP-48 owns independently accepted
chronology for one exact production-runtime occurrence. This adapter consumes both
read-only. It does not mint time, signer, release, provider, PAPER/LIVE, economic
edge, or trading authority.

The chronology authority is checked before and after signed/artifact verification
so verifier side effects cannot move recovery ownership, clock-incident generation,
runtime occurrence, or durable chronology authority behind a terminal PASS. The
signed WP-65 completion/signature instants must also fit inside the conservative
trusted chronology horizon; local wall clock and artifact/journal storage times are
never used as substitutes.
"""

from __future__ import annotations

from dataclasses import dataclass

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore
from .production_host import ProductionHostRuntime
from .qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)
from .recovery import RecoveryController
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_composed_qualification import (
    AcceptedComposedRuntimeTargetHostQualification,
)
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
    snapshot_target_host_measurement,
)
from .runtime_target_host_plan_bound_qualification import (
    _verify_declared_plan_runtime_target_host_qualification_without_chronology,
)
from .runtime_target_host_qualification import AcceptedRuntimeTargetHostQualification
from .trusted_chronology import ChronologyScope
from .trusted_chronology_cut import (
    TrustedChronologyCut,
    require_chronology_horizon,
    require_current_trusted_chronology_cut,
)


class RuntimeTargetHostChronologyBindingError(ValueError):
    """Raised when WP-65 and accepted runtime chronology do not bind exactly."""


def _snapshot_evidence_ref(value: EvidenceArtifactRef) -> EvidenceArtifactRef:
    if type(value) is not EvidenceArtifactRef:
        raise TypeError("receipt evidence ref must be exact EvidenceArtifactRef")
    return EvidenceArtifactRef(
        artifact_id=value.artifact_id,
        sha256=value.sha256,
        media_type=value.media_type,
        evidence_kind=value.evidence_kind,
        source_sha=value.source_sha,
    )


def _snapshot_signed_receipt(
    value: SignedQualificationAttestation,
) -> SignedQualificationAttestation:
    """Detach canonical signed authority before any external verifier can run."""

    if type(value) is not SignedQualificationAttestation:
        raise TypeError("receipt must be exact SignedQualificationAttestation")
    attestation = value.attestation
    if type(attestation) is not QualificationAttestation:
        raise TypeError("receipt attestation must be exact QualificationAttestation")
    if type(attestation.requirement_ids) is not tuple:
        raise RuntimeTargetHostChronologyBindingError(
            "receipt requirement_ids must remain an exact tuple"
        )
    if type(attestation.evidence_refs) is not tuple:
        raise RuntimeTargetHostChronologyBindingError(
            "receipt evidence_refs must remain an exact tuple"
        )
    if type(attestation.unresolved_limits) is not tuple:
        raise RuntimeTargetHostChronologyBindingError(
            "receipt unresolved_limits must remain an exact tuple"
        )
    snapshot = QualificationAttestation(
        attestation_id=attestation.attestation_id,
        source_sha=attestation.source_sha,
        domain=attestation.domain,
        gate=attestation.gate,
        package_id=attestation.package_id,
        protocol_id=attestation.protocol_id,
        protocol_version=attestation.protocol_version,
        requirement_ids=tuple(attestation.requirement_ids),
        evidence_refs=tuple(
            _snapshot_evidence_ref(ref) for ref in attestation.evidence_refs
        ),
        producer_id=attestation.producer_id,
        verifier_id=attestation.verifier_id,
        trust_root_id=attestation.trust_root_id,
        runner_id=attestation.runner_id,
        harness_version=attestation.harness_version,
        started_at=attestation.started_at,
        completed_at=attestation.completed_at,
        signed_at=attestation.signed_at,
        result=attestation.result,
        unresolved_limits=tuple(attestation.unresolved_limits),
        release_artifact_id=attestation.release_artifact_id,
        release_artifact_sha256=attestation.release_artifact_sha256,
        schema_version=attestation.schema_version,
        verification_method=attestation.verification_method,
    )
    return SignedQualificationAttestation(
        attestation=snapshot,
        signature_b64=value.signature_b64,
    )


def _snapshot_measurement(
    value: TargetHostMeasurementArtifact,
) -> TargetHostMeasurementArtifact:
    try:
        return snapshot_target_host_measurement(value)
    except RuntimeTargetHostMeasurementError as error:
        raise RuntimeTargetHostChronologyBindingError(str(error)) from error


def _require_pre_binding(
    chronology: TrustedChronologyCut,
    measurement: TargetHostMeasurementArtifact,
    *,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
) -> None:
    if type(chronology) is not TrustedChronologyCut:
        raise RuntimeTargetHostChronologyBindingError(
            "chronology verifier returned non-canonical cut"
        )
    bindings = (
        ("source SHA", chronology.source_sha, measurement.source_sha),
        (
            "release artifact id",
            chronology.release_artifact_id,
            expected_release_artifact_id,
        ),
        (
            "release artifact digest",
            chronology.release_artifact_sha256,
            expected_release_artifact_sha256,
        ),
        (
            "JournalStore identity",
            chronology.store_identity_digest,
            measurement.journal_store_identity_digest,
        ),
    )
    for name, observed, expected in bindings:
        if type(observed) is not str or type(expected) is not str or observed != expected:
            raise RuntimeTargetHostChronologyBindingError(
                f"runtime chronology {name} does not match terminal WP-65 authority"
            )


def _require_cross_binding(
    qualification: AcceptedComposedRuntimeTargetHostQualification,
    chronology: TrustedChronologyCut,
    measurement: TargetHostMeasurementArtifact,
    *,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
) -> None:
    if type(qualification) is not AcceptedComposedRuntimeTargetHostQualification:
        raise RuntimeTargetHostChronologyBindingError(
            "WP-65 verifier returned non-canonical composed acceptance"
        )
    accepted = qualification.qualification
    if type(accepted) is not AcceptedRuntimeTargetHostQualification:
        raise RuntimeTargetHostChronologyBindingError(
            "WP-65 verifier returned non-canonical signed acceptance"
        )
    _require_pre_binding(
        chronology,
        measurement,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    bindings = (
        ("source SHA", accepted.source_sha, measurement.source_sha),
        (
            "release artifact id",
            accepted.release_artifact_id,
            expected_release_artifact_id,
        ),
        (
            "release artifact digest",
            accepted.release_artifact_sha256,
            expected_release_artifact_sha256,
        ),
        (
            "JournalStore identity",
            accepted.journal_store_identity_digest,
            measurement.journal_store_identity_digest,
        ),
    )
    for name, observed, expected in bindings:
        if type(observed) is not str or type(expected) is not str or observed != expected:
            raise RuntimeTargetHostChronologyBindingError(
                f"signed WP-65 {name} does not match runtime chronology authority"
            )


@dataclass(frozen=True, slots=True)
class AcceptedChronologyBoundRuntimeTargetHostQualification:
    """Terminal WP-65 result retaining the exact accepted runtime chronology cut."""

    qualification: AcceptedComposedRuntimeTargetHostQualification
    chronology_cut: TrustedChronologyCut

    def __post_init__(self) -> None:
        if type(self.qualification) is not AcceptedComposedRuntimeTargetHostQualification:
            raise TypeError(
                "qualification must be exact AcceptedComposedRuntimeTargetHostQualification"
            )
        if type(self.chronology_cut) is not TrustedChronologyCut:
            raise TypeError("chronology_cut must be exact TrustedChronologyCut")


def verify_chronology_bound_runtime_target_host_qualification(
    receipt: SignedQualificationAttestation,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    journal_store: JournalStore,
    recovery: RecoveryController,
    runtime: ProductionHostRuntime,
    chronology_cut: TrustedChronologyCut,
    plan_id: str,
    spec: RuntimeBudgetSpec,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
    campaign_plan: RuntimeCampaignPlan,
    campaign_cut: RuntimeCampaignCut,
    measurement: TargetHostMeasurementArtifact,
) -> AcceptedChronologyBoundRuntimeTargetHostQualification:
    """Require one current RELEASE_RUNTIME chronology around terminal WP-65 PASS."""

    receipt = _snapshot_signed_receipt(receipt)
    measurement = _snapshot_measurement(measurement)
    if type(chronology_cut) is not TrustedChronologyCut:
        raise TypeError("chronology_cut must be exact TrustedChronologyCut")
    if chronology_cut.scope is not ChronologyScope.RELEASE_RUNTIME:
        raise RuntimeTargetHostChronologyBindingError(
            "terminal WP-65 requires RELEASE_RUNTIME trusted chronology"
        )

    chronology = require_current_trusted_chronology_cut(
        store=journal_store,
        recovery=recovery,
        cut=chronology_cut,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=measurement.source_sha,
        expected_scope=ChronologyScope.RELEASE_RUNTIME,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
        runtime=runtime,
    )
    _require_pre_binding(
        chronology,
        measurement,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    require_chronology_horizon(
        chronology,
        receipt.attestation.completed_at,
        receipt.attestation.signed_at,
    )

    qualification = _verify_declared_plan_runtime_target_host_qualification_without_chronology(
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
    _require_cross_binding(
        qualification,
        chronology,
        measurement,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )

    final_chronology = require_current_trusted_chronology_cut(
        store=journal_store,
        recovery=recovery,
        cut=chronology,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=measurement.source_sha,
        expected_scope=ChronologyScope.RELEASE_RUNTIME,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
        runtime=runtime,
    )
    if final_chronology != chronology:
        raise RuntimeTargetHostChronologyBindingError(
            "trusted runtime chronology changed during terminal WP-65 verification"
        )
    require_chronology_horizon(
        final_chronology,
        receipt.attestation.completed_at,
        receipt.attestation.signed_at,
    )
    _require_cross_binding(
        qualification,
        final_chronology,
        measurement,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    return AcceptedChronologyBoundRuntimeTargetHostQualification(
        qualification=qualification,
        chronology_cut=final_chronology,
    )
