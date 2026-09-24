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


def _sha256_identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{name} must use sha256:<64 hex>")
    digest = value[7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise ValueError(f"{name} must use sha256:<64 hex>")
    return value.lower()


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
        if not self.gate_id.strip():
            raise ValueError("gate_id is required")
        if self.status not in _VALID_STATUS:
            raise ValueError("gate status must be PASS, FAIL or INCONCLUSIVE")
        if not self.evidence_hashes:
            raise ValueError("gate evidence is required")
        for item in self.evidence_hashes:
            _sha256_identity(item, "gate evidence hash")
        _sha256_identity(self.candidate_hash, "gate candidate_hash")
        _sha256_identity(self.frozen_protocol_hash, "gate frozen_protocol_hash")
        _sha256_identity(self.input_snapshot_hash, "gate input_snapshot_hash")
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

    def __post_init__(self) -> None:
        _sha256_identity(self.candidate_hash, "candidate_hash")
        _sha256_identity(self.frozen_protocol_hash, "frozen_protocol_hash")
        _sha256_identity(self.input_snapshot_hash, "input_snapshot_hash")
        if self.economic_claim not in _VALID_CLAIMS:
            raise ValueError("economic_claim is not supported")
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


def qualify_scientific_learning(evidence: ScientificQualificationInput) -> ScientificQualificationResult:
    """Audit scientific evidence without creating financial or release authority."""
    by_id = {gate.gate_id: gate for gate in evidence.gates}
    checks: list[tuple[str, str]] = []
    reasons: list[str] = []

    for gate_id in _REQUIRED_GATES:
        gate = by_id.get(gate_id)
        if gate is None:
            checks.append((gate_id, "INCONCLUSIVE"))
            reasons.append("SCIENCE.MISSING_GATE:" + gate_id)
            continue
        binding_ok = (
            gate.candidate_hash == evidence.candidate_hash
            and gate.frozen_protocol_hash == evidence.frozen_protocol_hash
            and gate.input_snapshot_hash == evidence.input_snapshot_hash
        )
        if not binding_ok:
            checks.append((gate_id, "FAIL"))
            reasons.append("SCIENCE.EVIDENCE_BINDING_MISMATCH:" + gate_id)
            continue
        checks.append((gate_id, gate.status))
        if gate.status == "FAIL":
            reasons.extend(gate.reason_codes or ("SCIENCE.GATE_FAILED:" + gate_id,))
        elif gate.status == "INCONCLUSIVE":
            reasons.append("SCIENCE.GATE_INCONCLUSIVE:" + gate_id)

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

    forward = by_id.get("forward_evidence")
    claim_ok = True
    if evidence.economic_claim == "ECONOMIC_EDGE_QUALIFIED" and (forward is None or forward.status != "PASS"):
        claim_ok = False
        reasons.append("SCIENCE.CLAIM_EXCEEDS_EVIDENCE")
    if evidence.economic_claim == "RESEARCH_CANDIDATE":
        protocol = by_id.get("protocol")
        leakage = by_id.get("leakage")
        if protocol is None or leakage is None or protocol.status != "PASS" or leakage.status != "PASS":
            claim_ok = False
            reasons.append("SCIENCE.CLAIM_EXCEEDS_EVIDENCE")
    checks.append(("economic_claim", "PASS" if claim_ok else "FAIL"))

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
