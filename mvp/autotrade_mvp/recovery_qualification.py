"""Fail-closed recovery/release qualification evidence for AutoTrade.

This module is deliberately read-only.  It does not restore state, acquire
authority, restart processes or send provider requests.  It only evaluates
evidence produced by the canonical recovery/runtime components.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

from research.autotrade_research.artifacts import trusted_authenticated_reader
from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)

from .qualification_attestation import (
    AcceptedQualificationAttestation,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    verify_canonical_qualification_attestation,
)


_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_ARTIFACT_MEDIA_TYPE = "application/vnd.autotrade.release-artifact"
_RECOVERY_EVIDENCE_MEDIA_TYPE = "application/vnd.autotrade.recovery-evidence"
_QUALIFICATION_DOMAIN = "RECOVERY"
_QUALIFICATION_GATE = "RELEASE"
_QUALIFICATION_PACKAGE = "WP-59"
_QUALIFICATION_REQUIREMENT = "recovery-release-qualification"
_RECOVERY_POLICY_REQUIREMENT_PREFIX = "recovery-decision-policy/sha256:"
_RECOVERY_POLICY_REQUIREMENT_RE = re.compile(
    r"^recovery-decision-policy/sha256:[0-9a-f]{64}$"
)


class RecoveryScenario(StrEnum):
    POWER_LOSS = "POWER_LOSS"
    NETWORK_LOSS = "NETWORK_LOSS"
    STORAGE_LOSS = "STORAGE_LOSS"
    SESSION_LOSS = "SESSION_LOSS"
    SPLIT_BRAIN_ATTEMPT = "SPLIT_BRAIN_ATTEMPT"
    UPGRADE_FAILURE = "UPGRADE_FAILURE"


class RecoveryEvidenceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


_REQUIRED_SCENARIOS = frozenset(RecoveryScenario)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _artifact_id(value: str, *, name: str) -> str:
    result = _text(value, name=name)
    try:
        return str(UUID(result))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError(f"{name} must be a UUID") from error


def _git_sha(value: str, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or value != value.lower()
        or _GIT_SHA.fullmatch(value) is None
    ):
        raise ValueError(
            f"{name} must be a canonical lowercase 40- or 64-character Git object id"
        )
    return value


def _sha256(value: str, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _SHA256.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return value


def _nonnegative_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _boolean(value: bool, *, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be boolean")
    return value


def _text_tuple(value: tuple[str, ...], *, name: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be a tuple")
    normalized = tuple(_text(item, name=name) for item in value)
    if not allow_empty and not normalized:
        raise ValueError(f"{name} must be non-empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must contain unique values")
    return normalized


@dataclass(frozen=True, slots=True)
class RecoveryScenarioEvidence:
    scenario: RecoveryScenario
    status: RecoveryEvidenceStatus
    source_sha: str
    release_artifact_id: str
    release_artifact_sha256: str
    evidence_artifact_id: str
    evidence_artifact_sha256: str
    evidence_refs: tuple[str, ...]
    evidence_schema_version: str
    protocol_id: str
    test_run_id: str
    tests_run: tuple[str, ...]
    unresolved_limits: tuple[str, ...]
    downtime_ms: int
    data_loss_events: int
    duplicate_external_actions: int
    unknown_submissions: int
    unresolved_reconciliation_items: int
    journal_integrity_verified: bool
    backup_integrity_verified: bool
    reconciliation_complete: bool
    authority_reacquired: bool
    old_sender_fenced: bool
    rollback_completed: bool
    open_risk_present: bool
    protection_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, RecoveryScenario):
            raise TypeError("scenario must be RecoveryScenario")
        if not isinstance(self.status, RecoveryEvidenceStatus):
            raise TypeError("status must be RecoveryEvidenceStatus")
        object.__setattr__(self, "source_sha", _git_sha(self.source_sha, name="source_sha"))
        object.__setattr__(
            self,
            "release_artifact_id",
            _artifact_id(self.release_artifact_id, name="release_artifact_id"),
        )
        object.__setattr__(
            self,
            "release_artifact_sha256",
            _sha256(self.release_artifact_sha256, name="release_artifact_sha256"),
        )
        object.__setattr__(
            self,
            "evidence_artifact_id",
            _artifact_id(self.evidence_artifact_id, name="evidence_artifact_id"),
        )
        object.__setattr__(
            self,
            "evidence_artifact_sha256",
            _sha256(self.evidence_artifact_sha256, name="evidence_artifact_sha256"),
        )
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("evidence_refs must be a non-empty tuple")
        normalized_refs = tuple(_text(value, name="evidence_ref") for value in self.evidence_refs)
        if len(normalized_refs) != len(set(normalized_refs)):
            raise ValueError("evidence_refs must be unique")
        object.__setattr__(self, "evidence_refs", normalized_refs)
        object.__setattr__(
            self,
            "evidence_schema_version",
            _text(self.evidence_schema_version, name="evidence_schema_version"),
        )
        object.__setattr__(self, "protocol_id", _text(self.protocol_id, name="protocol_id"))
        object.__setattr__(self, "test_run_id", _text(self.test_run_id, name="test_run_id"))
        object.__setattr__(
            self,
            "tests_run",
            _text_tuple(self.tests_run, name="tests_run"),
        )
        object.__setattr__(
            self,
            "unresolved_limits",
            _text_tuple(
                self.unresolved_limits,
                name="unresolved_limits",
                allow_empty=True,
            ),
        )
        for field in (
            "downtime_ms",
            "data_loss_events",
            "duplicate_external_actions",
            "unknown_submissions",
            "unresolved_reconciliation_items",
        ):
            object.__setattr__(
                self,
                field,
                _nonnegative_int(getattr(self, field), name=field),
            )
        for field in (
            "journal_integrity_verified",
            "backup_integrity_verified",
            "reconciliation_complete",
            "authority_reacquired",
            "old_sender_fenced",
            "rollback_completed",
            "open_risk_present",
        ):
            _boolean(getattr(self, field), name=field)
        protection = _text(self.protection_state, name="protection_state").upper()
        if protection not in {"NO_OPEN_RISK", "PROVIDER_NATIVE", "QUALIFIED_EMERGENCY"}:
            raise ValueError("unsupported protection_state")
        if self.open_risk_present and protection == "NO_OPEN_RISK":
            raise ValueError("open risk requires qualified protection evidence")
        if not self.open_risk_present and protection != "NO_OPEN_RISK":
            raise ValueError("protection_state contradicts open_risk_present")
        object.__setattr__(self, "protection_state", protection)


def _snapshot_recovery_scenario_evidence(
    item: RecoveryScenarioEvidence,
) -> RecoveryScenarioEvidence:
    """Detach one exact terminal recovery evidence value before trust callbacks."""

    if type(item) is not RecoveryScenarioEvidence:
        raise TypeError(
            "evidence must contain canonical RecoveryScenarioEvidence exact values"
        )
    values = {
        "scenario": item.scenario,
        "status": item.status,
        "source_sha": item.source_sha,
        "release_artifact_id": item.release_artifact_id,
        "release_artifact_sha256": item.release_artifact_sha256,
        "evidence_artifact_id": item.evidence_artifact_id,
        "evidence_artifact_sha256": item.evidence_artifact_sha256,
        "evidence_refs": item.evidence_refs,
        "evidence_schema_version": item.evidence_schema_version,
        "protocol_id": item.protocol_id,
        "test_run_id": item.test_run_id,
        "tests_run": item.tests_run,
        "unresolved_limits": item.unresolved_limits,
        "downtime_ms": item.downtime_ms,
        "data_loss_events": item.data_loss_events,
        "duplicate_external_actions": item.duplicate_external_actions,
        "unknown_submissions": item.unknown_submissions,
        "unresolved_reconciliation_items": item.unresolved_reconciliation_items,
        "journal_integrity_verified": item.journal_integrity_verified,
        "backup_integrity_verified": item.backup_integrity_verified,
        "reconciliation_complete": item.reconciliation_complete,
        "authority_reacquired": item.authority_reacquired,
        "old_sender_fenced": item.old_sender_fenced,
        "rollback_completed": item.rollback_completed,
        "open_risk_present": item.open_risk_present,
        "protection_state": item.protection_state,
    }
    if type(values["scenario"]) is not RecoveryScenario:
        raise TypeError("scenario must be exact RecoveryScenario")
    if type(values["status"]) is not RecoveryEvidenceStatus:
        raise TypeError("status must be exact RecoveryEvidenceStatus")
    for field in (
        "source_sha",
        "release_artifact_id",
        "release_artifact_sha256",
        "evidence_artifact_id",
        "evidence_artifact_sha256",
        "evidence_schema_version",
        "protocol_id",
        "test_run_id",
        "protection_state",
    ):
        if type(values[field]) is not str:
            raise TypeError(f"{field} must use exact str")
    for field in ("evidence_refs", "tests_run", "unresolved_limits"):
        current = values[field]
        if type(current) is not tuple or any(type(value) is not str for value in current):
            raise TypeError(f"{field} must use an exact tuple of exact str values")
    for field in (
        "downtime_ms",
        "data_loss_events",
        "duplicate_external_actions",
        "unknown_submissions",
        "unresolved_reconciliation_items",
    ):
        if type(values[field]) is not int:
            raise TypeError(f"{field} must use exact int")
    for field in (
        "journal_integrity_verified",
        "backup_integrity_verified",
        "reconciliation_complete",
        "authority_reacquired",
        "old_sender_fenced",
        "rollback_completed",
        "open_risk_present",
    ):
        if type(values[field]) is not bool:
            raise TypeError(f"{field} must use exact bool")
    return RecoveryScenarioEvidence(**values)


@dataclass(frozen=True, slots=True)
class RecoveryQualificationPolicy:
    source_sha: str
    release_artifact_id: str
    release_artifact_sha256: str
    evidence_schema_version: str
    protocol_id: str
    max_downtime_ms: Mapping[RecoveryScenario, int]
    required_tests: Mapping[RecoveryScenario, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_sha", _git_sha(self.source_sha, name="source_sha"))
        object.__setattr__(
            self,
            "release_artifact_id",
            _artifact_id(self.release_artifact_id, name="release_artifact_id"),
        )
        object.__setattr__(
            self,
            "release_artifact_sha256",
            _sha256(self.release_artifact_sha256, name="release_artifact_sha256"),
        )
        object.__setattr__(
            self,
            "evidence_schema_version",
            _text(self.evidence_schema_version, name="evidence_schema_version"),
        )
        object.__setattr__(self, "protocol_id", _text(self.protocol_id, name="protocol_id"))
        if not isinstance(self.max_downtime_ms, Mapping):
            raise TypeError("max_downtime_ms must be a mapping")
        normalized: dict[RecoveryScenario, int] = {}
        for scenario, limit in self.max_downtime_ms.items():
            if not isinstance(scenario, RecoveryScenario):
                raise TypeError("max_downtime_ms keys must be RecoveryScenario")
            if scenario in normalized:
                raise ValueError("duplicate recovery scenario limit")
            normalized[scenario] = _nonnegative_int(limit, name=f"max_downtime_ms[{scenario}]")
        if set(normalized) != _REQUIRED_SCENARIOS:
            raise ValueError("max_downtime_ms must cover every required recovery scenario")
        object.__setattr__(self, "max_downtime_ms", MappingProxyType(normalized))

        if not isinstance(self.required_tests, Mapping):
            raise TypeError("required_tests must be a mapping")
        normalized_tests: dict[RecoveryScenario, tuple[str, ...]] = {}
        for scenario, tests in self.required_tests.items():
            if not isinstance(scenario, RecoveryScenario):
                raise TypeError("required_tests keys must be RecoveryScenario")
            if scenario in normalized_tests:
                raise ValueError("duplicate recovery scenario test requirement")
            normalized_tests[scenario] = _text_tuple(
                tests,
                name=f"required_tests[{scenario}]",
            )
        if set(normalized_tests) != _REQUIRED_SCENARIOS:
            raise ValueError("required_tests must cover every required recovery scenario")
        object.__setattr__(
            self,
            "required_tests",
            MappingProxyType(normalized_tests),
        )


def _snapshot_recovery_qualification_policy(
    policy: RecoveryQualificationPolicy,
) -> RecoveryQualificationPolicy:
    """Detach one exact terminal recovery policy before trust callbacks."""

    if type(policy) is not RecoveryQualificationPolicy:
        raise TypeError("policy must be the canonical RecoveryQualificationPolicy")
    values = {
        "source_sha": policy.source_sha,
        "release_artifact_id": policy.release_artifact_id,
        "release_artifact_sha256": policy.release_artifact_sha256,
        "evidence_schema_version": policy.evidence_schema_version,
        "protocol_id": policy.protocol_id,
        "max_downtime_ms": policy.max_downtime_ms,
        "required_tests": policy.required_tests,
    }
    for field in (
        "source_sha",
        "release_artifact_id",
        "release_artifact_sha256",
        "evidence_schema_version",
        "protocol_id",
    ):
        if type(values[field]) is not str:
            raise TypeError(f"{field} must use exact str")

    mapping_proxy_type = type(MappingProxyType({}))
    limits_view = values["max_downtime_ms"]
    tests_view = values["required_tests"]
    if type(limits_view) is not mapping_proxy_type:
        raise TypeError("max_downtime_ms must be the canonical mapping proxy")
    if type(tests_view) is not mapping_proxy_type:
        raise TypeError("required_tests must be the canonical mapping proxy")
    if (
        set(limits_view) != _REQUIRED_SCENARIOS
        or any(type(scenario) is not RecoveryScenario for scenario in limits_view)
    ):
        raise ValueError("max_downtime_ms must contain exact required scenarios")
    if (
        set(tests_view) != _REQUIRED_SCENARIOS
        or any(type(scenario) is not RecoveryScenario for scenario in tests_view)
    ):
        raise ValueError("required_tests must contain exact required scenarios")

    limits: dict[RecoveryScenario, int] = {}
    required_tests: dict[RecoveryScenario, tuple[str, ...]] = {}
    for scenario in RecoveryScenario:
        limit = limits_view[scenario]
        if type(limit) is not int:
            raise TypeError(
                f"max_downtime_ms[{scenario.value}] must use exact int"
            )
        limits[scenario] = limit

        required = tests_view[scenario]
        if type(required) is not tuple or any(
            type(test_id) is not str for test_id in required
        ):
            raise TypeError(
                f"required_tests[{scenario.value}] must use an exact tuple of exact str values"
            )
        required_tests[scenario] = tuple(required)

    return RecoveryQualificationPolicy(
        source_sha=values["source_sha"],
        release_artifact_id=values["release_artifact_id"],
        release_artifact_sha256=values["release_artifact_sha256"],
        evidence_schema_version=values["evidence_schema_version"],
        protocol_id=values["protocol_id"],
        max_downtime_ms=limits,
        required_tests=required_tests,
    )


def recovery_policy_subject_requirement(
    policy: RecoveryQualificationPolicy,
) -> str:
    """Bind terminal recovery criteria before outcome evaluation."""

    if type(policy) is not RecoveryQualificationPolicy:
        raise TypeError("policy must be the canonical RecoveryQualificationPolicy")
    scenarios = sorted(RecoveryScenario, key=lambda item: item.value)
    payload = {
        "source_sha": policy.source_sha,
        "release_artifact_id": policy.release_artifact_id,
        "release_artifact_sha256": policy.release_artifact_sha256,
        "evidence_schema_version": policy.evidence_schema_version,
        "protocol_id": policy.protocol_id,
        "max_downtime_ms": {
            scenario.value: policy.max_downtime_ms[scenario]
            for scenario in scenarios
        },
        "required_tests": {
            scenario.value: sorted(policy.required_tests[scenario])
            for scenario in scenarios
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return _RECOVERY_POLICY_REQUIREMENT_PREFIX + sha256(encoded).hexdigest()


def recovery_evidence_receipt_metadata(
    item: RecoveryScenarioEvidence,
) -> dict[str, object]:
    """Canonical immutable binding carried by one recovery evidence receipt."""

    if not isinstance(item, RecoveryScenarioEvidence):
        raise TypeError("item must be RecoveryScenarioEvidence")
    return {
        "evidence_kind": "RECOVERY_SCENARIO_EVIDENCE",
        "scenario": item.scenario.value,
        "status": item.status.value,
        "source_sha": item.source_sha,
        "release_artifact_id": item.release_artifact_id,
        "release_artifact_sha256": item.release_artifact_sha256,
        "evidence_artifact_id": item.evidence_artifact_id,
        "evidence_artifact_sha256": item.evidence_artifact_sha256,
        "evidence_refs": list(item.evidence_refs),
        "evidence_schema_version": item.evidence_schema_version,
        "protocol_id": item.protocol_id,
        "test_run_id": item.test_run_id,
        "tests_run": list(item.tests_run),
        "unresolved_limits": list(item.unresolved_limits),
        "downtime_ms": item.downtime_ms,
        "data_loss_events": item.data_loss_events,
        "duplicate_external_actions": item.duplicate_external_actions,
        "unknown_submissions": item.unknown_submissions,
        "unresolved_reconciliation_items": item.unresolved_reconciliation_items,
        "journal_integrity_verified": item.journal_integrity_verified,
        "backup_integrity_verified": item.backup_integrity_verified,
        "reconciliation_complete": item.reconciliation_complete,
        "authority_reacquired": item.authority_reacquired,
        "old_sender_fenced": item.old_sender_fenced,
        "rollback_completed": item.rollback_completed,
        "open_risk_present": item.open_risk_present,
        "protection_state": item.protection_state,
    }


def recovery_evidence_receipt_payload(
    item: RecoveryScenarioEvidence,
) -> dict[str, object]:
    """Canonical bytes whose digest binds every recovery qualification fact."""

    payload = dict(recovery_evidence_receipt_metadata(item))
    # The content digest cannot include itself; every other decision-relevant fact
    # remains inside the immutable receipt bytes.
    payload.pop("evidence_artifact_sha256")
    return payload


def recovery_evidence_receipt_bytes(
    item: RecoveryScenarioEvidence,
) -> bytes:
    return json.dumps(
        recovery_evidence_receipt_payload(item),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _store_artifact_matches(
    read_snapshot,
    *,
    artifact_id: str,
    artifact_sha256: str,
    media_type: str,
    source_sha: str,
    metadata: dict[str, object],
    expected_bytes: bytes | None = None,
) -> bool:
    """Verify stored bytes and declared bindings, not independent producer trust."""
    try:
        manifest, raw = read_snapshot(artifact_id)
        if not isinstance(manifest.get("manifest_hash"), str):
            return False
        if manifest.get("sha256") != artifact_sha256:
            return False
        if manifest.get("media_type") != media_type:
            return False
        if manifest.get("source_refs") != [f"git:{source_sha}"]:
            return False
        if manifest.get("metadata") != metadata:
            return False
        if "sha256:" + sha256(raw).hexdigest() != artifact_sha256:
            return False
        if expected_bytes is not None and raw != expected_bytes:
            return False
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ):
        return False
    return True


def _recovery_evidence_set_sha256(
    evidence: Sequence[RecoveryScenarioEvidence],
) -> str:
    payload = [
        recovery_evidence_receipt_metadata(item)
        for item in sorted(evidence, key=lambda current: current.scenario.value)
    ]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryQualificationDecision:
    status: RecoveryEvidenceStatus
    source_sha: str
    release_artifact_id: str
    release_artifact_sha256: str
    evidence_schema_version: str
    protocol_id: str
    evidence_set_sha256: str
    blockers: tuple[str, ...]
    measured_downtime_ms: Mapping[RecoveryScenario, int]
    qualification_attestation_id: str | None = None
    qualification_attestation_digest: str | None = None
    qualification_policy_id: str | None = None
    qualification_trust_root_id: str | None = None
    recovery_policy_requirement: str | None = None
    _verification_policy: InitVar[RecoveryQualificationPolicy | None] = None
    _verification_evidence: InitVar[Sequence[RecoveryScenarioEvidence] | None] = None
    _verification_store: InitVar[ArtifactStore | None] = None
    _verification_root: InitVar[str | Path | None] = None
    _verification_receipt: InitVar[SignedQualificationAttestation | None] = None

    def __post_init__(
        self,
        _verification_policy: RecoveryQualificationPolicy | None,
        _verification_evidence: Sequence[RecoveryScenarioEvidence] | None,
        _verification_store: ArtifactStore | None,
        _verification_root: str | Path | None,
        _verification_receipt: SignedQualificationAttestation | None,
    ) -> None:
        object.__setattr__(
            self,
            "source_sha",
            _git_sha(self.source_sha, name="source_sha"),
        )
        object.__setattr__(
            self,
            "release_artifact_id",
            _artifact_id(self.release_artifact_id, name="release_artifact_id"),
        )
        object.__setattr__(
            self,
            "release_artifact_sha256",
            _sha256(self.release_artifact_sha256, name="release_artifact_sha256"),
        )
        object.__setattr__(
            self,
            "evidence_schema_version",
            _text(self.evidence_schema_version, name="evidence_schema_version"),
        )
        object.__setattr__(
            self,
            "protocol_id",
            _text(self.protocol_id, name="protocol_id"),
        )
        object.__setattr__(
            self,
            "evidence_set_sha256",
            _sha256(self.evidence_set_sha256, name="evidence_set_sha256"),
        )
        if not isinstance(self.status, RecoveryEvidenceStatus):
            raise TypeError("status must be RecoveryEvidenceStatus")
        if not isinstance(self.blockers, tuple):
            raise TypeError("blockers must be a tuple")
        blockers = tuple(_text(value, name="blocker") for value in self.blockers)
        if len(blockers) != len(set(blockers)):
            raise ValueError("blockers must be unique")
        if not isinstance(self.measured_downtime_ms, Mapping):
            raise TypeError("measured_downtime_ms must be a mapping")
        measured: dict[RecoveryScenario, int] = {}
        for scenario, value in self.measured_downtime_ms.items():
            if not isinstance(scenario, RecoveryScenario):
                raise TypeError("measured_downtime_ms keys must be RecoveryScenario")
            if scenario in measured:
                raise ValueError("duplicate measured recovery scenario")
            measured[scenario] = _nonnegative_int(
                value,
                name=f"measured_downtime_ms[{scenario.value}]",
            )

        qualification_values = (
            self.qualification_attestation_id,
            self.qualification_attestation_digest,
            self.qualification_policy_id,
            self.qualification_trust_root_id,
        )
        if any(value is not None for value in qualification_values):
            if not all(value is not None for value in qualification_values):
                raise ValueError(
                    "qualification trust identity must be complete when present"
                )
            object.__setattr__(
                self,
                "qualification_attestation_id",
                _artifact_id(
                    self.qualification_attestation_id,
                    name="qualification_attestation_id",
                ),
            )
            object.__setattr__(
                self,
                "qualification_attestation_digest",
                _sha256(
                    self.qualification_attestation_digest,
                    name="qualification_attestation_digest",
                ),
            )
            object.__setattr__(
                self,
                "qualification_policy_id",
                _sha256(
                    self.qualification_policy_id,
                    name="qualification_policy_id",
                ),
            )
            object.__setattr__(
                self,
                "qualification_trust_root_id",
                _sha256(
                    self.qualification_trust_root_id,
                    name="qualification_trust_root_id",
                ),
            )

        if self.recovery_policy_requirement is not None:
            if (
                type(self.recovery_policy_requirement) is not str
                or _RECOVERY_POLICY_REQUIREMENT_RE.fullmatch(
                    self.recovery_policy_requirement
                )
                is None
            ):
                raise ValueError(
                    "recovery_policy_requirement must bind the exact decision policy"
                )

        if self.status is RecoveryEvidenceStatus.PASS:
            if blockers:
                raise ValueError("PASS recovery decision cannot contain blockers")
            if set(measured) != _REQUIRED_SCENARIOS:
                raise ValueError(
                    "PASS recovery decision must measure every required scenario"
                )
            if self.recovery_policy_requirement is None:
                raise ValueError(
                    "PASS recovery decision requires exact recovery policy identity"
                )
            if not all(value is not None for value in qualification_values):
                raise ValueError(
                    "PASS recovery decision requires accepted qualification trust"
                )
            if type(_verification_policy) is not RecoveryQualificationPolicy:
                raise ValueError(
                    "PASS recovery decision requires independently verifiable qualification evidence"
                )
            try:
                _verification_policy = _snapshot_recovery_qualification_policy(
                    _verification_policy
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "PASS recovery decision requires canonical recovery policy evidence"
                ) from error
            if (
                type(_verification_evidence) is not tuple
                or any(
                    type(item) is not RecoveryScenarioEvidence
                    for item in _verification_evidence
                )
            ):
                raise ValueError(
                    "PASS recovery decision requires exact independently verifiable qualification evidence"
                )
            if type(_verification_store) is not ArtifactStore:
                raise ValueError(
                    "PASS recovery decision requires canonical evidence store authority"
                )
            if not isinstance(_verification_root, (str, Path)):
                raise ValueError(
                    "PASS recovery decision requires independent evidence root authority"
                )
            if type(_verification_receipt) is not SignedQualificationAttestation:
                raise ValueError(
                    "PASS recovery decision requires exact signed qualification evidence"
                )

            verification_evidence = tuple(_verification_evidence)
            by_scenario = {
                item.scenario: item
                for item in verification_evidence
            }
            if (
                len(verification_evidence) != len(_REQUIRED_SCENARIOS)
                or len(by_scenario) != len(verification_evidence)
                or set(by_scenario) != _REQUIRED_SCENARIOS
            ):
                raise ValueError(
                    "PASS recovery decision evidence must cover every recovery scenario exactly once"
                )
            if len(
                {item.evidence_artifact_id for item in verification_evidence}
            ) != len(verification_evidence):
                raise ValueError(
                    "PASS recovery decision evidence artifact identities must be unique"
                )
            if not self.matches_policy(_verification_policy):
                raise ValueError(
                    "PASS recovery decision does not match recovery qualification policy"
                )
            if (
                _recovery_evidence_set_sha256(verification_evidence)
                != self.evidence_set_sha256
            ):
                raise ValueError(
                    "PASS recovery decision evidence set digest is not canonical"
                )
            expected_measured = {
                scenario: by_scenario[scenario].downtime_ms
                for scenario in _REQUIRED_SCENARIOS
            }
            if measured != expected_measured:
                raise ValueError(
                    "PASS recovery decision measured downtime does not match evidence"
                )
            try:
                trusted_read = trusted_authenticated_reader(
                    _verification_root,
                    publication_store=_verification_store,
                )
            except (
                ArtifactIntegrityError,
                FileNotFoundError,
                OSError,
                TypeError,
                ValueError,
            ) as error:
                raise ValueError(
                    "PASS recovery decision evidence root is not independently verifiable"
                ) from error
            if not _store_artifact_matches(
                trusted_read,
                artifact_id=self.release_artifact_id,
                artifact_sha256=self.release_artifact_sha256,
                media_type=_RELEASE_ARTIFACT_MEDIA_TYPE,
                source_sha=self.source_sha,
                metadata={
                    "evidence_kind": "RECOVERY_RELEASE_ARTIFACT",
                    "source_sha": self.source_sha,
                },
            ):
                raise ValueError(
                    "PASS recovery decision release artifact is not integrity verified"
                )

            for item in verification_evidence:
                if (
                    item.source_sha != self.source_sha
                    or item.release_artifact_id != self.release_artifact_id
                    or item.release_artifact_sha256 != self.release_artifact_sha256
                    or item.evidence_schema_version != self.evidence_schema_version
                    or item.protocol_id != self.protocol_id
                    or item.status is not RecoveryEvidenceStatus.PASS
                    or item.unresolved_limits
                    or item.downtime_ms
                    > _verification_policy.max_downtime_ms[item.scenario]
                    or not set(
                        _verification_policy.required_tests[item.scenario]
                    ).issubset(item.tests_run)
                    or item.data_loss_events
                    or item.duplicate_external_actions
                    or item.unknown_submissions
                    or item.unresolved_reconciliation_items
                    or not item.journal_integrity_verified
                    or not item.backup_integrity_verified
                    or not item.reconciliation_complete
                    or not item.authority_reacquired
                    or not item.old_sender_fenced
                    or (
                        item.scenario is RecoveryScenario.UPGRADE_FAILURE
                        and not item.rollback_completed
                    )
                    or (
                        item.open_risk_present
                        and item.protection_state
                        not in {"PROVIDER_NATIVE", "QUALIFIED_EMERGENCY"}
                    )
                ):
                    raise ValueError(
                        "PASS recovery decision evidence does not satisfy recovery policy"
                    )
                if not _store_artifact_matches(
                    trusted_read,
                    artifact_id=item.evidence_artifact_id,
                    artifact_sha256=item.evidence_artifact_sha256,
                    media_type=_RECOVERY_EVIDENCE_MEDIA_TYPE,
                    source_sha=item.source_sha,
                    metadata=recovery_evidence_receipt_metadata(item),
                    expected_bytes=recovery_evidence_receipt_bytes(item),
                ):
                    raise ValueError(
                        "PASS recovery decision evidence artifact is not integrity verified"
                    )

            try:
                accepted = verify_canonical_qualification_attestation(
                    _verification_receipt,
                    evidence_store=_verification_store,
                    evidence_root=_verification_root,
                    expected_source_sha=self.source_sha,
                    expected_domain=_QUALIFICATION_DOMAIN,
                    expected_gate=_QUALIFICATION_GATE,
                    expected_package_id=_QUALIFICATION_PACKAGE,
                    expected_protocol_id=self.protocol_id,
                    expected_protocol_version=self.evidence_schema_version,
                    expected_requirement_id=_QUALIFICATION_REQUIREMENT,
                    expected_release_artifact_id=self.release_artifact_id,
                    expected_release_artifact_sha256=self.release_artifact_sha256,
                )
            except (QualificationTrustError, TypeError, ValueError) as error:
                raise ValueError(
                    "PASS recovery decision qualification receipt is not canonically verified"
                ) from error
            if accepted.result != "PASS":
                raise ValueError(
                    "PASS recovery decision requires a canonical PASS attestation"
                )
            expected_refs = {
                (
                    item.evidence_artifact_id,
                    item.evidence_artifact_sha256,
                    item.source_sha,
                    _RECOVERY_EVIDENCE_MEDIA_TYPE,
                    "RECOVERY_SCENARIO_EVIDENCE",
                )
                for item in verification_evidence
            }
            accepted_refs = {
                (
                    ref.artifact_id,
                    ref.sha256,
                    ref.source_sha,
                    ref.media_type,
                    ref.evidence_kind,
                )
                for ref in accepted.evidence_refs
            }
            if accepted_refs != expected_refs:
                raise ValueError(
                    "PASS recovery decision qualification receipt does not cover exact recovery evidence"
                )
            if self.recovery_policy_requirement not in accepted.requirement_ids:
                raise ValueError(
                    "PASS recovery decision qualification receipt does not bind exact recovery policy"
                )
            accepted_identity = (
                accepted.attestation_id,
                accepted.attestation_digest,
                accepted.policy_id,
                accepted.trust_root_id,
            )
            if accepted_identity != (
                self.qualification_attestation_id,
                self.qualification_attestation_digest,
                self.qualification_policy_id,
                self.qualification_trust_root_id,
            ):
                raise ValueError(
                    "PASS recovery decision trust identity does not match canonical verification"
                )
        elif not blockers:
            raise ValueError("non-PASS recovery decision requires blockers")

        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(
            self,
            "measured_downtime_ms",
            MappingProxyType(measured),
        )

    @property
    def authorizes_trading(self) -> bool:
        return False

    def matches_policy(self, policy: RecoveryQualificationPolicy) -> bool:
        if not isinstance(policy, RecoveryQualificationPolicy):
            raise TypeError("policy must be RecoveryQualificationPolicy")
        return (
            self.source_sha == policy.source_sha
            and self.release_artifact_id == policy.release_artifact_id
            and self.release_artifact_sha256 == policy.release_artifact_sha256
            and self.evidence_schema_version == policy.evidence_schema_version
            and self.protocol_id == policy.protocol_id
            and self.recovery_policy_requirement
            == recovery_policy_subject_requirement(policy)
        )



def qualify_recovery_release(
    *,
    policy: RecoveryQualificationPolicy,
    evidence: Sequence[RecoveryScenarioEvidence],
    evidence_store: ArtifactStore | None = None,
    evidence_root: str | Path | None = None,
    qualification_receipt: SignedQualificationAttestation | None = None,
    qualification_policy: QualificationTrustPolicy | None = None,
    expected_policy_id: str | None = None,
    expected_policy_version: str | None = None,
) -> RecoveryQualificationDecision:
    """Evaluate recovery evidence without performing recovery itself."""

    policy = _snapshot_recovery_qualification_policy(policy)
    policy_requirement = recovery_policy_subject_requirement(policy)
    if type(evidence) not in (list, tuple):
        raise TypeError("evidence must be an exact list or tuple")
    evidence_snapshot = tuple(
        _snapshot_recovery_scenario_evidence(item)
        for item in evidence
    )
    if evidence_store is not None and type(evidence_store) is not ArtifactStore:
        raise TypeError(
            "evidence_store must be ArtifactStore (canonical exact type required)"
        )
    if qualification_receipt is not None and not isinstance(
        qualification_receipt, SignedQualificationAttestation
    ):
        raise TypeError("qualification_receipt must be SignedQualificationAttestation")
    if qualification_policy is not None and not isinstance(
        qualification_policy, QualificationTrustPolicy
    ):
        raise TypeError("qualification_policy must be QualificationTrustPolicy")

    by_scenario: dict[RecoveryScenario, RecoveryScenarioEvidence] = {}
    blockers: list[str] = []
    hard_failure = False
    inconclusive = False
    trusted_read = None
    if evidence_store is not None and evidence_root is not None:
        try:
            trusted_read = trusted_authenticated_reader(
                evidence_root,
                publication_store=evidence_store,
            )
        except (
            ArtifactIntegrityError,
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
        ):
            trusted_read = None

    for item in evidence_snapshot:
        if item.scenario in by_scenario:
            raise ValueError(f"duplicate evidence for {item.scenario.value}")
        by_scenario[item.scenario] = item

    missing = sorted(
        (scenario.value for scenario in _REQUIRED_SCENARIOS - set(by_scenario)),
    )
    for scenario in missing:
        blockers.append(f"missing_scenario:{scenario}")
        inconclusive = True

    release_artifact_verified = False
    if trusted_read is not None:
        release_artifact_verified = _store_artifact_matches(
            trusted_read,
            artifact_id=policy.release_artifact_id,
            artifact_sha256=policy.release_artifact_sha256,
            media_type=_RELEASE_ARTIFACT_MEDIA_TYPE,
            source_sha=policy.source_sha,
            metadata={
                "evidence_kind": "RECOVERY_RELEASE_ARTIFACT",
                "source_sha": policy.source_sha,
            },
        )
    if not release_artifact_verified:
        blockers.append("release_artifact:integrity_unverified")
        inconclusive = True

    accepted: AcceptedQualificationAttestation | None = None
    # Caller-supplied qualification policy/pins are compatibility-only. Terminal
    # recovery trust is selected from the exact-source canonical trust policy.
    if qualification_receipt is None:
        blockers.append("independent_evidence_trust_unavailable")
        inconclusive = True
    elif evidence_store is None or evidence_root is None:
        blockers.append("independent_evidence_trust_incomplete")
        inconclusive = True
    else:
        try:
            accepted = verify_canonical_qualification_attestation(
                qualification_receipt,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
                expected_source_sha=policy.source_sha,
                expected_domain=_QUALIFICATION_DOMAIN,
                expected_gate=_QUALIFICATION_GATE,
                expected_package_id=_QUALIFICATION_PACKAGE,
                expected_protocol_id=policy.protocol_id,
                expected_protocol_version=policy.evidence_schema_version,
                expected_requirement_id=_QUALIFICATION_REQUIREMENT,
                expected_release_artifact_id=policy.release_artifact_id,
                expected_release_artifact_sha256=policy.release_artifact_sha256,
            )
        except (QualificationTrustError, TypeError, ValueError):
            blockers.append("independent_evidence_trust_invalid")
            inconclusive = True
        else:
            signed_refs = {
                (item.artifact_id, item.sha256)
                for item in accepted.evidence_refs
            }
            expected_refs = {
                (item.evidence_artifact_id, item.evidence_artifact_sha256)
                for item in by_scenario.values()
            }
            if accepted.result == "FAIL":
                blockers.append("independent_evidence_attestation_failed")
                hard_failure = True
            elif accepted.result != "PASS":
                blockers.append("independent_evidence_attestation_inconclusive")
                inconclusive = True
            elif signed_refs != expected_refs:
                blockers.append("independent_evidence_set_mismatch")
                hard_failure = True
            if policy_requirement not in accepted.requirement_ids:
                blockers.append("independent_recovery_policy_mismatch")
                hard_failure = True

    for scenario in sorted(by_scenario, key=lambda item: item.value):
        item = by_scenario[scenario]
        prefix = scenario.value.lower()

        integrity_verified = False
        if trusted_read is not None:
            integrity_verified = _store_artifact_matches(
                trusted_read,
                artifact_id=item.evidence_artifact_id,
                artifact_sha256=item.evidence_artifact_sha256,
                media_type=_RECOVERY_EVIDENCE_MEDIA_TYPE,
                source_sha=item.source_sha,
                metadata=recovery_evidence_receipt_metadata(item),
                expected_bytes=recovery_evidence_receipt_bytes(item),
            )
        if not integrity_verified:
            blockers.append(f"{prefix}:evidence_integrity_unverified")
            inconclusive = True

        if item.source_sha != policy.source_sha:
            blockers.append(f"{prefix}:source_sha_mismatch")
            hard_failure = True
        if item.release_artifact_id != policy.release_artifact_id:
            blockers.append(f"{prefix}:release_artifact_id_mismatch")
            hard_failure = True
        if item.release_artifact_sha256 != policy.release_artifact_sha256:
            blockers.append(f"{prefix}:release_artifact_mismatch")
            hard_failure = True
        if item.evidence_schema_version != policy.evidence_schema_version:
            blockers.append(f"{prefix}:evidence_schema_version_mismatch")
            hard_failure = True
        if item.protocol_id != policy.protocol_id:
            blockers.append(f"{prefix}:protocol_id_mismatch")
            hard_failure = True
        missing_tests = sorted(set(policy.required_tests[scenario]) - set(item.tests_run))
        if missing_tests:
            blockers.extend(
                f"{prefix}:required_test_missing:{test_id}"
                for test_id in missing_tests
            )
            hard_failure = True
        if item.unresolved_limits:
            blockers.extend(
                f"{prefix}:unresolved_limit:{limit}"
                for limit in item.unresolved_limits
            )
            inconclusive = True
        if item.status is RecoveryEvidenceStatus.FAIL:
            blockers.append(f"{prefix}:scenario_failed")
            hard_failure = True
        elif item.status is RecoveryEvidenceStatus.INCONCLUSIVE:
            blockers.append(f"{prefix}:scenario_inconclusive")
            inconclusive = True

        if item.downtime_ms > policy.max_downtime_ms[scenario]:
            blockers.append(f"{prefix}:downtime_limit_exceeded")
            hard_failure = True
        if item.data_loss_events:
            blockers.append(f"{prefix}:data_loss_observed")
            hard_failure = True
        if item.duplicate_external_actions:
            blockers.append(f"{prefix}:duplicate_external_action")
            hard_failure = True
        if item.unknown_submissions:
            blockers.append(f"{prefix}:unknown_submission_unresolved")
            hard_failure = True
        if item.unresolved_reconciliation_items:
            blockers.append(f"{prefix}:reconciliation_unresolved")
            hard_failure = True
        if not item.journal_integrity_verified:
            blockers.append(f"{prefix}:journal_integrity_unverified")
            hard_failure = True
        if not item.backup_integrity_verified:
            blockers.append(f"{prefix}:backup_integrity_unverified")
            hard_failure = True
        if not item.reconciliation_complete:
            blockers.append(f"{prefix}:reconciliation_incomplete")
            hard_failure = True
        if not item.authority_reacquired:
            blockers.append(f"{prefix}:authority_not_reacquired")
            hard_failure = True
        if not item.old_sender_fenced:
            blockers.append(f"{prefix}:old_sender_not_fenced")
            hard_failure = True
        if scenario is RecoveryScenario.UPGRADE_FAILURE and not item.rollback_completed:
            blockers.append(f"{prefix}:rollback_incomplete")
            hard_failure = True
        if item.open_risk_present and item.protection_state not in {
            "PROVIDER_NATIVE",
            "QUALIFIED_EMERGENCY",
        }:
            blockers.append(f"{prefix}:open_risk_unprotected")
            hard_failure = True

    if hard_failure:
        status = RecoveryEvidenceStatus.FAIL
    elif inconclusive:
        status = RecoveryEvidenceStatus.INCONCLUSIVE
    else:
        status = RecoveryEvidenceStatus.PASS

    measured = MappingProxyType(
        {
            scenario: item.downtime_ms
            for scenario, item in sorted(
                by_scenario.items(),
                key=lambda pair: pair[0].value,
            )
        }
    )
    return RecoveryQualificationDecision(
        status=status,
        source_sha=policy.source_sha,
        release_artifact_id=policy.release_artifact_id,
        release_artifact_sha256=policy.release_artifact_sha256,
        evidence_schema_version=policy.evidence_schema_version,
        protocol_id=policy.protocol_id,
        evidence_set_sha256=_recovery_evidence_set_sha256(tuple(by_scenario.values())),
        blockers=tuple(blockers),
        measured_downtime_ms=measured,
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
        recovery_policy_requirement=policy_requirement,
        _verification_policy=(
            policy if status is RecoveryEvidenceStatus.PASS else None
        ),
        _verification_evidence=(
            evidence_snapshot
            if status is RecoveryEvidenceStatus.PASS
            else None
        ),
        _verification_store=(
            evidence_store if status is RecoveryEvidenceStatus.PASS else None
        ),
        _verification_root=(
            evidence_root if status is RecoveryEvidenceStatus.PASS else None
        ),
        _verification_receipt=(
            qualification_receipt
            if status is RecoveryEvidenceStatus.PASS
            else None
        ),
    )
