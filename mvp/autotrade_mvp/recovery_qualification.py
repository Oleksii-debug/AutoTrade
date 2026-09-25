"""Fail-closed recovery/release qualification evidence for AutoTrade.

This module is deliberately read-only.  It does not restore state, acquire
authority, restart processes or send provider requests.  It only evaluates
evidence produced by the canonical recovery/runtime components.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)

from .qualification_attestation import (
    AcceptedQualificationAttestation,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    verify_qualification_attestation,
)


_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_ARTIFACT_MEDIA_TYPE = "application/vnd.autotrade.release-artifact"
_RECOVERY_EVIDENCE_MEDIA_TYPE = "application/vnd.autotrade.recovery-evidence"
_RECOVERY_EVIDENCE_KIND = "RECOVERY_SCENARIO_EVIDENCE"
_RECOVERY_QUALIFICATION_DECISION_TOKEN = object()
_QUALIFICATION_DOMAIN = "RECOVERY"
_QUALIFICATION_GATE = "RELEASE"
_QUALIFICATION_PACKAGE = "WP-59"
_QUALIFICATION_REQUIREMENT = "recovery-release-qualification"


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


def recovery_evidence_receipt_metadata(
    item: RecoveryScenarioEvidence,
) -> dict[str, object]:
    """Canonical immutable binding carried by one recovery evidence receipt."""

    if not isinstance(item, RecoveryScenarioEvidence):
        raise TypeError("item must be RecoveryScenarioEvidence")
    return {
        "evidence_kind": _RECOVERY_EVIDENCE_KIND,
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
    store: ArtifactStore,
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
        manifest = store.load_manifest(artifact_id)
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
        data = store.read_bytes(artifact_id)
        if expected_bytes is not None:
            if data != expected_bytes:
                return False
            if "sha256:" + sha256(data).hexdigest() != artifact_sha256:
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


def _qualification_covers_exact_recovery_evidence(
    receipt: SignedQualificationAttestation,
    evidence: Sequence[RecoveryScenarioEvidence],
) -> bool:
    expected = {
        (
            item.evidence_artifact_id,
            item.evidence_artifact_sha256,
            item.source_sha,
            _RECOVERY_EVIDENCE_MEDIA_TYPE,
            _RECOVERY_EVIDENCE_KIND,
        )
        for item in evidence
    }
    observed = {
        (
            ref.artifact_id,
            ref.sha256,
            ref.source_sha,
            ref.media_type,
            ref.evidence_kind,
        )
        for ref in receipt.attestation.evidence_refs
    }
    return observed == expected


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
    _verification_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
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
                _sha256(self.qualification_policy_id, name="qualification_policy_id"),
            )
            object.__setattr__(
                self,
                "qualification_trust_root_id",
                _sha256(
                    self.qualification_trust_root_id,
                    name="qualification_trust_root_id",
                ),
            )

        if self.status is RecoveryEvidenceStatus.PASS:
            if blockers:
                raise ValueError("PASS recovery decision cannot contain blockers")
            if set(measured) != _REQUIRED_SCENARIOS:
                raise ValueError(
                    "PASS recovery decision must measure every required scenario"
                )
            if not all(value is not None for value in qualification_values):
                raise ValueError(
                    "PASS recovery decision requires accepted qualification trust"
                )
            if self._verification_token is not _RECOVERY_QUALIFICATION_DECISION_TOKEN:
                raise ValueError(
                    "PASS recovery decision must come from canonical qualification"
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
        )


def qualify_recovery_release(
    *,
    policy: RecoveryQualificationPolicy,
    evidence: Sequence[RecoveryScenarioEvidence],
    evidence_store: ArtifactStore | None = None,
    qualification_receipt: SignedQualificationAttestation | None = None,
    qualification_policy: QualificationTrustPolicy | None = None,
    expected_policy_id: str | None = None,
    expected_policy_version: str | None = None,
) -> RecoveryQualificationDecision:
    """Evaluate recovery evidence without performing recovery itself."""

    if not isinstance(policy, RecoveryQualificationPolicy):
        raise TypeError("policy must be RecoveryQualificationPolicy")
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        raise TypeError("evidence must be a sequence")
    if evidence_store is not None and not isinstance(evidence_store, ArtifactStore):
        raise TypeError("evidence_store must be ArtifactStore")
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

    evidence_artifact_ids: set[str] = set()
    for item in evidence:
        if not isinstance(item, RecoveryScenarioEvidence):
            raise TypeError("evidence must contain RecoveryScenarioEvidence")
        if item.scenario in by_scenario:
            raise ValueError(f"duplicate evidence for {item.scenario.value}")
        if item.evidence_artifact_id in evidence_artifact_ids:
            raise ValueError("duplicate recovery evidence artifact identity")
        evidence_artifact_ids.add(item.evidence_artifact_id)
        by_scenario[item.scenario] = item

    missing = sorted(
        (scenario.value for scenario in _REQUIRED_SCENARIOS - set(by_scenario)),
    )
    for scenario in missing:
        blockers.append(f"missing_scenario:{scenario}")
        inconclusive = True

    release_artifact_verified = False
    if evidence_store is not None:
        release_artifact_verified = _store_artifact_matches(
            evidence_store,
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
    trust_inputs = (
        evidence_store,
        qualification_receipt,
        qualification_policy,
        expected_policy_id,
        expected_policy_version,
    )
    if all(value is None for value in trust_inputs[1:]):
        blockers.append("independent_evidence_trust_unavailable")
        inconclusive = True
    elif any(value is None for value in trust_inputs):
        blockers.append("independent_evidence_trust_incomplete")
        inconclusive = True
    else:
        if not _qualification_covers_exact_recovery_evidence(
            qualification_receipt,
            tuple(by_scenario.values()),
        ):
            blockers.append("independent_evidence_set_mismatch")
            hard_failure = True
        try:
            accepted = verify_qualification_attestation(
                qualification_receipt,
                policy=qualification_policy,
                evidence_store=evidence_store,
                expected_policy_id=expected_policy_id,
                expected_policy_version=expected_policy_version,
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
            if accepted.result == "FAIL":
                blockers.append("independent_evidence_attestation_failed")
                hard_failure = True
            elif accepted.result != "PASS":
                blockers.append("independent_evidence_attestation_inconclusive")
                inconclusive = True

    for scenario in sorted(by_scenario, key=lambda item: item.value):
        item = by_scenario[scenario]
        prefix = scenario.value.lower()

        integrity_verified = False
        if evidence_store is not None:
            integrity_verified = _store_artifact_matches(
                evidence_store,
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
        _verification_token=(
            _RECOVERY_QUALIFICATION_DECISION_TOKEN
            if status is RecoveryEvidenceStatus.PASS and accepted is not None
            else None
        ),
    )
