"""Fail-closed WP-65 terminal authority over accepted runtime chronology.

The predecessor implementation remains in ``_runtime_target_host_chronology_bound_qualification_impl``.
This facade seals the authority-bearing dependency graph at import time so later
module-global rebinding cannot redirect a terminal WP-65 acceptance path.
"""

from __future__ import annotations

import sys

from . import _runtime_target_host_chronology_bound_qualification_impl as _impl
from . import runtime_target_host_plan_bound_qualification as _plan_bound


def _horizon_observer(_chronology: object, *_claimed_instants: str) -> None:
    """Compatibility observer only; current-cut verification owns horizon authority."""

    return None


def _build_receipt_snapshot(
    *,
    evidence_ref_type,
    attestation_type,
    signed_receipt_type,
    binding_error_type,
):
    """Build one callback-free signed-receipt snapshotter from captured exact types."""

    evidence_ref_fields = frozenset(
        {"artifact_id", "sha256", "media_type", "evidence_kind", "source_sha"}
    )
    attestation_fields = frozenset(
        {
            "attestation_id",
            "source_sha",
            "domain",
            "gate",
            "package_id",
            "protocol_id",
            "protocol_version",
            "requirement_ids",
            "evidence_refs",
            "producer_id",
            "verifier_id",
            "trust_root_id",
            "runner_id",
            "harness_version",
            "started_at",
            "completed_at",
            "signed_at",
            "result",
            "unresolved_limits",
            "release_artifact_id",
            "release_artifact_sha256",
            "schema_version",
            "verification_method",
        }
    )
    signed_receipt_fields = frozenset({"attestation", "signature_b64"})

    def detached_exact_state(
        value: object,
        *,
        expected_fields: frozenset[str],
        name: str,
    ) -> dict[str, object]:
        raw_state = value.__dict__
        if type(raw_state) is not dict:
            raise binding_error_type(f"{name} state must remain an exact built-in dict")
        state = dict.copy(raw_state)
        keys = tuple(state)
        if not all(type(key) is str for key in keys):
            raise binding_error_type(f"{name} state keys must remain exact text")
        actual_fields = frozenset(keys)
        if actual_fields != expected_fields:
            missing = sorted(expected_fields - actual_fields)
            extra = sorted(actual_fields - expected_fields)
            raise binding_error_type(
                f"{name} fields mismatch: missing={missing} extra={extra}"
            )
        return state

    def snapshot_evidence_ref(value):
        if type(value) is not evidence_ref_type:
            raise TypeError("receipt evidence ref must be exact EvidenceArtifactRef")
        state = detached_exact_state(
            value,
            expected_fields=evidence_ref_fields,
            name="receipt evidence ref",
        )
        return evidence_ref_type(
            artifact_id=state["artifact_id"],
            sha256=state["sha256"],
            media_type=state["media_type"],
            evidence_kind=state["evidence_kind"],
            source_sha=state["source_sha"],
        )

    def snapshot_signed_receipt(value):
        if type(value) is not signed_receipt_type:
            raise TypeError("receipt must be exact SignedQualificationAttestation")
        receipt_state = detached_exact_state(
            value,
            expected_fields=signed_receipt_fields,
            name="signed receipt",
        )
        attestation = receipt_state["attestation"]
        if type(attestation) is not attestation_type:
            raise TypeError("receipt attestation must be exact QualificationAttestation")
        state = detached_exact_state(
            attestation,
            expected_fields=attestation_fields,
            name="receipt attestation",
        )

        requirement_ids = state["requirement_ids"]
        evidence_refs = state["evidence_refs"]
        unresolved_limits = state["unresolved_limits"]
        if type(requirement_ids) is not tuple:
            raise binding_error_type("receipt requirement_ids must remain an exact tuple")
        if type(evidence_refs) is not tuple:
            raise binding_error_type("receipt evidence_refs must remain an exact tuple")
        if type(unresolved_limits) is not tuple:
            raise binding_error_type("receipt unresolved_limits must remain an exact tuple")

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
            if type(state[field]) is not str:
                raise binding_error_type(
                    f"receipt {field} must remain exact inert text"
                )
        for field in ("release_artifact_id", "release_artifact_sha256"):
            field_value = state[field]
            if field_value is not None and type(field_value) is not str:
                raise binding_error_type(
                    f"receipt {field} must remain exact inert text or None"
                )
        signature_b64 = receipt_state["signature_b64"]
        if type(signature_b64) is not str:
            raise binding_error_type(
                "receipt signature_b64 must remain exact inert text"
            )

        snapshot = attestation_type(
            attestation_id=state["attestation_id"],
            source_sha=state["source_sha"],
            domain=state["domain"],
            gate=state["gate"],
            package_id=state["package_id"],
            protocol_id=state["protocol_id"],
            protocol_version=state["protocol_version"],
            requirement_ids=tuple(requirement_ids),
            evidence_refs=tuple(snapshot_evidence_ref(ref) for ref in evidence_refs),
            producer_id=state["producer_id"],
            verifier_id=state["verifier_id"],
            trust_root_id=state["trust_root_id"],
            runner_id=state["runner_id"],
            harness_version=state["harness_version"],
            started_at=state["started_at"],
            completed_at=state["completed_at"],
            signed_at=state["signed_at"],
            result=state["result"],
            unresolved_limits=tuple(unresolved_limits),
            release_artifact_id=state["release_artifact_id"],
            release_artifact_sha256=state["release_artifact_sha256"],
            schema_version=state["schema_version"],
            verification_method=state["verification_method"],
        )
        return signed_receipt_type(
            attestation=snapshot,
            signature_b64=signature_b64,
        )

    return snapshot_signed_receipt


