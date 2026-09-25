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

from .release_candidate import ReleaseCandidateDecision


class WindowsUpdateError(ValueError):
    """Raised when update/rollback evidence is malformed."""


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")

_INSTALL_STEPS = (
    "STOP_AND_FENCE_FINANCIAL_SENDER",
    "VERIFY_PRE_UPDATE_BACKUP",
    "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH",
    "INSTALL_CANDIDATE_SIDE_BY_SIDE",
    "APPLY_VERIFIED_SCHEMA_TRANSITION_IF_REQUIRED",
    "START_DEGRADED_NO_TRADING_AUTHORITY",
    "RUN_POST_UPDATE_RECONCILIATION",
    "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
)
_ROLLBACK_STEPS = (
    "STOP_AND_FENCE_FINANCIAL_SENDER",
    "RESTORE_OR_REINSTALL_PREVIOUS_QUALIFIED_STATE",
    "START_DEGRADED_NO_TRADING_AUTHORITY",
    "RUN_POST_RESTORE_RECONCILIATION",
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
            f"{name} must be an exact 40-character lowercase Git SHA"
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


def _release_manifest(decision: ReleaseCandidateDecision, *, name: str) -> dict[str, Any]:
    if not isinstance(decision, ReleaseCandidateDecision):
        raise TypeError(f"{name} must be ReleaseCandidateDecision")
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

    release_id = _text(manifest.get("release_id"), name=f"{name}.release_id")
    source_sha = _git_sha(manifest.get("source_sha"), name=f"{name}.source_sha")
    schema_contract_hash = _sha256(
        manifest.get("schema_contract_hash"),
        name=f"{name}.schema_contract_hash",
    )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise WindowsUpdateError(f"{name}.artifacts must be a list")

    windows_packages = []
    seen_roles: set[str] = set()
    for raw in artifacts:
        if not isinstance(raw, dict):
            raise WindowsUpdateError(f"{name} artifact must be an object")
        role = _text(raw.get("role"), name=f"{name}.artifact.role").upper()
        if role in seen_roles:
            raise WindowsUpdateError(f"{name} contains duplicate artifact role")
        seen_roles.add(role)
        artifact_sha = _sha256(
            raw.get("artifact_sha256"),
            name=f"{name}.{role}.artifact_sha256",
        )
        artifact_source = _git_sha(
            raw.get("source_sha"),
            name=f"{name}.{role}.source_sha",
        )
        if artifact_source != source_sha:
            raise WindowsUpdateError(
                f"{name} artifact source does not match release source"
            )
        if role == "WINDOWS_PACKAGE":
            if _text(
                raw.get("signature_status"),
                name=f"{name}.WINDOWS_PACKAGE.signature_status",
            ).upper() != "VERIFIED":
                raise WindowsUpdateError(
                    f"{name} Windows package signature is not verified"
                )
            if _text(
                raw.get("evidence_status"),
                name=f"{name}.WINDOWS_PACKAGE.evidence_status",
            ).upper() != "PASS":
                raise WindowsUpdateError(
                    f"{name} Windows package evidence is not PASS"
                )
            windows_packages.append(artifact_sha)
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
    }


def build_windows_update_plan(
    *,
    current_release: ReleaseCandidateDecision,
    candidate_release: ReleaseCandidateDecision,
    current_journal_schema_version: int,
    candidate_journal_schema_version: int,
    backup_evidence: BackupEvidence,
    migration_evidence: MigrationEvidence | None = None,
) -> WindowsUpdatePlan:
    """Return a deterministic fail-closed update/rollback plan.

    PLAN_READY means only that the supplied evidence is internally sufficient
    to attempt a separately controlled Windows update. It never means that an
    installer was executed, reconciliation completed, or trading was authorized.
    """

    current = _release_manifest(current_release, name="current_release")
    candidate = _release_manifest(candidate_release, name="candidate_release")
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


def _plan_document(plan: WindowsUpdatePlan) -> dict[str, Any]:
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


def start_update_checkpoint(plan: WindowsUpdatePlan) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan)
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
) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan)
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
) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan)
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
) -> WindowsUpdateCheckpoint:
    document = _plan_document(plan)
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
) -> WindowsUpdateCheckpoint:
    """Restore and validate checkpoint state against the exact immutable plan."""

    document = _plan_document(plan)
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
    observed_windows_package_sha256: str,
    observed_journal_schema_version: int,
) -> WindowsUpdateRestartAssessment:
    """Classify crash/restart state from independently observed durable facts.

    The assessment never assumes that a side effect happened merely because a
    checkpoint step was recorded, or vice versa. Any candidate/mixed state
    starts fail closed and requires reconciliation or rollback; it never grants
    permission to blindly replay installation/migration steps.
    """

    document = _plan_document(plan)
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
