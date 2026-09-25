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


def qualify_scientific_learning(
    evidence: ScientificQualificationInput,
    *,
    evidence_verifier: GateEvidenceVerifier | None = None,
) -> ScientificQualificationResult:
    """Audit independently verified scientific evidence without authority expansion."""
    if not isinstance(evidence, ScientificQualificationInput):
        raise TypeError("evidence must be ScientificQualificationInput")
    by_id = {gate.gate_id: gate for gate in evidence.gates}
    checks: list[tuple[str, str]] = []
    reasons: list[str] = []
    effective_status: dict[str, str] = {}
    verification_state: dict[str, str] = {}

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

        verified = False
        if evidence_verifier is not None:
            try:
                verification = evidence_verifier(gate)
            except Exception:
                verification = False
            if not isinstance(verification, bool):
                verification = False
            verified = verification
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
    )