def _build_measurement_snapshot(
    *,
    snapshot_target_host_measurement,
    measurement_error_type,
    binding_error_type,
):
    """Capture the canonical raw measurement snapshot dependency exactly once."""

    def snapshot_measurement(value):
        try:
            return snapshot_target_host_measurement(value)
        except measurement_error_type as error:
            raise binding_error_type(str(error)) from error

    return snapshot_measurement


def _build_terminal_verifier(
    *,
    snapshot_signed_receipt,
    snapshot_measurement,
    trusted_cut_type,
    chronology_scope_type,
    binding_error_type,
    composed_acceptance_type,
    signed_acceptance_type,
    accepted_terminal_type,
    require_current_cut,
    verify_plan_qualification,
):
    """Construct the terminal verifier from closure-captured canonical authority."""

    def exact_text(value: object, *, name: str) -> str:
        if type(value) is not str or not value:
            raise binding_error_type(f"{name} must remain exact non-empty text")
        return value

    def require_pre_binding(
        chronology,
        *,
        source_sha: str,
        journal_store_identity_digest: str,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
    ) -> None:
        if type(chronology) is not trusted_cut_type:
            raise binding_error_type("chronology verifier returned non-canonical cut")
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
            if (
                type(observed) is not str
                or type(expected) is not str
                or observed != expected
            ):
                raise binding_error_type(
                    f"runtime chronology {name} does not match terminal WP-65 authority"
                )

    def require_cross_binding(
        qualification,
        chronology,
        *,
        receipt_attestation_id: str,
        receipt_attestation_digest: str,
        source_sha: str,
        journal_store_identity_digest: str,
        measurement_digest: str,
        expected_release_artifact_id: str,
        expected_release_artifact_sha256: str,
    ) -> None:
        if type(qualification) is not composed_acceptance_type:
            raise binding_error_type(
                "WP-65 verifier returned non-canonical composed acceptance"
            )
        accepted = qualification.qualification
        if type(accepted) is not signed_acceptance_type:
            raise binding_error_type(
                "WP-65 verifier returned non-canonical signed acceptance"
            )
        require_pre_binding(
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
            if (
                type(observed) is not str
                or type(expected) is not str
                or observed != expected
            ):
                raise binding_error_type(
                    f"signed WP-65 {name} does not match runtime chronology authority"
                )

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
        receipt_authority = snapshot_signed_receipt(receipt)
        receipt_for_verifier = snapshot_signed_receipt(receipt_authority)
        measurement_authority = snapshot_measurement(measurement)
        measurement_for_verifier = snapshot_measurement(measurement_authority)

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
        if chronology_cut.scope is not chronology_scope_type.RELEASE_RUNTIME:
            raise binding_error_type(
                "terminal WP-65 requires RELEASE_RUNTIME trusted chronology"
            )

        claimed_instants = (completed_at, signed_at)
        chronology = require_current_cut(
            store=journal_store,
            recovery=recovery,
            cut=chronology_cut,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=source_sha,
            expected_scope=chronology_scope_type.RELEASE_RUNTIME,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
            runtime=runtime,
            claimed_instants=claimed_instants,
        )
        require_pre_binding(
            chronology,
            source_sha=source_sha,
            journal_store_identity_digest=journal_store_identity_digest,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
        )

        qualification = verify_plan_qualification(
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
        require_cross_binding(
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

        final_chronology = require_current_cut(
            store=journal_store,
            recovery=recovery,
            cut=chronology,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=source_sha,
            expected_scope=chronology_scope_type.RELEASE_RUNTIME,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=expected_release_artifact_sha256,
            runtime=runtime,
            claimed_instants=claimed_instants,
        )
        if final_chronology != chronology:
            raise binding_error_type(
                "trusted runtime chronology changed during terminal WP-65 verification"
            )
        require_cross_binding(
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
        return accepted_terminal_type(
            qualification=qualification,
            chronology_cut=final_chronology,
        )

    return verify_chronology_bound_runtime_target_host_qualification


_snapshot_signed_receipt = _build_receipt_snapshot(
    evidence_ref_type=_impl.EvidenceArtifactRef,
    attestation_type=_impl.QualificationAttestation,
    signed_receipt_type=_impl.SignedQualificationAttestation,
    binding_error_type=_impl.RuntimeTargetHostChronologyBindingError,
)
_snapshot_measurement = _build_measurement_snapshot(
    snapshot_target_host_measurement=_impl.snapshot_target_host_measurement,
    measurement_error_type=_impl.RuntimeTargetHostMeasurementError,
    binding_error_type=_impl.RuntimeTargetHostChronologyBindingError,
)
verify_chronology_bound_runtime_target_host_qualification = _build_terminal_verifier(
    snapshot_signed_receipt=_snapshot_signed_receipt,
    snapshot_measurement=_snapshot_measurement,
    trusted_cut_type=_impl.TrustedChronologyCut,
    chronology_scope_type=_impl.ChronologyScope,
    binding_error_type=_impl.RuntimeTargetHostChronologyBindingError,
    composed_acceptance_type=_impl.AcceptedComposedRuntimeTargetHostQualification,
    signed_acceptance_type=_impl.AcceptedRuntimeTargetHostQualification,
    accepted_terminal_type=_impl.AcceptedChronologyBoundRuntimeTargetHostQualification,
    require_current_cut=_impl.require_current_trusted_chronology_cut,
    verify_plan_qualification=_impl.verify_declared_plan_runtime_target_host_qualification,
)

# Preserve compatibility seam names for diagnostic tests, but terminal authority
# no longer resolves them after construction.
_impl.require_chronology_horizon = _horizon_observer
_impl._build_receipt_snapshot = _build_receipt_snapshot
_impl._build_measurement_snapshot = _build_measurement_snapshot
_impl._build_terminal_verifier = _build_terminal_verifier
_impl.verify_chronology_bound_runtime_target_host_qualification = (
    verify_chronology_bound_runtime_target_host_qualification
)
_plan_bound._bind_terminal_chronology_verifier(
    verify_chronology_bound_runtime_target_host_qualification
)
sys.modules[__name__] = _impl
