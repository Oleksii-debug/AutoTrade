"""Whole-recovery release evidence gate.

A green test suite is insufficient.  Release recovery requires exact artifact,
backup/restore, crash, restart, ambiguous-send and rollback evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json


@dataclass(frozen=True)
class RecoveryReleaseEvidence:
    source_revision: str
    package_hash: str
    backup_artifact_hash: str
    clean_restore_verified: bool
    backup_integrity_verified: bool
    journal_replay_equivalent: bool
    checkpoint_resume_equivalent: bool
    unknown_send_reconciled: bool
    provider_state_reconciled_after_restart: bool
    stale_owner_fenced: bool
    credential_rotation_exercised: bool
    rollback_verified: bool
    windows_restart_verified: bool
    evidence_ids: tuple[str, ...]

    def validate(self) -> None:
        if not self.source_revision.strip() or not self.package_hash.strip() or not self.backup_artifact_hash.strip():
            raise ValueError("source/package/backup identities are required")
        if not self.evidence_ids or any(not item.strip() for item in self.evidence_ids):
            raise ValueError("durable evidence_ids are required")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")

    def fingerprint(self) -> str:
        self.validate()
        payload = {key: getattr(self, key) for key in self.__dataclass_fields__}
        payload["evidence_ids"] = list(self.evidence_ids)
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RecoveryReleaseQualification:
    passed: bool
    reasons: tuple[str, ...]
    evidence_fingerprint: str


def qualify_recovery_release(evidence: RecoveryReleaseEvidence) -> RecoveryReleaseQualification:
    evidence.validate()
    required = {
        "clean_restore_verified": evidence.clean_restore_verified,
        "backup_integrity_verified": evidence.backup_integrity_verified,
        "journal_replay_equivalent": evidence.journal_replay_equivalent,
        "checkpoint_resume_equivalent": evidence.checkpoint_resume_equivalent,
        "unknown_send_reconciled": evidence.unknown_send_reconciled,
        "provider_state_reconciled_after_restart": evidence.provider_state_reconciled_after_restart,
        "stale_owner_fenced": evidence.stale_owner_fenced,
        "credential_rotation_exercised": evidence.credential_rotation_exercised,
        "rollback_verified": evidence.rollback_verified,
        "windows_restart_verified": evidence.windows_restart_verified,
    }
    reasons = tuple(f"missing_{name}" for name, value in required.items() if not value)
    return RecoveryReleaseQualification(
        passed=not reasons,
        reasons=reasons,
        evidence_fingerprint=evidence.fingerprint(),
    )
