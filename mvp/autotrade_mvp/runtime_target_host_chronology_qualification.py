"""Terminal WP-65 composition with current RELEASE_RUNTIME trusted chronology.

This adapter composes the existing durable-plan target-host verifier with WP-48's
accepted trusted chronology cut.  It does not create a clock, signer, trust root,
release identity, workload measurement, provider authority, or trading authority.
Terminal authority is returned only when the same signed WP-65 receipt, exact
delivered release, current production runtime occurrence, and signed receipt time
horizon all survive their existing canonical trust boundaries.
"""

from __future__ import annotations

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore
from .production_host import ProductionHostRuntime
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    SignedQualificationAttestation,
    verify_canonical_qualification_attestation,
)
from .recovery import RecoveryController
from .runtime_target_host_plan_bound_qualification import (
    verify_declared_plan_runtime_target_host_qualification,
)
from .runtime_target_host_qualification import (
    DOMAIN,
    GATE,
    PACKAGE_ID,
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    REQUIREMENT_ID,
    AcceptedRuntimeTargetHostQualification,
)
from .trusted_chronology import ChronologyScope
from .trusted_chronology_cut import (
    TrustedChronologyCut,
    require_chronology_horizon,
    require_current_trusted_chronology_cut,
)


class RuntimeTargetHostChronologyError(ValueError):
    """Raised when WP-65 evidence cannot cross the trusted chronology boundary."""


def verify_declared_plan_runtime_target_host_qualification_with_release_chronology(
    receipt: SignedQualificationAttestation,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    journal_store: JournalStore,
    plan_id: str,
    spec: RuntimeBudgetSpec,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
    chronology_cut: TrustedChronologyCut,
    recovery: RecoveryController,
    runtime: ProductionHostRuntime,
) -> AcceptedRuntimeTargetHostQualification:
    """Return WP-65 authority only inside the exact release/runtime UTC horizon.

    The target-host verifier owns workload-plan, measurement, signer/policy and
    delivered-release verification.  This adapter re-verifies the *same* signed
    receipt through the canonical qualification boundary so its signed time
    instants are available, then requires a current WP-48 RELEASE_RUNTIME cut for
    that accepted source/release/runtime and proves all three signed instants are
    covered by the cut's conservative UTC horizon.
    """

    accepted = verify_declared_plan_runtime_target_host_qualification(
        receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        journal_store=journal_store,
        plan_id=plan_id,
        spec=spec,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )

    canonical = verify_canonical_qualification_attestation(
        receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=accepted.source_sha,
        expected_domain=DOMAIN,
        expected_gate=GATE,
        expected_package_id=PACKAGE_ID,
        expected_protocol_id=PROTOCOL_ID,
        expected_protocol_version=PROTOCOL_VERSION,
        expected_requirement_id=REQUIREMENT_ID,
        expected_release_artifact_id=accepted.release_artifact_id,
        expected_release_artifact_sha256=accepted.release_artifact_sha256,
    )
    if type(canonical) is not AcceptedQualificationAttestation:
        raise RuntimeTargetHostChronologyError(
            "canonical verifier returned non-canonical WP-65 acceptance"
        )
    if (
        canonical.attestation_id != accepted.attestation_id
        or canonical.attestation_digest != accepted.attestation_digest
    ):
        raise RuntimeTargetHostChronologyError(
            "chronology receipt differs from accepted WP-65 qualification"
        )

    signed_horizon = (
        canonical.started_at,
        canonical.completed_at,
        canonical.signed_at,
    )
    durable_cut = require_current_trusted_chronology_cut(
        store=journal_store,
        recovery=recovery,
        cut=chronology_cut,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=accepted.source_sha,
        expected_scope=ChronologyScope.RELEASE_RUNTIME,
        expected_release_artifact_id=accepted.release_artifact_id,
        expected_release_artifact_sha256=accepted.release_artifact_sha256,
        runtime=runtime,
    )
    require_chronology_horizon(durable_cut, *signed_horizon)
    return accepted
