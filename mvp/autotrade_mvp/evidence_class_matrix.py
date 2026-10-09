"""Plan-7 capability-to-evidence mapping; diagnostic only, never a release gate.

This module catalogs proof *classes* and reports missing or unverified proof.
It neither verifies an issuer's signature (Plan 4 owns trust) nor promotes a
candidate, sends orders, or issues a Plan-9 final release decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping

_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

SOURCE = "SOURCE"
SIMULATION = "SIMULATION"
REPLAY = "REPLAY"
TARGET_HOST_PHYSICAL = "TARGET_HOST_PHYSICAL"
REAL_PROVIDER = "REAL_PROVIDER"
PAPER = "PAPER"
LIVE = "LIVE"
NVDA_PHYSICAL = "NVDA_PHYSICAL"
SIGNED_RELEASE = "SIGNED_RELEASE"

EVIDENCE_CLASSES = frozenset((
    SOURCE, SIMULATION, REPLAY, TARGET_HOST_PHYSICAL, REAL_PROVIDER,
    PAPER, LIVE, NVDA_PHYSICAL, SIGNED_RELEASE,
))

# Declarative, closed-world inventory. The Plan-9 entries describe *missing
# proof requirements*, not a way for this Plan-7 module to certify Plan 9.
_CAPABILITIES = {
    "scientific_gate_engineering": (SOURCE, REPLAY),
    "ablation_component_engineering": (SOURCE, REPLAY),
    "strategy_economics_harness": (SOURCE, SIMULATION, REPLAY),
    "performance_load_harness": (SOURCE, SIMULATION, REPLAY),
    "target_host_performance": (TARGET_HOST_PHYSICAL,),
    "provider_account_qualification": (REAL_PROVIDER,),
    "paper_order_qualification": (PAPER,),
    "live_order_qualification": (LIVE,),
    "physical_nvda_acceptance": (NVDA_PHYSICAL,),
    "delivered_signed_release": (SIGNED_RELEASE,),
}
REQUIRED_EVIDENCE: Mapping[str, tuple[str, ...]] = MappingProxyType(_CAPABILITIES)
PLAN9_OWNED = frozenset((
    "target_host_performance", "provider_account_qualification",
    "paper_order_qualification", "live_order_qualification",
    "physical_nvda_acceptance", "delivered_signed_release",
))


class EvidenceMatrixError(ValueError):
    """Invalid mapping input; fail closed instead of silently dropping it."""


def _identity(value: object, label: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise EvidenceMatrixError(f"{label} must be canonical")
    return value


@dataclass(frozen=True)
class EvidenceReference:
    """An untrusted evidence claim; its fields are NOT proof of authenticity."""

    capability: str
    evidence_class: str
    source_sha: str
    content_digest: str
    evidence_ref: str
    issuer: str
    claimed_status: str

    def __post_init__(self) -> None:
        if self.capability not in REQUIRED_EVIDENCE:
            raise EvidenceMatrixError("unknown capability")
        if self.evidence_class not in EVIDENCE_CLASSES:
            raise EvidenceMatrixError("unknown evidence class")
        _identity(self.source_sha, "source_sha", _SHA)
        _identity(self.content_digest, "content_digest", _DIGEST)
        if type(self.evidence_ref) is not str or not self.evidence_ref.strip() or self.evidence_ref != self.evidence_ref.strip():
            raise EvidenceMatrixError("evidence_ref must be nonempty canonical text")
        if type(self.issuer) is not str or not self.issuer.strip() or self.issuer != self.issuer.strip():
            raise EvidenceMatrixError("issuer must be nonempty canonical text")
        if self.claimed_status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise EvidenceMatrixError("claimed_status must be PASS, FAIL or INCONCLUSIVE")


@dataclass(frozen=True)
class CapabilityCoverage:
    capability: str
    source_sha: str
    required_classes: tuple[str, ...]
    missing_classes: tuple[str, ...]
    unverified_classes: tuple[str, ...]
    rejected_cross_source_classes: tuple[str, ...]
    rejected_wrong_class_count: int
    status: str
    plan9_owned: bool

    def machine_readable(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "source_sha": self.source_sha,
            "required_classes": list(self.required_classes),
            "missing_classes": list(self.missing_classes),
            "unverified_classes": list(self.unverified_classes),
            "rejected_cross_source_classes": list(self.rejected_cross_source_classes),
            "rejected_wrong_class_count": self.rejected_wrong_class_count,
            "status": self.status,
            "plan9_owned": self.plan9_owned,
            "authorizes_trading": False,
            "final_release_eligible": False,
        }


def capability_requirements() -> dict[str, object]:
    """Portable deterministic machine-readable contract; never a PASS ledger."""
    return {
        "schema": "autotrade.plan7.evidence-class-matrix.v1",
        "scope": "DIAGNOSTIC_ONLY",
        "plan9_is_final_release_authority": True,
        "requirements": {
            name: {"classes": list(classes), "plan9_owned": name in PLAN9_OWNED}
            for name, classes in sorted(REQUIRED_EVIDENCE.items())
        },
    }


def inspect_capability_evidence(
    capability: str,
    *,
    source_sha: str,
    references: Iterable[EvidenceReference],
) -> CapabilityCoverage:
    """Describe gaps. A self-declared PASS always remains UNVERIFIED.

    Exactly matching proof classes and candidate Git SHA are required for
    reference inventory. No hierarchy exists: source/simulation/replay cannot
    count as PAPER, LIVE, physical NVDA, real-provider or signed-release proof.
    Only the external owning verifier may authenticate the referenced material.
    """
    if capability not in REQUIRED_EVIDENCE:
        raise EvidenceMatrixError("unknown capability")
    _identity(source_sha, "source_sha", _SHA)
    observed = tuple(references)
    if any(type(ref) is not EvidenceReference for ref in observed):
        raise EvidenceMatrixError("references must be EvidenceReference values")
    seen: set[tuple[str, str, str]] = set()
    for ref in observed:
        key = (ref.capability, ref.evidence_class, ref.evidence_ref)
        if key in seen:
            raise EvidenceMatrixError("duplicate evidence reference")
        seen.add(key)

    required = REQUIRED_EVIDENCE[capability]
    matching = [ref for ref in observed if ref.capability == capability]
    missing: list[str] = []
    unverified: list[str] = []
    stale: list[str] = []
    for evidence_class in required:
        exact = [ref for ref in matching if ref.evidence_class == evidence_class and ref.source_sha == source_sha]
        if exact:
            # Even a forged PASS claim cannot issue an independent verifier PASS.
            unverified.append(evidence_class)
        else:
            missing.append(evidence_class)
            if any(ref.evidence_class == evidence_class and ref.source_sha != source_sha for ref in matching):
                stale.append(evidence_class)
    wrong = sum(ref.evidence_class not in required for ref in matching)
    status = "INCONCLUSIVE"
    if any(ref.claimed_status == "FAIL" for ref in matching if ref.source_sha == source_sha and ref.evidence_class in required):
        status = "FAIL"
    return CapabilityCoverage(
        capability, source_sha, required, tuple(missing), tuple(unverified),
        tuple(stale), wrong, status, capability in PLAN9_OWNED,
    )


def matrix_fingerprint() -> str:
    payload = json.dumps(capability_requirements(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + sha256(payload.encode("ascii")).hexdigest()
