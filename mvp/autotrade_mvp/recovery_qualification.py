"""Fail-closed recovery/release qualification evidence for AutoTrade.

This module is deliberately read-only.  It does not restore state, acquire
authority, restart processes or send provider requests.  It only evaluates
evidence produced by the canonical recovery/runtime components.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Mapping, Sequence


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def _git_sha(value: str, *, name: str) -> str:
    result = _text(value, name=name).lower()
    if _GIT_SHA.fullmatch(result) is None:
        raise ValueError(f"{name} must be a lowercase 40-character git SHA")
    return result


def _sha256(value: str, *, name: str) -> str:
    result = _text(value, name=name).lower()
    if _SHA256.fullmatch(result) is None:
        raise ValueError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return result


def _nonnegative_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _boolean(value: bool, *, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class RecoveryScenarioEvidence:
    scenario: RecoveryScenario
    status: RecoveryEvidenceStatus
    source_sha: str
    release_artifact_sha256: str
    evidence_refs: tuple[str, ...]
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
            "release_artifact_sha256",
            _sha256(self.release_artifact_sha256, name="release_artifact_sha256"),
        )
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("evidence_refs must be a non-empty tuple")
        normalized_refs = tuple(_text(value, name="evidence_ref") for value in self.evidence_refs)
        if len(normalized_refs) != len(set(normalized_refs)):
            raise ValueError("evidence_refs must be unique")
        object.__setattr__(self, "evidence_refs", normalized_refs)
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
    release_artifact_sha256: str
    max_downtime_ms: Mapping[RecoveryScenario, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_sha", _git_sha(self.source_sha, name="source_sha"))
        object.__setattr__(
            self,
            "release_artifact_sha256",
            _sha256(self.release_artifact_sha256, name="release_artifact_sha256"),
        )
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


@dataclass(frozen=True, slots=True)
class RecoveryQualificationDecision:
    status: RecoveryEvidenceStatus
    blockers: tuple[str, ...]
    measured_downtime_ms: Mapping[RecoveryScenario, int]

    @property
    def authorizes_trading(self) -> bool:
        return False


def qualify_recovery_release(
    *,
    policy: RecoveryQualificationPolicy,
    evidence: Sequence[RecoveryScenarioEvidence],
) -> RecoveryQualificationDecision:
    """Evaluate recovery evidence without performing recovery itself."""

    if not isinstance(policy, RecoveryQualificationPolicy):
        raise TypeError("policy must be RecoveryQualificationPolicy")
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        raise TypeError("evidence must be a sequence")

    by_scenario: dict[RecoveryScenario, RecoveryScenarioEvidence] = {}
    blockers: list[str] = []
    hard_failure = False
    inconclusive = False

    for item in evidence:
        if not isinstance(item, RecoveryScenarioEvidence):
            raise TypeError("evidence must contain RecoveryScenarioEvidence")
        if item.scenario in by_scenario:
            raise ValueError(f"duplicate evidence for {item.scenario.value}")
        by_scenario[item.scenario] = item

    missing = sorted(
        (scenario.value for scenario in _REQUIRED_SCENARIOS - set(by_scenario)),
    )
    for scenario in missing:
        blockers.append(f"missing_scenario:{scenario}")
        inconclusive = True

    for scenario in sorted(by_scenario, key=lambda item: item.value):
        item = by_scenario[scenario]
        prefix = scenario.value.lower()

        if item.source_sha != policy.source_sha:
            blockers.append(f"{prefix}:source_sha_mismatch")
            hard_failure = True
        if item.release_artifact_sha256 != policy.release_artifact_sha256:
            blockers.append(f"{prefix}:release_artifact_mismatch")
            hard_failure = True
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
        blockers=tuple(blockers),
        measured_downtime_ms=measured,
    )
