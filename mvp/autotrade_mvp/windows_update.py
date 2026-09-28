"""Fail-closed Windows update and rollback planning for AutoTrade.

This module does not install binaries, mutate the durable journal, clear restore
reconciliation gates, or grant trading authority. It consumes frozen release
candidate manifests plus verified backup/migration evidence and emits a
deterministic update plan. Execution remains a separate, qualified release task.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any

from research.autotrade_research.artifacts.store import ArtifactStore

from .qualification_attestation import (
    QualificationTrustError,
    QualificationTrustPolicy,
    parse_signed_qualification_attestation,
)
from .release_candidate import (
    ReleaseArtifactEvidence,
    ReleaseCandidateDecision,
    ReleaseCandidateError,
    ReleaseCandidateInput,
    freeze_release_candidate,
)


class WindowsUpdateError(ValueError):
    """Raised when update/rollback evidence is malformed."""


@dataclass(frozen=True)
class WindowsUpdateTrustContext:
    """Pinned trust inputs required to consume a frozen release downstream."""

    evidence_store: ArtifactStore
    qualification_policy: QualificationTrustPolicy
    expected_policy_id: str
    expected_policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_store, ArtifactStore):
            raise TypeError("evidence_store must be ArtifactStore")
        if not isinstance(self.qualification_policy, QualificationTrustPolicy):
            raise TypeError(
                "qualification_policy must be QualificationTrustPolicy"
            )
        policy_id = _text(
            self.expected_policy_id,
            name="expected_policy_id",
        )
        policy_version = _text(
            self.expected_policy_version,
            name="expected_policy_version",
        )
        if self.qualification_policy.policy_id != policy_id:
            raise WindowsUpdateError(
                "expected_policy_id does not match pinned qualification policy"
            )
        if self.qualification_policy.policy_version != policy_version:
            raise WindowsUpdateError(
                "expected_policy_version does not match pinned qualification policy"
            )
        object.__setattr__(self, "expected_policy_id", policy_id)
        object.__setattr__(self, "expected_policy_version", policy_version)


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_INSTALL_STEPS = (
    "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH",
    "QUIESCE_NEW_ADMISSIONS",
    "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
    "VERIFY_PRE_UPDATE_BACKUP",
    "STOP_AND_FENCE_FINANCIAL_SENDER",
    "INSTALL_CANDIDATE_SIDE_BY_SIDE",
    "APPLY_VERIFIED_SCHEMA_TRANSITION_IF_REQUIRED",
    "START_DEGRADED_NO_TRADING_AUTHORITY",
    "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY",
    "REESTABLISH_AUTHENTICATED_PROVIDER_SESSIONS",
    "RUN_POST_UPDATE_RECONCILIATION",
    "VERIFY_HOST_UI_COMPATIBILITY",
    "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
)
_ROLLBACK_STEPS = (
    "QUIESCE_NEW_ADMISSIONS",
    "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
    "STOP_AND_FENCE_FINANCIAL_SENDER",
    "RESTORE_OR_REINSTALL_PREVIOUS_QUALIFIED_STATE",
    "START_DEGRADED_NO_TRADING_AUTHORITY",
    "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY",
    "REESTABLISH_AUTHENTICATED_PROVIDER_SESSIONS",
    "RUN_POST_RESTORE_RECONCILIATION",
    "VERIFY_HOST_UI_COMPATIBILITY",
    "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
)


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WindowsUpdateError(f"{name} is required")
    return value.strip()


def _sha256(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise WindowsUpdateError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return text


def _git_sha(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _GIT_SHA.fullmatch(text) is None:
        raise WindowsUpdateError(
            f"{name} must be an exact 40- or 64-character lowercase Git object id"
        )
    return text


def _positive_version(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WindowsUpdateError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class BackupEvidence:
    manifest_sha256: str
    source_sha: str
    journal_schema_version: int
    verification_status: str
    reconciliation_required_after_restore: bool

    def __post_init__(self) -> None:
        status = _text(
            self.verification_status,
            name="verification_status",
        ).upper()
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise WindowsUpdateError("unsupported backup verification_status")
        if not isinstance(self.reconciliation_required_after_restore, bool):
            raise WindowsUpdateError(
                "reconciliation_required_after_restore must be bool"
            )
        object.__setattr__(
            self,
            "manifest_sha256",
            _sha256(self.manifest_sha256, name="manifest_sha256"),
        )
        object.__setattr__(
            self,
            "source_sha",
            _git_sha(self.source_sha, name="source_sha"),
        )
        object.__setattr__(
            self,
            "journal_schema_version",
            _positive_version(
                self.journal_schema_version,
                name="journal_schema_version",
            ),
        )
        object.__setattr__(self, "verification_status", status)


@dataclass(frozen=True)
class MigrationEvidence:
    from_schema_version: int
    to_schema_version: int
    source_sha: str
    evidence_sha256: str
    verification_status: str
    rollback_mode: str
    reverse_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        from_version = _positive_version(
            self.from_schema_version,
            name="from_schema_version",
        )
        to_version = _positive_version(
            self.to_schema_version,
            name="to_schema_version",
        )
        if from_version == to_version:
            raise WindowsUpdateError(
                "migration evidence requires an actual schema transition"
            )
        status = _text(
            self.verification_status,
            name="verification_status",
        ).upper()
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise WindowsUpdateError("unsupported migration verification_status")
        rollback_mode = _text(self.rollback_mode, name="rollback_mode").upper()
        if rollback_mode not in {
            "REVERSIBLE_MIGRATION",
            "RESTORE_PRE_UPDATE_BACKUP",
        }:
            raise WindowsUpdateError("unsupported migration rollback_mode")
        object.__setattr__(self, "from_schema_version", from_version)
        object.__setattr__(self, "to_schema_version", to_version)
        object.__setattr__(
            self,
            "source_sha",
            _git_sha(self.source_sha, name="source_sha"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256(self.evidence_sha256, name="evidence_sha256"),
        )
        reverse_digest = self.reverse_evidence_sha256
        if rollback_mode == "REVERSIBLE_MIGRATION":
            if reverse_digest is None:
                raise WindowsUpdateError(
                    "reversible migration requires reverse_evidence_sha256"
                )
            reverse_digest = _sha256(
                reverse_digest,
                name="reverse_evidence_sha256",
            )
            if reverse_digest == self.evidence_sha256:
                raise WindowsUpdateError(
                    "forward and reverse migration evidence must be distinct"
                )
        elif reverse_digest is not None:
            raise WindowsUpdateError(
                "backup-restore rollback cannot claim reverse migration evidence"
            )
        object.__setattr__(self, "verification_status", status)
        object.__setattr__(self, "rollback_mode", rollback_mode)
        object.__setattr__(self, "reverse_evidence_sha256", reverse_digest)


@dataclass(frozen=True)
class WindowsUpdatePlan:
    status: str
    reasons: tuple[str, ...]
    plan_json: str | None
    plan_sha256: str | None

    def __post_init__(self) -> None:
        if self.status not in {"PLAN_READY", "BLOCKED"}:
            raise WindowsUpdateError("unsupported update plan status")
        if self.status == "PLAN_READY":
            if self.reasons:
                raise WindowsUpdateError(
                    "ready update plan cannot contain blocking reasons"
                )
            if self.plan_json is None or self.plan_sha256 is None:
                raise WindowsUpdateError(
                    "ready update plan requires canonical plan evidence"
                )
        else:
            if not self.reasons:
                raise WindowsUpdateError(
                    "blocked update plan requires at least one reason"
                )
            if self.plan_json is not None or self.plan_sha256 is not None:
                raise WindowsUpdateError(
                    "blocked update plan cannot publish an executable plan"
                )


def _release_manifest(
    decision: ReleaseCandidateDecision,
    *,
    name: str,
    trust: WindowsUpdateTrustContext,
) -> dict[str, Any]:
    """Validate and project an already-qualified immutable frozen release.

    Release qualification is an upstream authority. This consumer does not
    manufacture a second PASS by re-freezing self-asserted evidence without the
    trust inputs used by WP-54. Instead it validates the exact frozen manifest,
    its qualification provenance identifiers and all artifact identities before
    constructing an update plan.
    """

    if not isinstance(decision, ReleaseCandidateDecision):
        raise TypeError(f"{name} must be ReleaseCandidateDecision")
    if not isinstance(trust, WindowsUpdateTrustContext):
        raise TypeError("trust must be WindowsUpdateTrustContext")
    if decision.status != "FROZEN":
        raise WindowsUpdateError(f"{name} must be a frozen release candidate")
    if decision.manifest_json is None or decision.manifest_sha256 is None:
        raise WindowsUpdateError(f"{name} has no frozen manifest")
    expected_digest = "sha256:" + sha256(
        decision.manifest_json.encode("utf-8")
    ).hexdigest()
    if _sha256(
        decision.manifest_sha256,
        name=f"{name}.manifest_sha256",
    ) != expected_digest:
        raise WindowsUpdateError(f"{name} manifest digest does not match bytes")
    try:
        manifest = json.loads(decision.manifest_json)
    except json.JSONDecodeError as error:
        raise WindowsUpdateError(f"{name} manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise WindowsUpdateError(f"{name} manifest must be an object")
    expected_manifest_fields = {
        "release_id",
        "source_sha",
        "baseline_hash",
        "schema_contract_hash",
        "artifacts",
        "qualification",
    }
    if set(manifest) != expected_manifest_fields:
        raise WindowsUpdateError(f"{name} manifest structure is not canonical")

    canonical = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != decision.manifest_json:
        raise WindowsUpdateError(f"{name} manifest JSON is not canonical")

    qualification = manifest.get("qualification")
    expected_qualification = {
        "attestation_id": decision.qualification_attestation_id,
        "attestation_digest": decision.qualification_attestation_digest,
        "policy_id": decision.qualification_policy_id,
        "trust_root_id": decision.qualification_trust_root_id,
    }
    if (
        not isinstance(qualification, dict)
        or set(qualification) != {*expected_qualification, "receipt"}
        or {
            key: qualification.get(key)
            for key in expected_qualification
        }
        != expected_qualification
        or any(value is None for value in expected_qualification.values())
    ):
        raise WindowsUpdateError(
            f"{name} qualification provenance does not match frozen decision"
        )
    _text(
        decision.qualification_attestation_id,
        name=f"{name}.qualification.attestation_id",
    )
    _sha256(
        decision.qualification_attestation_digest,
        name=f"{name}.qualification.attestation_digest",
    )
    _text(
        decision.qualification_policy_id,
        name=f"{name}.qualification.policy_id",
    )
    _text(
        decision.qualification_trust_root_id,
        name=f"{name}.qualification.trust_root_id",
    )

    try:
        artifacts_raw = manifest["artifacts"]
        if not isinstance(artifacts_raw, list):
            raise WindowsUpdateError(f"{name}.artifacts must be a list")
        artifacts = tuple(
            ReleaseArtifactEvidence.create(
                role=raw["role"],
                artifact_id=raw["artifact_id"],
                artifact_sha256=raw["artifact_sha256"],
                source_sha=raw["source_sha"],
                signature_status=raw["signature_status"],
                evidence_status=raw["evidence_status"],
            )
            if isinstance(raw, dict)
            and set(raw)
            == {
                "role",
                "artifact_id",
                "artifact_sha256",
                "source_sha",
                "signature_status",
                "evidence_status",
            }
            else (_ for _ in ()).throw(
                WindowsUpdateError(f"{name} artifact structure is not canonical")
            )
            for raw in artifacts_raw
        )
        reconstructed = ReleaseCandidateInput.create(
            release_id=manifest["release_id"],
            source_sha=manifest["source_sha"],
            baseline_hash=manifest["baseline_hash"],
            schema_contract_hash=manifest["schema_contract_hash"],
            artifacts=artifacts,
            unresolved_blockers=(),
        )
        receipt = parse_signed_qualification_attestation(
            qualification["receipt"]
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        ReleaseCandidateError,
        QualificationTrustError,
    ) as error:
        if isinstance(error, WindowsUpdateError):
            raise
        raise WindowsUpdateError(
            f"{name} frozen release evidence is invalid"
        ) from error

    refrozen = freeze_release_candidate(
        reconstructed,
        evidence_store=trust.evidence_store,
        qualification_receipt=receipt,
        qualification_policy=trust.qualification_policy,
        expected_policy_id=trust.expected_policy_id,
        expected_policy_version=trust.expected_policy_version,
    )
    if (
        refrozen.status != "FROZEN"
        or refrozen.manifest_json != decision.manifest_json
        or refrozen.manifest_sha256 != decision.manifest_sha256
        or refrozen.qualification_attestation_id
        != decision.qualification_attestation_id
        or refrozen.qualification_attestation_digest
        != decision.qualification_attestation_digest
        or refrozen.qualification_policy_id
        != decision.qualification_policy_id
        or refrozen.qualification_trust_root_id
        != decision.qualification_trust_root_id
    ):
        raise WindowsUpdateError(
            f"{name} is not independently reverified by pinned release trust"
        )

    release_id = _text(
        reconstructed.release_id,
        name=f"{name}.release_id",
    )
    source_sha = _git_sha(
        reconstructed.source_sha,
        name=f"{name}.source_sha",
    )
    schema_contract_hash = _sha256(
        reconstructed.schema_contract_hash,
        name=f"{name}.schema_contract_hash",
    )
    windows_packages = [
        artifact.artifact_sha256
        for artifact in artifacts
        if artifact.role == "WINDOWS_PACKAGE"
    ]
    if len(windows_packages) != 1:
        raise WindowsUpdateError(
            f"{name} must contain exactly one WINDOWS_PACKAGE artifact"
        )

    return {
        "release_id": release_id,
        "source_sha": source_sha,
        "schema_contract_hash": schema_contract_hash,
        "windows_package_sha256": windows_packages[0],
        "manifest_sha256": decision.manifest_sha256,
        "manifest_json": decision.manifest_json,
    }

def build_windows_update_plan(
    *,
    current_release: ReleaseCandidateDecision,
    candidate_release: ReleaseCandidateDecision,
    current_journal_schema_version: int,
    candidate_journal_schema_version: int,
    backup_evidence: BackupEvidence,
    trust: WindowsUpdateTrustContext,
    migration_evidence: MigrationEvidence | None = None,
) -> WindowsUpdatePlan:
    """Return a deterministic fail-closed update/rollback plan.

    PLAN_READY means only that the supplied evidence is internally sufficient
    to attempt a separately controlled Windows update. It never means that an
    installer was executed, reconciliation completed, or trading was authorized.
    """

    current = _release_manifest(
        current_release,
        name="current_release",
        trust=trust,
    )
    candidate = _release_manifest(
        candidate_release,
        name="candidate_release",
        trust=trust,
    )
    current_schema = _positive_version(
        current_journal_schema_version,
        name="current_journal_schema_version",
    )
    candidate_schema = _positive_version(
        candidate_journal_schema_version,
        name="candidate_journal_schema_version",
    )
    if not isinstance(backup_evidence, BackupEvidence):
        raise TypeError("backup_evidence must be BackupEvidence")
    if migration_evidence is not None and not isinstance(
        migration_evidence,
        MigrationEvidence,
    ):
        raise TypeError("migration_evidence must be MigrationEvidence or None")

    reasons: list[str] = []
    if (
        current["release_id"] == candidate["release_id"]
        and current["source_sha"] == candidate["source_sha"]
        and current["windows_package_sha256"] == candidate["windows_package_sha256"]
    ):
        reasons.append("candidate_is_identical_to_current_release")

    if backup_evidence.verification_status != "PASS":
        reasons.append("pre_update_backup_not_verified")
    if not backup_evidence.reconciliation_required_after_restore:
        reasons.append("backup_restore_reconciliation_gate_missing")
    if backup_evidence.journal_schema_version != current_schema:
        reasons.append("backup_schema_does_not_match_current_runtime")
    if backup_evidence.source_sha != current["source_sha"]:
        reasons.append("backup_source_does_not_match_current_release")

    schema_changes = current_schema != candidate_schema
    if schema_changes:
        if migration_evidence is None:
            reasons.append("schema_change_requires_verified_migration_evidence")
        else:
            if migration_evidence.verification_status != "PASS":
                reasons.append("migration_evidence_not_verified")
            if migration_evidence.from_schema_version != current_schema:
                reasons.append("migration_from_schema_mismatch")
            if migration_evidence.to_schema_version != candidate_schema:
                reasons.append("migration_to_schema_mismatch")
            if migration_evidence.source_sha != candidate["source_sha"]:
                reasons.append("migration_source_sha_mismatch")
    elif migration_evidence is not None:
        reasons.append("migration_evidence_supplied_without_schema_change")

    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return WindowsUpdatePlan(
            status="BLOCKED",
            reasons=unique_reasons,
            plan_json=None,
            plan_sha256=None,
        )

    rollback_mode = (
        migration_evidence.rollback_mode
        if migration_evidence is not None
        else "REINSTALL_PREVIOUS_BUNDLE"
    )
    plan = {
        "schema_version": "1.0.0",
        "current_release": current,
        "candidate_release": candidate,
        "journal_schema_transition": {
            "from": current_schema,
            "to": candidate_schema,
        },
        "pre_update_backup": {
            "manifest_sha256": backup_evidence.manifest_sha256,
            "source_sha": backup_evidence.source_sha,
            "journal_schema_version": backup_evidence.journal_schema_version,
            "verification_status": backup_evidence.verification_status,
            "reconciliation_required_after_restore": (
                backup_evidence.reconciliation_required_after_restore
            ),
        },
        "migration_evidence": (
            None
            if migration_evidence is None
            else {
                "from_schema_version": migration_evidence.from_schema_version,
                "to_schema_version": migration_evidence.to_schema_version,
                "source_sha": migration_evidence.source_sha,
                "evidence_sha256": migration_evidence.evidence_sha256,
                "verification_status": migration_evidence.verification_status,
                "rollback_mode": migration_evidence.rollback_mode,
                "reverse_evidence_sha256": migration_evidence.reverse_evidence_sha256,
            }
        ),
        "install_steps": list(_INSTALL_STEPS),
        "rollback": {
            "mode": rollback_mode,
            "steps": list(_ROLLBACK_STEPS),
        },
        "trading_authority_granted_by_plan": False,
    }
    canonical = json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return WindowsUpdatePlan(
        status="PLAN_READY",
        reasons=(),
        plan_json=canonical,
        plan_sha256="sha256:" + sha256(canonical.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True)
class WindowsUpdateCheckpoint:
    """Pure restart-safe progress record for one immutable update plan.

    Persistence is intentionally delegated to the canonical release/recovery
    store. The checkpoint itself cannot execute a process, mutate financial
    state or grant authority.
    """

    plan_sha256: str
    update_completed_steps: tuple[str, ...] = ()
    rollback_started: bool = False
    rollback_completed_steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_sha256",
            _sha256(self.plan_sha256, name="plan_sha256"),
        )
        if not isinstance(self.rollback_started, bool):
            raise WindowsUpdateError("rollback_started must be bool")
        for field_name in ("update_completed_steps", "rollback_completed_steps"):
            value = getattr(self, field_name)
            if isinstance(value, (str, bytes)) or not isinstance(value, tuple):
                raise WindowsUpdateError(f"{field_name} must be a tuple")
            normalized = tuple(
                _text(step, name=f"{field_name}.step").upper()
                for step in value
            )
            if len(set(normalized)) != len(normalized):
                raise WindowsUpdateError(f"{field_name} contains duplicate steps")
            object.__setattr__(self, field_name, normalized)
        if not self.rollback_started and self.rollback_completed_steps:
            raise WindowsUpdateError(
                "rollback steps cannot exist before rollback starts"
            )


def _validated_plan_release(
    value: object,
    *,
    name: str,
    trust: WindowsUpdateTrustContext,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WindowsUpdateError(f"{name} must be an object")
    expected_fields = {
        "release_id",
        "source_sha",
        "schema_contract_hash",
        "windows_package_sha256",
        "manifest_sha256",
        "manifest_json",
    }
    if set(value) != expected_fields:
        raise WindowsUpdateError(f"{name} structure is not canonical")
    manifest_json = value.get("manifest_json")
    if not isinstance(manifest_json, str) or not manifest_json:
        raise WindowsUpdateError(f"{name}.manifest_json is required")
    try:
        parsed_manifest = json.loads(manifest_json)
        if type(parsed_manifest) is not dict or set(parsed_manifest) != {
            "release_id",
            "source_sha",
            "baseline_hash",
            "schema_contract_hash",
            "artifacts",
            "qualification",
        }:
            raise ValueError("release manifest structure is not canonical")
        qualification = parsed_manifest["qualification"]
        if type(qualification) is not dict or set(qualification) != {
            "attestation_id",
            "attestation_digest",
            "policy_id",
            "trust_root_id",
            "receipt",
        }:
            raise ValueError("release qualification structure is not canonical")
        artifacts_raw = parsed_manifest["artifacts"]
        if not isinstance(artifacts_raw, list):
            raise ValueError("release artifacts must be a list")
        artifact_fields = {
            "role",
            "artifact_id",
            "artifact_sha256",
            "source_sha",
            "signature_status",
            "evidence_status",
        }
        artifacts = []
        for raw in artifacts_raw:
            if type(raw) is not dict or set(raw) != artifact_fields:
                raise ValueError("release artifact structure is not canonical")
            artifacts.append(ReleaseArtifactEvidence.create(**raw))
        reconstructed = ReleaseCandidateInput.create(
            release_id=parsed_manifest["release_id"],
            source_sha=parsed_manifest["source_sha"],
            baseline_hash=parsed_manifest["baseline_hash"],
            schema_contract_hash=parsed_manifest["schema_contract_hash"],
            artifacts=tuple(artifacts),
            unresolved_blockers=(),
        )
        receipt = parse_signed_qualification_attestation(
            qualification["receipt"]
        )
        manifest_sha256 = _sha256(
            value.get("manifest_sha256"),
            name=f"{name}.manifest_sha256",
        )
        decision = freeze_release_candidate(
            reconstructed,
            evidence_store=trust.evidence_store,
            qualification_receipt=receipt,
            qualification_policy=trust.qualification_policy,
            expected_policy_id=trust.expected_policy_id,
            expected_policy_version=trust.expected_policy_version,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ReleaseCandidateError,
        QualificationTrustError,
    ) as error:
        raise WindowsUpdateError(
            f"{name} frozen release provenance is invalid"
        ) from error
    if (
        decision.status != "FROZEN"
        or decision.manifest_json != manifest_json
        or decision.manifest_sha256 != manifest_sha256
        or decision.qualification_attestation_id
        != qualification["attestation_id"]
        or decision.qualification_attestation_digest
        != qualification["attestation_digest"]
        or decision.qualification_policy_id
        != qualification["policy_id"]
        or decision.qualification_trust_root_id
        != qualification["trust_root_id"]
    ):
        raise WindowsUpdateError(
            f"{name} frozen release provenance is not independently verified"
        )
    canonical = _release_manifest(
        decision,
        name=name,
        trust=trust,
    )
    if value != canonical:
        raise WindowsUpdateError(
            f"{name} does not match its immutable frozen release manifest"
        )
    return canonical


def _plan_document(
    plan: WindowsUpdatePlan,
    *,
    trust: WindowsUpdateTrustContext,
) -> dict[str, Any]:
    """Validate executable plan bytes as strongly as the original planner."""

    if not isinstance(plan, WindowsUpdatePlan):
        raise TypeError("plan must be WindowsUpdatePlan")
    if plan.status != "PLAN_READY" or plan.plan_json is None or plan.plan_sha256 is None:
        raise WindowsUpdateError("plan must be PLAN_READY")
    expected_digest = "sha256:" + sha256(plan.plan_json.encode("utf-8")).hexdigest()
    if _sha256(plan.plan_sha256, name="plan_sha256") != expected_digest:
        raise WindowsUpdateError("update plan digest does not match bytes")
    try:
        document = json.loads(plan.plan_json)
    except json.JSONDecodeError as error:
        raise WindowsUpdateError("update plan is not valid JSON") from error
    if not isinstance(document, dict):
        raise WindowsUpdateError("update plan must be an object")
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != plan.plan_json:
        raise WindowsUpdateError("update plan JSON is not canonical")

    expected_fields = {
        "schema_version",
        "current_release",
        "candidate_release",
        "journal_schema_transition",
        "pre_update_backup",
        "migration_evidence",
        "install_steps",
        "rollback",
        "trading_authority_granted_by_plan",
    }
    if set(document) != expected_fields:
        raise WindowsUpdateError("update plan structure is not canonical")
    if document.get("schema_version") != "1.0.0":
        raise WindowsUpdateError("unsupported update plan schema_version")
    if document.get("trading_authority_granted_by_plan") is not False:
        raise WindowsUpdateError("update plan cannot grant trading authority")

    current = _validated_plan_release(
        document.get("current_release"),
        name="current_release",
        trust=trust,
    )
    candidate = _validated_plan_release(
        document.get("candidate_release"),
        name="candidate_release",
        trust=trust,
    )
    if (
        current["release_id"] == candidate["release_id"]
        and current["source_sha"] == candidate["source_sha"]
        and current["windows_package_sha256"]
        == candidate["windows_package_sha256"]
    ):
        raise WindowsUpdateError("candidate is identical to current release")

    transition = document.get("journal_schema_transition")
    if not isinstance(transition, dict) or set(transition) != {"from", "to"}:
        raise WindowsUpdateError("journal schema transition is not canonical")
    current_schema = _positive_version(
        transition.get("from"),
        name="journal_schema_transition.from",
    )
    candidate_schema = _positive_version(
        transition.get("to"),
        name="journal_schema_transition.to",
    )

    backup_raw = document.get("pre_update_backup")
    backup_fields = {
        "manifest_sha256",
        "source_sha",
        "journal_schema_version",
        "verification_status",
        "reconciliation_required_after_restore",
    }
    if not isinstance(backup_raw, dict) or set(backup_raw) != backup_fields:
        raise WindowsUpdateError("pre-update backup evidence is not canonical")
    try:
        backup = BackupEvidence(
            manifest_sha256=backup_raw["manifest_sha256"],
            source_sha=backup_raw["source_sha"],
            journal_schema_version=backup_raw["journal_schema_version"],
            verification_status=backup_raw["verification_status"],
            reconciliation_required_after_restore=backup_raw[
                "reconciliation_required_after_restore"
            ],
        )
    except (TypeError, ValueError, WindowsUpdateError) as error:
        raise WindowsUpdateError("pre-update backup evidence is invalid") from error
    if backup.verification_status != "PASS":
        raise WindowsUpdateError("pre-update backup is not verified")
    if not backup.reconciliation_required_after_restore:
        raise WindowsUpdateError("backup restore reconciliation gate is missing")
    if backup.journal_schema_version != current_schema:
        raise WindowsUpdateError("backup schema does not match current runtime")
    if backup.source_sha != current["source_sha"]:
        raise WindowsUpdateError("backup source does not match current release")

    migration_raw = document.get("migration_evidence")
    schema_changes = current_schema != candidate_schema
    migration: MigrationEvidence | None = None
    if schema_changes:
        migration_fields = {
            "from_schema_version",
            "to_schema_version",
            "source_sha",
            "evidence_sha256",
            "verification_status",
            "rollback_mode",
            "reverse_evidence_sha256",
        }
        if not isinstance(migration_raw, dict) or set(migration_raw) != migration_fields:
            raise WindowsUpdateError(
                "schema change requires canonical migration evidence"
            )
        try:
            migration = MigrationEvidence(
                from_schema_version=migration_raw["from_schema_version"],
                to_schema_version=migration_raw["to_schema_version"],
                source_sha=migration_raw["source_sha"],
                evidence_sha256=migration_raw["evidence_sha256"],
                verification_status=migration_raw["verification_status"],
                rollback_mode=migration_raw["rollback_mode"],
                reverse_evidence_sha256=migration_raw["reverse_evidence_sha256"],
            )
        except (TypeError, ValueError, WindowsUpdateError) as error:
            raise WindowsUpdateError("migration evidence is invalid") from error
        if migration.verification_status != "PASS":
            raise WindowsUpdateError("migration evidence is not verified")
        if migration.from_schema_version != current_schema:
            raise WindowsUpdateError("migration source schema mismatch")
        if migration.to_schema_version != candidate_schema:
            raise WindowsUpdateError("migration target schema mismatch")
        if migration.source_sha != candidate["source_sha"]:
            raise WindowsUpdateError("migration source release mismatch")
    elif migration_raw is not None:
        raise WindowsUpdateError(
            "migration evidence cannot exist without schema change"
        )

    rollback = document.get("rollback")
    if not isinstance(rollback, dict) or set(rollback) != {"mode", "steps"}:
        raise WindowsUpdateError("rollback plan structure is not canonical")
    expected_rollback_mode = (
        migration.rollback_mode
        if migration is not None
        else "REINSTALL_PREVIOUS_BUNDLE"
    )
    if rollback.get("mode") != expected_rollback_mode:
        raise WindowsUpdateError("rollback mode does not match migration evidence")

    return document


def _step_sequence(document: dict[str, Any], *, rollback: bool) -> tuple[str, ...]:
    raw = (
        document.get("rollback", {}).get("steps")
        if rollback and isinstance(document.get("rollback"), dict)
        else document.get("install_steps")
    )
    if not isinstance(raw, list) or not raw:
        raise WindowsUpdateError("update plan step sequence is missing")
    steps = tuple(_text(step, name="plan step").upper() for step in raw)
    if len(set(steps)) != len(steps):
        raise WindowsUpdateError("update plan step sequence contains duplicates")
    expected = _ROLLBACK_STEPS if rollback else _INSTALL_STEPS
    if steps != expected:
        raise WindowsUpdateError("update plan step sequence is not canonical")
    return steps


def start_update_checkpoint(
    plan: WindowsUpdatePlan,
    *,
    trust: WindowsUpdateTrustContext,
) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan, trust=trust)
    _step_sequence(document, rollback=False)
    _step_sequence(document, rollback=True)
    assert plan.plan_sha256 is not None
    return WindowsUpdateCheckpoint(plan_sha256=plan.plan_sha256)


def _validate_prefix(
    completed: tuple[str, ...],
    expected: tuple[str, ...],
    *,
    name: str,
) -> None:
    if completed != expected[: len(completed)]:
        raise WindowsUpdateError(f"{name} is not a valid ordered plan prefix")


def advance_update_checkpoint(
    plan: WindowsUpdatePlan,
    checkpoint: WindowsUpdateCheckpoint,
    step: str,
    *,
    trust: WindowsUpdateTrustContext,
) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan, trust=trust)
    if not isinstance(checkpoint, WindowsUpdateCheckpoint):
        raise TypeError("checkpoint must be WindowsUpdateCheckpoint")
    if checkpoint.plan_sha256 != plan.plan_sha256:
        raise WindowsUpdateError("checkpoint belongs to a different update plan")
    if checkpoint.rollback_started:
        raise WindowsUpdateError("update cannot continue after rollback starts")
    expected = _step_sequence(document, rollback=False)
    _validate_prefix(
        checkpoint.update_completed_steps,
        expected,
        name="update checkpoint",
    )
    normalized = _text(step, name="step").upper()
    if normalized in checkpoint.update_completed_steps:
        return checkpoint
    index = len(checkpoint.update_completed_steps)
    if index >= len(expected):
        raise WindowsUpdateError("update plan is already complete")
    if normalized != expected[index]:
        raise WindowsUpdateError(
            f"out-of-order update step: expected {expected[index]}"
        )
    return WindowsUpdateCheckpoint(
        plan_sha256=checkpoint.plan_sha256,
        update_completed_steps=checkpoint.update_completed_steps + (normalized,),
        rollback_started=False,
        rollback_completed_steps=(),
    )


def start_rollback_checkpoint(
    plan: WindowsUpdatePlan,
    checkpoint: WindowsUpdateCheckpoint,
    *,
    trust: WindowsUpdateTrustContext,
) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan, trust=trust)
    if not isinstance(checkpoint, WindowsUpdateCheckpoint):
        raise TypeError("checkpoint must be WindowsUpdateCheckpoint")
    if checkpoint.plan_sha256 != plan.plan_sha256:
        raise WindowsUpdateError("checkpoint belongs to a different update plan")
    expected_update = _step_sequence(document, rollback=False)
    _validate_prefix(
        checkpoint.update_completed_steps,
        expected_update,
        name="update checkpoint",
    )
    expected_rollback = _step_sequence(document, rollback=True)
    _validate_prefix(
        checkpoint.rollback_completed_steps,
        expected_rollback,
        name="rollback checkpoint",
    )
    if checkpoint.rollback_started:
        return checkpoint
    return WindowsUpdateCheckpoint(
        plan_sha256=checkpoint.plan_sha256,
        update_completed_steps=checkpoint.update_completed_steps,
        rollback_started=True,
        rollback_completed_steps=(),
    )


def advance_rollback_checkpoint(
    plan: WindowsUpdatePlan,
    checkpoint: WindowsUpdateCheckpoint,
    step: str,
    *,
    trust: WindowsUpdateTrustContext,
) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan, trust=trust)
    if not isinstance(checkpoint, WindowsUpdateCheckpoint):
        raise TypeError("checkpoint must be WindowsUpdateCheckpoint")
    if checkpoint.plan_sha256 != plan.plan_sha256:
        raise WindowsUpdateError("checkpoint belongs to a different update plan")
    if not checkpoint.rollback_started:
        raise WindowsUpdateError("rollback has not started")
    expected_update = _step_sequence(document, rollback=False)
    expected_rollback = _step_sequence(document, rollback=True)
    _validate_prefix(
        checkpoint.update_completed_steps,
        expected_update,
        name="update checkpoint",
    )
    _validate_prefix(
        checkpoint.rollback_completed_steps,
        expected_rollback,
        name="rollback checkpoint",
    )
    normalized = _text(step, name="step").upper()
    if normalized in checkpoint.rollback_completed_steps:
        return checkpoint
    index = len(checkpoint.rollback_completed_steps)
    if index >= len(expected_rollback):
        raise WindowsUpdateError("rollback plan is already complete")
    if normalized != expected_rollback[index]:
        raise WindowsUpdateError(
            f"out-of-order rollback step: expected {expected_rollback[index]}"
        )
    return WindowsUpdateCheckpoint(
        plan_sha256=checkpoint.plan_sha256,
        update_completed_steps=checkpoint.update_completed_steps,
        rollback_started=True,
        rollback_completed_steps=checkpoint.rollback_completed_steps + (normalized,),
    )



def serialize_update_checkpoint(checkpoint: WindowsUpdateCheckpoint) -> str:
    """Serialize progress canonically without persisting or executing it."""

    if not isinstance(checkpoint, WindowsUpdateCheckpoint):
        raise TypeError("checkpoint must be WindowsUpdateCheckpoint")
    body = {
        "schema_version": 1,
        "plan_sha256": checkpoint.plan_sha256,
        "update_completed_steps": list(checkpoint.update_completed_steps),
        "rollback_started": checkpoint.rollback_started,
        "rollback_completed_steps": list(checkpoint.rollback_completed_steps),
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def restore_update_checkpoint(
    plan: WindowsUpdatePlan,
    serialized: str,
    *,
    trust: WindowsUpdateTrustContext,
) -> WindowsUpdateCheckpoint:
    """Restore and validate checkpoint state against the exact immutable plan."""

    document = _plan_document(plan, trust=trust)
    if not isinstance(serialized, str) or not serialized:
        raise WindowsUpdateError("serialized checkpoint is required")
    try:
        body = json.loads(serialized)
    except json.JSONDecodeError as error:
        raise WindowsUpdateError("serialized checkpoint is invalid JSON") from error
    if not isinstance(body, dict) or set(body) != {
        "schema_version",
        "plan_sha256",
        "update_completed_steps",
        "rollback_started",
        "rollback_completed_steps",
    }:
        raise WindowsUpdateError("serialized checkpoint structure is invalid")
    if body["schema_version"] != 1:
        raise WindowsUpdateError("unsupported checkpoint schema version")
    update_steps = body["update_completed_steps"]
    rollback_steps = body["rollback_completed_steps"]
    if not isinstance(update_steps, list) or not isinstance(rollback_steps, list):
        raise WindowsUpdateError("checkpoint step collections must be lists")
    try:
        checkpoint = WindowsUpdateCheckpoint(
            plan_sha256=body["plan_sha256"],
            update_completed_steps=tuple(update_steps),
            rollback_started=body["rollback_started"],
            rollback_completed_steps=tuple(rollback_steps),
        )
    except (TypeError, ValueError, WindowsUpdateError) as error:
        raise WindowsUpdateError("serialized checkpoint fields are invalid") from error
    if checkpoint.plan_sha256 != plan.plan_sha256:
        raise WindowsUpdateError("checkpoint belongs to a different update plan")

    expected_update = _step_sequence(document, rollback=False)
    expected_rollback = _step_sequence(document, rollback=True)
    _validate_prefix(
        checkpoint.update_completed_steps,
        expected_update,
        name="update checkpoint",
    )
    _validate_prefix(
        checkpoint.rollback_completed_steps,
        expected_rollback,
        name="rollback checkpoint",
    )
    if not checkpoint.rollback_started and checkpoint.rollback_completed_steps:
        raise WindowsUpdateError(
            "rollback steps cannot exist before rollback starts"
        )
    return checkpoint



@dataclass(frozen=True)
class WindowsUpdateRestartAssessment:
    disposition: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        allowed = {
            "RESUME_PREINSTALL_VERIFICATION",
            "DEGRADED_RECONCILIATION_REQUIRED",
            "ROLLBACK_REQUIRED",
            "BLOCKED_UNKNOWN_STATE",
        }
        if self.disposition not in allowed:
            raise WindowsUpdateError("unsupported restart disposition")
        if not isinstance(self.reasons, tuple) or not self.reasons:
            raise WindowsUpdateError("restart assessment requires reasons")


def assess_windows_update_restart(
    plan: WindowsUpdatePlan,
    checkpoint: WindowsUpdateCheckpoint,
    *,
    trust: WindowsUpdateTrustContext,
    observed_windows_package_sha256: str,
    observed_journal_schema_version: int,
) -> WindowsUpdateRestartAssessment:
    """Classify crash/restart state from independently observed durable facts.

    The assessment never assumes that a side effect happened merely because a
    checkpoint step was recorded, or vice versa. Any candidate/mixed state
    starts fail closed and requires reconciliation or rollback; it never grants
    permission to blindly replay installation/migration steps.
    """

    document = _plan_document(plan, trust=trust)
    if not isinstance(checkpoint, WindowsUpdateCheckpoint):
        raise TypeError("checkpoint must be WindowsUpdateCheckpoint")
    if checkpoint.plan_sha256 != plan.plan_sha256:
        raise WindowsUpdateError("checkpoint belongs to a different update plan")
    expected_update = _step_sequence(document, rollback=False)
    expected_rollback = _step_sequence(document, rollback=True)
    _validate_prefix(
        checkpoint.update_completed_steps,
        expected_update,
        name="update checkpoint",
    )
    _validate_prefix(
        checkpoint.rollback_completed_steps,
        expected_rollback,
        name="rollback checkpoint",
    )

    observed_package = _sha256(
        observed_windows_package_sha256,
        name="observed_windows_package_sha256",
    )
    observed_schema = _positive_version(
        observed_journal_schema_version,
        name="observed_journal_schema_version",
    )
    current = document.get("current_release")
    candidate = document.get("candidate_release")
    transition = document.get("journal_schema_transition")
    if (
        not isinstance(current, dict)
        or not isinstance(candidate, dict)
        or not isinstance(transition, dict)
    ):
        raise WindowsUpdateError("update plan release metadata is invalid")
    current_package = _sha256(
        current.get("windows_package_sha256"),
        name="current_windows_package_sha256",
    )
    candidate_package = _sha256(
        candidate.get("windows_package_sha256"),
        name="candidate_windows_package_sha256",
    )
    current_schema = _positive_version(
        transition.get("from"),
        name="plan current schema",
    )
    candidate_schema = _positive_version(
        transition.get("to"),
        name="plan candidate schema",
    )

    if observed_package not in {current_package, candidate_package}:
        return WindowsUpdateRestartAssessment(
            disposition="BLOCKED_UNKNOWN_STATE",
            reasons=("observed_package_is_not_current_or_candidate",),
        )
    if observed_schema not in {current_schema, candidate_schema}:
        return WindowsUpdateRestartAssessment(
            disposition="BLOCKED_UNKNOWN_STATE",
            reasons=("observed_journal_schema_is_not_current_or_candidate",),
        )

    if checkpoint.rollback_started:
        if observed_package == current_package and observed_schema == current_schema:
            return WindowsUpdateRestartAssessment(
                disposition="DEGRADED_RECONCILIATION_REQUIRED",
                reasons=(
                    "rollback_target_observed",
                    "post_restore_reconciliation_still_required",
                ),
            )
        return WindowsUpdateRestartAssessment(
            disposition="ROLLBACK_REQUIRED",
            reasons=("rollback_in_progress_and_target_state_not_restored",),
        )

    side_effect_boundary = expected_update.index("INSTALL_CANDIDATE_SIDE_BY_SIDE")
    before_install = len(checkpoint.update_completed_steps) <= side_effect_boundary
    if (
        before_install
        and observed_package == current_package
        and observed_schema == current_schema
    ):
        return WindowsUpdateRestartAssessment(
            disposition="RESUME_PREINSTALL_VERIFICATION",
            reasons=("observed_state_matches_pre_update_runtime",),
        )

    if observed_package == candidate_package and observed_schema == candidate_schema:
        return WindowsUpdateRestartAssessment(
            disposition="DEGRADED_RECONCILIATION_REQUIRED",
            reasons=(
                "candidate_state_observed_after_crash",
                "blind_replay_of_install_or_migration_forbidden",
            ),
        )

    return WindowsUpdateRestartAssessment(
        disposition="ROLLBACK_REQUIRED",
        reasons=(
            "mixed_package_or_schema_state_observed",
            "blind_forward_resume_forbidden",
        ),
    )
