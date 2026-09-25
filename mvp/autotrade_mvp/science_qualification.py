"""Independent whole-science qualification for AutoTrade candidate evidence.

This layer does not optimize models, select trades, or promote candidates.  It audits
already-produced evidence and returns PASS, FAIL or INCONCLUSIVE.  A strong backtest
metric is deliberately not an input: protocol validity, causal leakage, untouched
holdout/forward evidence, retention, promotion controls, ablations and uncertainty are.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Callable

from research.autotrade_research.artifacts.store import ArtifactStore

from .qualification_attestation import (
    AcceptedQualificationAttestation,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    verify_qualification_attestation,
)


_VALID_STATUS = {"PASS", "FAIL", "INCONCLUSIVE"}
_VALID_CLAIMS = {"NONE", "RESEARCH_CANDIDATE", "ECONOMIC_EDGE_QUALIFIED"}
_REQUIRED_GATES = (
    "protocol",
    "leakage",
    "holdout",
    "retention",
    "promotion",
    "ablation",
    "uncertainty",
    "forward_evidence",
)

GateEvidenceVerifier = Callable[["QualificationGate"], bool]

_QUALIFICATION_DOMAIN = "SCIENCE"
_QUALIFICATION_GATE = "ECONOMIC_EDGE"
_QUALIFICATION_PACKAGE = "WP-56"
_QUALIFICATION_PROTOCOL_VERSION = "1.0.0"
_QUALIFICATION_REQUIREMENT = "scientific-learning-qualification"


def _git_sha_identity(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or value != value.lower()
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be a lowercase 40-character Git SHA")
    return value


def _sha256_identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{name} must use sha256:<64 hex>")
    digest = value[7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return value


@dataclass(frozen=True)
class QualificationGate:
    gate_id: str
    status: str
    evidence_hashes: tuple[str, ...]
    candidate_hash: str
    frozen_protocol_hash: str
    input_snapshot_hash: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.gate_id, str) or not self.gate_id.strip():
            raise ValueError("gate_id is required")
        if self.gate_id not in _REQUIRED_GATES:
            raise ValueError("gate_id is not a supported scientific qualification gate")
        if self.status not in _VALID_STATUS:
            raise ValueError("gate status must be PASS, FAIL or INCONCLUSIVE")
        if not isinstance(self.evidence_hashes, tuple) or not self.evidence_hashes:
            raise ValueError("gate evidence must be a non-empty tuple")
        for item in self.evidence_hashes:
            _sha256_identity(item, "gate evidence hash")
        _sha256_identity(self.candidate_hash, "gate candidate_hash")
        _sha256_identity(self.frozen_protocol_hash, "gate frozen_protocol_hash")
        _sha256_identity(self.input_snapshot_hash, "gate input_snapshot_hash")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.reason_codes
        ):
            raise ValueError("reason_codes must be a tuple of non-empty strings")
        if self.status == "FAIL" and not self.reason_codes:
            raise ValueError("failed gate requires a reason code")


@dataclass(frozen=True)
class ScientificQualificationInput:
    candidate_hash: str
    frozen_protocol_hash: str
    input_snapshot_hash: str
    gates: tuple[QualificationGate, ...]
    economic_claim: str
    holdout_used_for_tuning: bool = False
    future_information_used_for_routing: bool = False
    population_coverage_hash: str | None = None
    source_sha: str | None = None

    def __post_init__(self) -> None:
        _sha256_identity(self.candidate_hash, "candidate_hash")
        _sha256_identity(self.frozen_protocol_hash, "frozen_protocol_hash")
        _sha256_identity(self.input_snapshot_hash, "input_snapshot_hash")
        if self.economic_claim not in _VALID_CLAIMS:
            raise ValueError("economic_claim is not supported")
        if not isinstance(self.gates, tuple):
            raise TypeError("gates must be a tuple")
        if any(not isinstance(gate, QualificationGate) for gate in self.gates):
            raise TypeError("gates must contain QualificationGate values")
        if not isinstance(self.holdout_used_for_tuning, bool):
            raise TypeError("holdout_used_for_tuning must be boolean")
        if not isinstance(self.future_information_used_for_routing, bool):
            raise TypeError("future_information_used_for_routing must be boolean")
        if self.population_coverage_hash is not None:
            _sha256_identity(
                self.population_coverage_hash,
                "population_coverage_hash",
            )
        if self.source_sha is not None:
            _git_sha_identity(self.source_sha, "source_sha")
        ids = [gate.gate_id for gate in self.gates]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate qualification gate")


@dataclass(frozen=True)
class ScientificQualificationResult:
    qualification_id: str
    status: str
    economic_claim_accepted: bool
    checks: tuple[tuple[str, str], ...]
    reason_codes: tuple[str, ...]
    release_or_trading_authority: bool = False
    qualification_attestation_id: str | None = None
    qualification_attestation_digest: str | None = None
    qualification_policy_id: str | None = None
    qualification_trust_root_id: str | None = None


def _required_signed_bindings(
    evidence: ScientificQualificationInput,
) -> frozenset[str]:
    bindings = {
        _QUALIFICATION_REQUIREMENT,
        f"candidate/{evidence.candidate_hash}",
        f"input/{evidence.input_snapshot_hash}",
        *(f"gate/{gate_id}" for gate_id in _REQUIRED_GATES),
    }
    if evidence.population_coverage_hash is not None:
        bindings.add(f"population/{evidence.population_coverage_hash}")
    return frozenset(bindings)


def _required_evidence_digests(
    evidence: ScientificQualificationInput,
) -> frozenset[str]:
    digests = {
        digest
        for gate in evidence.gates
        for digest in gate.evidence_hashes
    }
    if evidence.population_coverage_hash is not None:
        digests.add(evidence.population_coverage_hash)
    return frozenset(digests)


def qualify_scientific_learning(
    evidence: ScientificQualificationInput,
    *,
    evidence_verifier: GateEvidenceVerifier | None = None,
    qualification_receipt: SignedQualificationAttestation | None = None,
    qualification_policy: QualificationTrustPolicy | None = None,
    evidence_store: ArtifactStore | None = None,
    expected_policy_id: str | None = None,
    expected_policy_version: str | None = None,
) -> ScientificQualificationResult:
    """Audit scientific evidence without granting release or trading authority.

    ``evidence_verifier`` is retained only for source compatibility. A
    caller-selected callback is not an independent trust boundary and can
    never make a gate terminally VERIFIED.
    """
    if not isinstance(evidence, ScientificQualificationInput):
        raise TypeError("evidence must be ScientificQualificationInput")
    by_id = {gate.gate_id: gate for gate in evidence.gates}
    checks: list[tuple[str, str]] = []
    reasons: list[str] = []
    effective_status: dict[str, str] = {}
    verification_state: dict[str, str] = {}

    accepted: AcceptedQualificationAttestation | None = None
    signed_digest_set: frozenset[str] = frozenset()
    signed_requirement_set: frozenset[str] = frozenset()
    trust_status = "INCONCLUSIVE"
    trust_inputs = (
        qualification_receipt,
        qualification_policy,
        evidence_store,
        expected_policy_id,
        expected_policy_version,
    )
    if all(value is None for value in trust_inputs):
        reasons.append("SCIENCE.INDEPENDENT_ATTESTATION_MISSING")
    elif any(value is None for value in trust_inputs) or evidence.source_sha is None:
        reasons.append("SCIENCE.INDEPENDENT_ATTESTATION_INCOMPLETE")
    else:
        try:
            accepted = verify_qualification_attestation(
                qualification_receipt,
                policy=qualification_policy,
                evidence_store=evidence_store,
                expected_policy_id=expected_policy_id,
                expected_policy_version=expected_policy_version,
                expected_source_sha=evidence.source_sha,
                expected_domain=_QUALIFICATION_DOMAIN,
                expected_gate=_QUALIFICATION_GATE,
                expected_package_id=_QUALIFICATION_PACKAGE,
                expected_protocol_id=evidence.frozen_protocol_hash,
                expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
                expected_requirement_id=_QUALIFICATION_REQUIREMENT,
            )
        except (QualificationTrustError, TypeError, ValueError):
            trust_status = "FAIL"
            reasons.append("SCIENCE.INDEPENDENT_ATTESTATION_INVALID")
        else:
            signed_digest_set = frozenset(
                item.sha256
                for item in qualification_receipt.attestation.evidence_refs
            )
            signed_requirement_set = frozenset(
                qualification_receipt.attestation.requirement_ids
            )
            if accepted.result == "FAIL":
                trust_status = "FAIL"
                reasons.append("SCIENCE.INDEPENDENT_ATTESTATION_RESULT_FAIL")
            elif accepted.result != "PASS":
                reasons.append("SCIENCE.INDEPENDENT_ATTESTATION_INCONCLUSIVE")
            elif not _required_signed_bindings(evidence) <= signed_requirement_set:
                trust_status = "FAIL"
                reasons.append("SCIENCE.INDEPENDENT_ATTESTATION_BINDING_MISMATCH")
            elif not _required_evidence_digests(evidence) <= signed_digest_set:
                trust_status = "FAIL"
                reasons.append("SCIENCE.INDEPENDENT_ATTESTATION_EVIDENCE_MISMATCH")
            else:
                trust_status = "PASS"

    checks.append(("independent_attestation", trust_status))

    for gate_id in _REQUIRED_GATES:
        gate = by_id.get(gate_id)
        if gate is None:
            checks.append((gate_id, "INCONCLUSIVE"))
            effective_status[gate_id] = "INCONCLUSIVE"
            verification_state[gate_id] = "MISSING"
            reasons.append("SCIENCE.MISSING_GATE:" + gate_id)
            continue
        binding_ok = (
            gate.candidate_hash == evidence.candidate_hash
            and gate.frozen_protocol_hash == evidence.frozen_protocol_hash
            and gate.input_snapshot_hash == evidence.input_snapshot_hash
        )
        if not binding_ok:
            checks.append((gate_id, "FAIL"))
            effective_status[gate_id] = "FAIL"
            verification_state[gate_id] = "BINDING_FAIL"
            reasons.append("SCIENCE.EVIDENCE_BINDING_MISMATCH:" + gate_id)
            continue

        verified = (
            trust_status == "PASS"
            and f"gate/{gate_id}" in signed_requirement_set
            and all(digest in signed_digest_set for digest in gate.evidence_hashes)
        )
        if not verified:
            checks.append((gate_id, "INCONCLUSIVE"))
            effective_status[gate_id] = "INCONCLUSIVE"
            verification_state[gate_id] = "UNVERIFIED"
            reasons.append("SCIENCE.EVIDENCE_UNVERIFIED:" + gate_id)
            continue

        verification_state[gate_id] = "VERIFIED"
        checks.append((gate_id, gate.status))
        effective_status[gate_id] = gate.status
        if gate.status == "FAIL":
            reasons.extend(gate.reason_codes or ("SCIENCE.GATE_FAILED:" + gate_id,))
        elif gate.status == "INCONCLUSIVE":
            reasons.append("SCIENCE.GATE_INCONCLUSIVE:" + gate_id)

    retention_gate = by_id.get("retention")
    if evidence.population_coverage_hash is None:
        checks.append(("population_coverage", "INCONCLUSIVE"))
        reasons.append("SCIENCE.POPULATION_COVERAGE_MISSING")
    elif (
        retention_gate is None
        or evidence.population_coverage_hash not in retention_gate.evidence_hashes
    ):
        checks.append(("population_coverage", "FAIL"))
        reasons.append("SCIENCE.POPULATION_COVERAGE_NOT_BOUND_TO_RETENTION")
    else:
        checks.append(("population_coverage", "PASS"))

    if evidence.holdout_used_for_tuning:
        checks.append(("holdout_usage", "FAIL"))
        reasons.append("SCIENCE.HOLDOUT_MISUSE")
    else:
        checks.append(("holdout_usage", "PASS"))

    if evidence.future_information_used_for_routing:
        checks.append(("routing_causality", "FAIL"))
        reasons.append("SCIENCE.FUTURE_LEAKAGE")
    else:
        checks.append(("routing_causality", "PASS"))

    claim_ok = True
    claim_status = "PASS"
    if (
        evidence.economic_claim == "ECONOMIC_EDGE_QUALIFIED"
        and effective_status.get("forward_evidence") != "PASS"
    ):
        claim_ok = False
        reasons.append("SCIENCE.CLAIM_EXCEEDS_EVIDENCE")
        claim_status = (
            "INCONCLUSIVE"
            if verification_state.get("forward_evidence") in {"MISSING", "UNVERIFIED"}
            else "FAIL"
        )
    if evidence.economic_claim == "RESEARCH_CANDIDATE":
        blockers = tuple(
            gate_id
            for gate_id in ("protocol", "leakage")
            if effective_status.get(gate_id) != "PASS"
        )
        if blockers:
            claim_ok = False
            reasons.append("SCIENCE.CLAIM_EXCEEDS_EVIDENCE")
            claim_status = (
                "INCONCLUSIVE"
                if all(
                    verification_state.get(gate_id) in {"MISSING", "UNVERIFIED"}
                    for gate_id in blockers
                )
                else "FAIL"
            )
    checks.append(("economic_claim", claim_status))

    has_fail = any(status == "FAIL" for _, status in checks)
    has_inconclusive = any(status == "INCONCLUSIVE" for _, status in checks)
    status = "FAIL" if has_fail else ("INCONCLUSIVE" if has_inconclusive else "PASS")
    economic_claim_accepted = claim_ok and status == "PASS"

    canonical = json.dumps(
        {
            "candidate_hash": evidence.candidate_hash,
            "source_sha": evidence.source_sha,
            "protocol": evidence.frozen_protocol_hash,
            "snapshot": evidence.input_snapshot_hash,
            "claim": evidence.economic_claim,
            "holdout_used_for_tuning": evidence.holdout_used_for_tuning,
            "future_information_used_for_routing": evidence.future_information_used_for_routing,
            "population_coverage_hash": evidence.population_coverage_hash,
            "gates": [
                {
                    "gate_id": gate.gate_id,
                    "status": gate.status,
                    "evidence_hashes": list(gate.evidence_hashes),
                    "candidate_hash": gate.candidate_hash,
                    "frozen_protocol_hash": gate.frozen_protocol_hash,
                    "input_snapshot_hash": gate.input_snapshot_hash,
                    "reason_codes": list(gate.reason_codes),
                }
                for gate in sorted(evidence.gates, key=lambda item: item.gate_id)
            ],
            "checks": checks,
            "reasons": sorted(set(reasons)),
            "qualification_attestation": (
                None
                if accepted is None
                else {
                    "attestation_id": accepted.attestation_id,
                    "attestation_digest": accepted.attestation_digest,
                    "policy_id": accepted.policy_id,
                    "trust_root_id": accepted.trust_root_id,
                    "result": accepted.result,
                }
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    qualification_id = "science-" + sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return ScientificQualificationResult(
        qualification_id=qualification_id,
        status=status,
        economic_claim_accepted=economic_claim_accepted,
        checks=tuple(checks),
        reason_codes=tuple(dict.fromkeys(reasons)),
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
    )
