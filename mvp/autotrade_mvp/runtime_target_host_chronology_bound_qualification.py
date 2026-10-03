"""Bind terminal WP-65 target-host qualification to accepted RELEASE_RUNTIME chronology.

WP-65 owns target-host performance evidence. WP-48 owns independently accepted
chronology for one exact production-runtime occurrence. This adapter consumes both
read-only. It does not mint time, signer, release, provider, PAPER/LIVE, economic
edge, or trading authority.

Chronology authority is checked before and after signed/artifact verification so
verifier side effects cannot move recovery ownership, clock-incident generation,
runtime occurrence, or durable chronology authority behind a terminal PASS. Signed
WP-65 completion/signature instants must fit inside the conservative trusted
chronology horizon; local wall clock and artifact/journal storage times are never
used as substitutes.
"""

from __future__ import annotations

from dataclasses import dataclass

from autotrade_runtime.artifacts import ArtifactStore

from . import runtime_target_host_plan_bound_qualification as _plan_bound
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
from .runtime_target_host_qualification import AcceptedRuntimeTargetHostQualification
from .trusted_chronology import ChronologyScope
from .trusted_chronology_cut import (
    TrustedChronologyCut,
    require_chronology_horizon,
    require_current_trusted_chronology_cut,
)

# Isolated local seam: focused tests may replace this symbol, while the product
# entry in runtime_target_host_plan_bound_qualification remains chronology-gated.
verify_declared_plan_runtime_target_host_qualification = (
    _plan_bound._verify_declared_plan_runtime_target_host_qualification_without_chronology
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
    for field in (
        "attestation_id",
        "source_sha",
        "domain",
        "gate",
        "package_id",
        "protocol_id",
        "protocol_version",
        "producer_id",
        "verifier_id",
        "trust_root_id",
        "runner_id",
        "harness_version",
        "started_at",
        "completed_at",
        "signed_at",
        "result",
        "schema_version",
        "verification_method",
    ):
        if type(getattr(attestation, field)) is not str:
            raise RuntimeTargetHostChronologyBindingError(
                f"receipt {field} must remain exact inert text"
            )
    for field in ("release_artifact_id", "release_artifact_sha256"):
        field_value = getattr(attestation, field)
        if field_value is not None and type(field_value) is not str:
            raise RuntimeTargetHostChronologyBindingError(
                f"receipt {field} must remain exact inert text or None"
            )
    if type(value.signature_b64) is not str:
        raise RuntimeTargetHostChronologyBindingError(
            "receipt signature_b64 must remain exact inert text"
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


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value:
        raise RuntimeTargetHostChronologyBindingError(
            f"{name} must remain exact non-empty text"
        )
    return value


def _require_pre_binding(
    chronology: TrustedChronologyCut,
    *,
    source_sha: str,
    journal_store_identity_digest: str,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
) -> None:
    if type(chronology) is not TrustedChronologyCut:
        raise RuntimeTargetHostChronologyBindingError(
            "chronology verifier returned non-canonical cut"
        )
    bindings = (
        ("source SHA", chronology.source_sha, source_sha),
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
            journal_store_identity_digest,
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
    *,
    receipt_attestation_id: str,
    receipt_attestation_digest: str,
    source_sha: str,
    journal_store_identity_digest: str,
    measurement_digest: str,
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
        source_sha=source_sha,
        journal_store_identity_digest=journal_store_identity_digest,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    bindings = (
        ("attestation id", accepted.attestation_id, receipt_attestation_id),
        (
            "attestation digest",
            accepted.attestation_digest,
            receipt_attestation_digest,
        ),
        ("source SHA", accepted.source_sha, source_sha),
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
            journal_store_identity_digest,
        ),
        (
            "target-host measurement digest",
            qualification.target_host_measurement_digest,
            measurement_digest,
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

    receipt_authority = _snapshot_signed_receipt(receipt)
    receipt_for_verifier = _snapshot_signed_receipt(receipt_authority)
    measurement_authority = _snapshot_measurement(measurement)
    measurement_for_verifier = _snapshot_measurement(measurement_authority)

    receipt_attestation_id = _exact_text(
        receipt_authority.attestation.attestation_id,
        name="signed qualification attestation_id",
    )
    receipt_attestation_digest = _exact_text(
        receipt_authority.attestation.content_digest,
        name="signed qualification attestation digest",
    )
    completed_at = _exact_text(
        receipt_authority.attestation.completed_at,
        name="signed qualification completed_at",
    )
    signed_at = _exact_text(
        receipt_authority.attestation.signed_at,
        name="signed qualification signed_at",
    )
    source_sha = _exact_text(
        measurement_authority.source_sha,
        name="target-host measurement source_sha",
    )
    journal_store_identity_digest = _exact_text(
        measurement_authority.journal_store_identity_digest,
        name="target-host measurement JournalStore identity",
    )
    measurement_digest = _exact_text(
        measurement_authority.digest,
        name="target-host measurement digest",
    )

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
        expected_source_sha=source_sha,
        expected_scope=ChronologyScope.RELEASE_RUNTIME,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
        runtime=runtime,
    )
    _require_pre_binding(
        chronology,
        source_sha=source_sha,
        journal_store_identity_digest=journal_store_identity_digest,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )
    require_chronology_horizon(chronology, completed_at, signed_at)

    qualification = verify_declared_plan_runtime_target_host_qualification(
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
    _require_cross_binding(
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

    final_chronology = require_current_trusted_chronology_cut(
        store=journal_store,
        recovery=recovery,
        cut=chronology,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=source_sha,
        expected_scope=ChronologyScope.RELEASE_RUNTIME,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
        runtime=runtime,
    )
    if final_chronology != chronology:
        raise RuntimeTargetHostChronologyBindingError(
            "trusted runtime chronology changed during terminal WP-65 verification"
        )
    require_chronology_horizon(final_chronology, completed_at, signed_at)
    _require_cross_binding(
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
    return AcceptedChronologyBoundRuntimeTargetHostQualification(
        qualification=qualification,
        chronology_cut=final_chronology,
    )
