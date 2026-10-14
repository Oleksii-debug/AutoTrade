"""Deterministic Plan-9 claimed-scope and evidence inventory.

This module records what a candidate claims and what it explicitly excludes.
It is deliberately not an evidence verifier, release signer, trading-authority
issuer, or substitute for physical/provider qualification.  Evidence classes
are exact and non-hierarchical: SOURCE/SIMULATION/REPLAY can never satisfy a
REAL_PROVIDER, PAPER, LIVE, NVDA_PHYSICAL, TARGET_HOST_PHYSICAL, or
SIGNED_RELEASE requirement.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from .evidence_class_matrix import (
    LIVE,
    NVDA_PHYSICAL,
    PAPER,
    REAL_PROVIDER,
    REPLAY,
    SIGNED_RELEASE,
    SIMULATION,
    SOURCE,
    TARGET_HOST_PHYSICAL,
    EVIDENCE_CLASSES,
)


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

CLAIMED = "CLAIMED"
NOT_ACTIVATED = "NOT_ACTIVATED"
NOT_CLAIMED = "NOT_CLAIMED"
CLAIM_STATES = frozenset((CLAIMED, NOT_ACTIVATED, NOT_CLAIMED))

_REQUIREMENTS = {
    "historical_replay": (REPLAY,),
    "zero_simulation": (SIMULATION,),
    "accounting": (SOURCE, SIMULATION),
    "risk": (SOURCE, SIMULATION),
    "recovery": (SOURCE, SIMULATION),
    "security": (SOURCE,),
    "accessibility_source": (SOURCE,),
    "target_host_performance": (TARGET_HOST_PHYSICAL,),
    "provenance_source": (SOURCE,),
    "economics_simulation": (SIMULATION,),
    "provider_account": (REAL_PROVIDER,),
    "paper_execution": (PAPER,),
    "live_execution": (LIVE,),
    "physical_nvda": (NVDA_PHYSICAL,),
    "delivered_signed_release": (SIGNED_RELEASE,),
}
REQUIREMENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(_REQUIREMENTS)

_EXTERNAL = frozenset((
    "target_host_performance",
    "provider_account",
    "paper_execution",
    "live_execution",
    "physical_nvda",
    "delivered_signed_release",
))


class Plan9ClaimedScopeError(ValueError):
    """The matrix is ambiguous, incomplete, or attempts evidence promotion."""


def _canonical(value: object, *, name: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise Plan9ClaimedScopeError(f"{name} must be canonical")
    return value


def _text(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise Plan9ClaimedScopeError(f"{name} must be canonical non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class ScopeClaim:
    capability: str
    state: str
    contract_digest: str
    limitation: str

    def __post_init__(self) -> None:
        if self.capability not in REQUIREMENTS:
            raise Plan9ClaimedScopeError("unknown capability")
        if self.state not in CLAIM_STATES:
            raise Plan9ClaimedScopeError("unknown claim state")
        if self.state == NOT_ACTIVATED and self.capability not in _EXTERNAL:
            raise Plan9ClaimedScopeError(
                "NOT_ACTIVATED is reserved for external capabilities"
            )
        _canonical(self.contract_digest, name="contract_digest", pattern=_DIGEST)
        _text(self.limitation, name="limitation")


@dataclass(frozen=True, slots=True)
class EvidencePointer:
    capability: str
    evidence_class: str
    source_sha: str
    package_digest: str
    evidence_digest: str
    evidence_ref: str
    reported_status: str

    def __post_init__(self) -> None:
        if self.capability not in REQUIREMENTS:
            raise Plan9ClaimedScopeError("unknown capability")
        if self.evidence_class not in EVIDENCE_CLASSES:
            raise Plan9ClaimedScopeError("unknown evidence class")
        _canonical(self.source_sha, name="source_sha", pattern=_GIT_SHA)
        _canonical(self.package_digest, name="package_digest", pattern=_DIGEST)
        _canonical(self.evidence_digest, name="evidence_digest", pattern=_DIGEST)
        _text(self.evidence_ref, name="evidence_ref")
        if self.reported_status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise Plan9ClaimedScopeError("reported_status is not canonical")


@dataclass(frozen=True, slots=True)
class ClaimedScopeMatrix:
    source_sha: str
    package_digest: str
    rows: tuple[Mapping[str, object], ...]
    matrix_digest: str

    def machine_readable(self) -> dict[str, object]:
        return {
            "schema": "autotrade.plan9.claimed-scope-matrix.v1",
            "source_sha": self.source_sha,
            "package_digest": self.package_digest,
            "rows": [dict(row) for row in self.rows],
            "matrix_digest": self.matrix_digest,
            "trading_authority": "NOT_AUTHORIZED",
            "real_money": "NOT_AUTHORIZED",
            "final_release": "NOT_AUTHORIZED",
            "evidence_verification": "EXTERNAL_REQUIRED",
        }


def build_claimed_scope_matrix(
    *,
    source_sha: str,
    package_digest: str,
    claims: Iterable[ScopeClaim],
    evidence: Iterable[EvidencePointer],
) -> ClaimedScopeMatrix:
    """Build a complete inventory without authenticating any evidence pointer."""
    source_sha = _canonical(source_sha, name="source_sha", pattern=_GIT_SHA)
    package_digest = _canonical(
        package_digest, name="package_digest", pattern=_DIGEST
    )
    claim_values = tuple(claims)
    evidence_values = tuple(evidence)
    if any(type(item) is not ScopeClaim for item in claim_values):
        raise Plan9ClaimedScopeError("claims must contain exact ScopeClaim values")
    if any(type(item) is not EvidencePointer for item in evidence_values):
        raise Plan9ClaimedScopeError(
            "evidence must contain exact EvidencePointer values"
        )

    claims_by_capability: dict[str, ScopeClaim] = {}
    for claim in claim_values:
        if claim.capability in claims_by_capability:
            raise Plan9ClaimedScopeError("duplicate capability claim")
        claims_by_capability[claim.capability] = claim
    missing_claims = sorted(set(REQUIREMENTS) - set(claims_by_capability))
    extra_claims = sorted(set(claims_by_capability) - set(REQUIREMENTS))
    if missing_claims or extra_claims:
        raise Plan9ClaimedScopeError(
            f"claims must cover the closed capability set; missing={missing_claims}; "
            f"extra={extra_claims}"
        )

    seen_evidence: set[tuple[str, str, str]] = set()
    for pointer in evidence_values:
        key = (pointer.capability, pointer.evidence_class, pointer.evidence_ref)
        if key in seen_evidence:
            raise Plan9ClaimedScopeError("duplicate evidence pointer")
        seen_evidence.add(key)

    rows: list[Mapping[str, object]] = []
    for capability in sorted(REQUIREMENTS):
        claim = claims_by_capability[capability]
        required = REQUIREMENTS[capability]
        candidates = tuple(
            pointer for pointer in evidence_values
            if pointer.capability == capability
        )
        exact = tuple(
            pointer for pointer in candidates
            if pointer.source_sha == source_sha
            and pointer.package_digest == package_digest
        )
        missing_classes = tuple(
            evidence_class for evidence_class in required
            if not any(pointer.evidence_class == evidence_class for pointer in exact)
        )
        wrong_classes = tuple(sorted({
            pointer.evidence_class for pointer in exact
            if pointer.evidence_class not in required
        }))
        stale_count = sum(
            pointer.source_sha != source_sha
            or pointer.package_digest != package_digest
            for pointer in candidates
        )

        if claim.state == CLAIMED:
            status = "FAIL" if any(
                pointer.reported_status == "FAIL"
                and pointer.evidence_class in required
                for pointer in exact
            ) else "INCONCLUSIVE"
        elif claim.state == NOT_ACTIVATED:
            status = "EXPLICITLY_NOT_ACTIVATED"
        else:
            status = "EXPLICITLY_NOT_CLAIMED"

        rows.append(MappingProxyType({
            "capability": capability,
            "claim_state": claim.state,
            "contract_digest": claim.contract_digest,
            "required_evidence_classes": list(required),
            "missing_evidence_classes": list(missing_classes),
            "rejected_wrong_classes": list(wrong_classes),
            "stale_or_cross_package_evidence_count": stale_count,
            "reported_evidence_count": len(exact),
            "status": status,
            "limitation": claim.limitation,
            "independently_verified": False,
            "authorizes_trading": False,
        }))

    payload = {
        "schema": "autotrade.plan9.claimed-scope-matrix.v1",
        "source_sha": source_sha,
        "package_digest": package_digest,
        "rows": [dict(row) for row in rows],
        "trading_authority": "NOT_AUTHORIZED",
        "real_money": "NOT_AUTHORIZED",
        "final_release": "NOT_AUTHORIZED",
        "evidence_verification": "EXTERNAL_REQUIRED",
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return ClaimedScopeMatrix(
        source_sha=source_sha,
        package_digest=package_digest,
        rows=tuple(rows),
        matrix_digest="sha256:" + sha256(canonical).hexdigest(),
    )
