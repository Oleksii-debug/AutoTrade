"""Terminal provenance gate for WP-57 forward-paper qualification.

The causal/statistical mechanics stay in research.autotrade_research.forward_paper.
This module only binds one completed mechanics assessment to immutable campaign
bytes and the shared canonical qualification trust authority. It grants neither
trading authority nor an economic-edge claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Mapping
from uuid import UUID

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from research.autotrade_research.forward_paper import (
    ForwardPaperAssessment,
    ForwardPaperEvidence,
    ForwardPaperProtocol,
    assess_forward_paper,
)

from .qualification_attestation import (
    AcceptedQualificationAttestation,
    QualificationTrustError,
    SignedQualificationAttestation,
    verify_canonical_qualification_attestation,
)


_MEDIA_TYPE = "application/vnd.autotrade.forward-paper-qualification"
_EVIDENCE_KIND = "FORWARD_PAPER_CAMPAIGN"
_DOMAIN = "FORWARD_PAPER"
_GATE = "QUALIFICATION"
_PACKAGE_ID = "WP-57"
_PROTOCOL_ID = "forward-paper-qualification-v1"
_PROTOCOL_VERSION = "1.0.0"
_REQUIREMENT_ID = "forward-paper-terminal-evidence"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ForwardPaperQualificationError(ValueError):
    """Raised when terminal forward-paper evidence identity is malformed."""


def _artifact_id(value: str) -> str:
    if not isinstance(value, str):
        raise ForwardPaperQualificationError("artifact_id must be a canonical UUID")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ForwardPaperQualificationError(
            "artifact_id must be a canonical UUID"
        ) from error
    if normalized != value:
        raise ForwardPaperQualificationError("artifact_id must be a canonical UUID")
    return normalized


def _digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ForwardPaperQualificationError(
            "artifact_sha256 must be canonical sha256"
        )
    return value


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    rendered = format(normalized, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _protocol_payload(protocol: ForwardPaperProtocol) -> dict[str, object]:
    return {
        "campaign_id": protocol.campaign_id,
        "exact_build_sha": protocol.exact_build_sha,
        "protocol_hash": protocol.protocol_hash,
        "registered_at": protocol.registered_at,
        "starts_at": protocol.starts_at,
        "ends_at": protocol.ends_at,
        "minimum_predictions": protocol.minimum_predictions,
        "maximum_decision_latency_ms": protocol.maximum_decision_latency_ms,
        "required_provider_capabilities": list(
            protocol.required_provider_capabilities
        ),
        "required_operational_cases": list(protocol.required_operational_cases),
    }


def _evidence_payload(evidence: ForwardPaperEvidence) -> dict[str, object]:
    return {
        "exact_build_sha": evidence.exact_build_sha,
        "protocol_hash": evidence.protocol_hash,
        "observed_until": evidence.observed_until,
        "predictions": [
            {
                "prediction_id": item.prediction_id,
                "provider_capability": item.provider_capability,
                "input_hash": item.input_hash,
                "proposal_hash": item.proposal_hash,
                "information_cutoff_at": item.information_cutoff_at,
                "sealed_at": item.sealed_at,
                "decision_deadline_at": item.decision_deadline_at,
                "outcome_horizon_end_at": item.outcome_horizon_end_at,
                "decision_latency_ms": item.decision_latency_ms,
            }
            for item in evidence.predictions
        ],
        "outcomes": [
            {
                "prediction_id": item.prediction_id,
                "outcome_hash": item.outcome_hash,
                "outcome_available_at": item.outcome_available_at,
                "evaluated_at": item.evaluated_at,
            }
            for item in evidence.outcomes
        ],
        "operational_observations": [
            {
                "provider_capability": item.provider_capability,
                "case": item.case,
                "observed_at": item.observed_at,
                "reconciled": item.reconciled,
            }
            for item in evidence.operational_observations
        ],
        "costs_by_currency": {
            key: _decimal_text(value)
            for key, value in sorted(evidence.costs_by_currency.items())
        },
        "costs_complete": evidence.costs_complete,
        "account_reconciliation_complete": (
            evidence.account_reconciliation_complete
        ),
    }


def _assessment_payload(
    assessment: ForwardPaperAssessment,
) -> dict[str, object]:
    return {
        "evidence_status": assessment.evidence_status,
        "operational_status": assessment.operational_status,
        "economic_edge_status": assessment.economic_edge_status,
        "reasons": list(assessment.reasons),
        "prediction_count": assessment.prediction_count,
        "evaluated_outcome_count": assessment.evaluated_outcome_count,
    }


def forward_paper_qualification_payload(
    protocol: ForwardPaperProtocol,
    evidence: ForwardPaperEvidence,
) -> dict[str, object]:
    if not isinstance(protocol, ForwardPaperProtocol):
        raise TypeError("protocol must be ForwardPaperProtocol")
    if not isinstance(evidence, ForwardPaperEvidence):
        raise TypeError("evidence must be ForwardPaperEvidence")
    assessment = assess_forward_paper(protocol, evidence)
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "protocol": _protocol_payload(protocol),
        "evidence": _evidence_payload(evidence),
        "assessment": _assessment_payload(assessment),
    }


def forward_paper_qualification_bytes(
    protocol: ForwardPaperProtocol,
    evidence: ForwardPaperEvidence,
) -> bytes:
    return json.dumps(
        forward_paper_qualification_payload(protocol, evidence),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def forward_paper_qualification_metadata(
    protocol: ForwardPaperProtocol,
) -> dict[str, object]:
    if not isinstance(protocol, ForwardPaperProtocol):
        raise TypeError("protocol must be ForwardPaperProtocol")
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "source_sha": protocol.exact_build_sha,
        "campaign_id": protocol.campaign_id,
        "protocol_hash": protocol.protocol_hash,
    }


def _store_matches(
    store: ArtifactStore,
    *,
    protocol: ForwardPaperProtocol,
    evidence: ForwardPaperEvidence,
    artifact_id: str,
    artifact_sha256: str,
) -> bool:
    try:
        manifest = store.load_manifest(artifact_id)
        data = store.read_bytes(artifact_id)
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ):
        return False
    if not isinstance(manifest, Mapping) or not isinstance(data, bytes):
        return False
    if not isinstance(manifest.get("manifest_hash"), str):
        return False
    if manifest.get("artifact_id") != artifact_id:
        return False
    if manifest.get("sha256") != artifact_sha256:
        return False
    if "sha256:" + sha256(data).hexdigest() != artifact_sha256:
        return False
    if data != forward_paper_qualification_bytes(protocol, evidence):
        return False
    if manifest.get("media_type") != _MEDIA_TYPE:
        return False
    if manifest.get("source_refs") != [f"git:{protocol.exact_build_sha}"]:
        return False
    if manifest.get("metadata") != forward_paper_qualification_metadata(protocol):
        return False
    return True


@dataclass(frozen=True)
class ForwardPaperQualificationResult:
    status: str
    mechanics: ForwardPaperAssessment
    reason_codes: tuple[str, ...]
    evidence_artifact_id: str | None
    evidence_artifact_sha256: str | None
    qualification_attestation_id: str | None
    qualification_attestation_digest: str | None
    qualification_policy_id: str | None
    qualification_trust_root_id: str | None
    trading_authority_granted: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ForwardPaperQualificationError(
                "unsupported forward-paper qualification status"
            )
        if not isinstance(self.mechanics, ForwardPaperAssessment):
            raise TypeError("mechanics must be ForwardPaperAssessment")
        if self.mechanics.economic_edge_status != "NOT_ESTABLISHED":
            raise ForwardPaperQualificationError(
                "WP-57 qualification cannot establish economic edge"
            )
        if self.trading_authority_granted:
            raise ForwardPaperQualificationError(
                "WP-57 qualification cannot grant trading authority"
            )


def qualify_forward_paper(
    protocol: ForwardPaperProtocol,
    evidence: ForwardPaperEvidence,
    *,
    evidence_store: ArtifactStore | None = None,
    evidence_artifact_id: str | None = None,
    evidence_artifact_sha256: str | None = None,
    qualification_receipt: SignedQualificationAttestation | None = None,
) -> ForwardPaperQualificationResult:
    """Bind the existing WP-57 mechanics to immutable independently signed evidence."""

    mechanics = assess_forward_paper(protocol, evidence)
    reasons = list(mechanics.reasons)
    accepted: AcceptedQualificationAttestation | None = None

    if mechanics.evidence_status == "INVALID" or mechanics.operational_status == "FAIL":
        status = "FAIL"
    elif (
        mechanics.evidence_status != "VALID"
        or mechanics.operational_status != "PASS"
    ):
        status = "INCONCLUSIVE"
    else:
        status = "PASS"

    identity_missing = (
        evidence_artifact_id is None or evidence_artifact_sha256 is None
    )
    if identity_missing:
        if evidence_artifact_id is not None or evidence_artifact_sha256 is not None:
            reasons.append("forward_paper_artifact_identity_incomplete")
        else:
            reasons.append("forward_paper_artifact_identity_missing")
        if status == "PASS":
            status = "INCONCLUSIVE"
        artifact_id = None
        artifact_sha256 = None
    else:
        artifact_id = _artifact_id(evidence_artifact_id)
        artifact_sha256 = _digest(evidence_artifact_sha256)

    if evidence_store is None:
        reasons.append("immutable_forward_paper_evidence_unavailable")
        if status == "PASS":
            status = "INCONCLUSIVE"
    elif not isinstance(evidence_store, ArtifactStore):
        raise TypeError("evidence_store must be ArtifactStore")
    elif artifact_id is not None and artifact_sha256 is not None:
        if not _store_matches(
            evidence_store,
            protocol=protocol,
            evidence=evidence,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
        ):
            reasons.append("immutable_forward_paper_evidence_mismatch")
            status = "FAIL"

    if qualification_receipt is None:
        reasons.append("independent_forward_paper_trust_unavailable")
        if status == "PASS":
            status = "INCONCLUSIVE"
    elif evidence_store is None or artifact_id is None or artifact_sha256 is None:
        reasons.append("independent_forward_paper_trust_incomplete")
        if status == "PASS":
            status = "INCONCLUSIVE"
    else:
        signed_refs = {
            (
                ref.artifact_id,
                ref.sha256,
                ref.media_type,
                ref.evidence_kind,
                ref.source_sha,
            )
            for ref in qualification_receipt.attestation.evidence_refs
        }
        expected_refs = {
            (
                artifact_id,
                artifact_sha256,
                _MEDIA_TYPE,
                _EVIDENCE_KIND,
                protocol.exact_build_sha,
            )
        }
        signed_requirements = frozenset(
            qualification_receipt.attestation.requirement_ids
        )
        if signed_refs != expected_refs:
            reasons.append("independent_forward_paper_evidence_set_mismatch")
            status = "FAIL"
        if f"protocol/{protocol.protocol_hash}" not in signed_requirements:
            reasons.append("independent_forward_paper_protocol_mismatch")
            status = "FAIL"
        try:
            accepted = verify_canonical_qualification_attestation(
                qualification_receipt,
                evidence_store=evidence_store,
                expected_source_sha=protocol.exact_build_sha,
                expected_domain=_DOMAIN,
                expected_gate=_GATE,
                expected_package_id=_PACKAGE_ID,
                expected_protocol_id=_PROTOCOL_ID,
                expected_protocol_version=_PROTOCOL_VERSION,
                expected_requirement_id=_REQUIREMENT_ID,
            )
        except (QualificationTrustError, TypeError, ValueError):
            reasons.append("independent_forward_paper_trust_invalid")
            status = "FAIL"
        else:
            if accepted.result == "FAIL":
                reasons.append("independent_forward_paper_attestation_failed")
                status = "FAIL"
            elif accepted.result != "PASS":
                reasons.append("independent_forward_paper_attestation_inconclusive")
                if status == "PASS":
                    status = "INCONCLUSIVE"

    return ForwardPaperQualificationResult(
        status=status,
        mechanics=mechanics,
        reason_codes=tuple(dict.fromkeys(reasons)),
        evidence_artifact_id=artifact_id,
        evidence_artifact_sha256=artifact_sha256,
        qualification_attestation_id=(
            None if accepted is None else accepted.attestation_id
        ),
        qualification_attestation_digest=(
            None if accepted is None else accepted.attestation_digest
        ),
        qualification_policy_id=(
            None if accepted is None else accepted.policy_id
        ),
        qualification_trust_root_id=(
            None if accepted is None else accepted.trust_root_id
        ),
        trading_authority_granted=False,
    )
