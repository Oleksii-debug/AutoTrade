"""Qualification gate for adopting the pinned LEAN runtime without creating a second OMS.

This module does not claim the external runtime is built.  It makes the evidence
required for admission explicit and machine-checkable so architecture prose cannot be
mistaken for integration proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json


LEAN_PIN = "985ef30ad3ac774218c5ac516b4cb0aa2655730f"


@dataclass(frozen=True)
class LeanAdoptionEvidence:
    source_commit: str
    source_archive_sha256: str
    windows_clean_build: bool
    linux_clean_build: bool
    canonical_decimal_vectors: bool
    callback_order_vectors: bool
    shutdown_restart_equivalent: bool
    simulator_bridge_verified: bool
    second_oms_absent: bool
    exact_source_license_recorded: bool
    evidence_ids: tuple[str, ...]

    def validate(self) -> None:
        if not self.source_commit.strip() or not self.source_archive_sha256.strip():
            raise ValueError("source identity is required")
        if not self.evidence_ids or any(not item.strip() for item in self.evidence_ids):
            raise ValueError("evidence_ids are required")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")

    def fingerprint(self) -> str:
        self.validate()
        payload = {key: getattr(self, key) for key in self.__dataclass_fields__}
        payload["evidence_ids"] = list(self.evidence_ids)
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class LeanAdoptionQualification:
    passed: bool
    reasons: tuple[str, ...]
    evidence_fingerprint: str


def qualify_lean_adoption(evidence: LeanAdoptionEvidence) -> LeanAdoptionQualification:
    evidence.validate()
    reasons: list[str] = []
    if evidence.source_commit != LEAN_PIN:
        reasons.append("source_commit_not_pinned_baseline")
    required = {
        "windows_clean_build": evidence.windows_clean_build,
        "linux_clean_build": evidence.linux_clean_build,
        "canonical_decimal_vectors": evidence.canonical_decimal_vectors,
        "callback_order_vectors": evidence.callback_order_vectors,
        "shutdown_restart_equivalent": evidence.shutdown_restart_equivalent,
        "simulator_bridge_verified": evidence.simulator_bridge_verified,
        "second_oms_absent": evidence.second_oms_absent,
        "exact_source_license_recorded": evidence.exact_source_license_recorded,
    }
    reasons.extend(f"missing_{name}" for name, value in required.items() if not value)
    return LeanAdoptionQualification(
        passed=not reasons,
        reasons=tuple(reasons),
        evidence_fingerprint=evidence.fingerprint(),
    )
