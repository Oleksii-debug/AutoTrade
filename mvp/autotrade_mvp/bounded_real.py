"""Fail-closed evidence gate for bounded real-account qualification.

This module never grants trading authority and never sends provider requests.
It only evaluates whether a completed, separately authorized bounded-real
qualification bundle is internally consistent with one exact build/envelope
and immutable evidence artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import FrozenSet, Iterable, Sequence
from uuid import UUID


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_ACTIONS = frozenset(
    {
        "ORDER.SUBMIT",
        "ORDER.CANCEL",
        "ORDER.REPLACE",
        "PROTECTION.MAINTAIN",
        "PROTECTION.REPAIR",
        "FLATTEN",
    }
)
_FORBIDDEN_ACTIONS = frozenset({"WITHDRAW", "TRANSFER", "CREDENTIAL.ROTATE"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _sha(value: str, *, name: str) -> str:
    result = _text(value, name=name).lower()
    if _SHA_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be an exact 40-character Git SHA")
    return result


def _digest(value: str, *, name: str) -> str:
    result = _text(value, name=name)
    if _DIGEST_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be a canonical SHA-256 digest")
    return result


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _bool(value, *, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be boolean")
    return value


def _actions(values: Iterable[str]) -> FrozenSet[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("allowed_actions must be a collection")
    result = frozenset(_text(value, name="action").upper() for value in values)
    if not result:
        raise ValueError("allowed_actions must be non-empty")
    forbidden = result & _FORBIDDEN_ACTIONS
    if forbidden:
        raise ValueError(
            "bounded-real qualification cannot include forbidden actions: "
            + ", ".join(sorted(forbidden))
        )
    unsupported = result - _ALLOWED_ACTIONS
    if unsupported:
        raise ValueError(
            "bounded-real qualification contains unsupported actions: "
            + ", ".join(sorted(unsupported))
        )
    return result


@dataclass(frozen=True)
class BoundedRealEnvelope:
    envelope_id: str
    source_sha: str
    account_id: str
    provider_id: str
    policy_id: str
    allowed_actions: FrozenSet[str]
    max_capital: Decimal
    max_single_notional: Decimal
    max_gross_leverage: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "envelope_id", _text(self.envelope_id, name="envelope_id"))
        object.__setattr__(self, "source_sha", _sha(self.source_sha, name="source_sha"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "provider_id", _text(self.provider_id, name="provider_id"))
        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(self, "allowed_actions", _actions(self.allowed_actions))
        object.__setattr__(self, "max_capital", _decimal(self.max_capital, name="max_capital"))
        object.__setattr__(
            self,
            "max_single_notional",
            _decimal(self.max_single_notional, name="max_single_notional"),
        )
        object.__setattr__(
            self,
            "max_gross_leverage",
            _decimal(self.max_gross_leverage, name="max_gross_leverage"),
        )

    @classmethod
    def create(cls, **values) -> "BoundedRealEnvelope":
        return cls(**values)


@dataclass(frozen=True)
class EvidenceRef:
    artifact_id: str
    sha256: str
    source_sha: str
    envelope_id: str
    provider_id: str
    account_id: str

    def __post_init__(self) -> None:
        artifact_id = _text(self.artifact_id, name="artifact_id")
        try:
            UUID(artifact_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("artifact_id must be a UUID") from error
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "sha256", _digest(self.sha256, name="sha256"))
        object.__setattr__(self, "source_sha", _sha(self.source_sha, name="source_sha"))
        object.__setattr__(self, "envelope_id", _text(self.envelope_id, name="envelope_id"))
        object.__setattr__(self, "provider_id", _text(self.provider_id, name="provider_id"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))

    @classmethod
    def create(cls, **values) -> "EvidenceRef":
        return cls(**values)


@dataclass(frozen=True)
class QualificationEvidence:
    evidence_kind: str
    evidence_ref: EvidenceRef
    passed: bool
    unresolved_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_ref, EvidenceRef):
            raise TypeError("evidence_ref must be EvidenceRef")
        blockers = tuple(
            _text(value, name="unresolved_blocker")
            for value in self.unresolved_blockers
        )
        if len(blockers) != len(set(blockers)):
            raise ValueError("unresolved_blockers must be unique")
        object.__setattr__(
            self,
            "evidence_kind",
            _text(self.evidence_kind, name="evidence_kind").upper(),
        )
        object.__setattr__(self, "passed", _bool(self.passed, name="passed"))
        object.__setattr__(self, "unresolved_blockers", blockers)

    @property
    def evidence_id(self) -> str:
        return self.evidence_ref.artifact_id

    @classmethod
    def create(cls, **values) -> "QualificationEvidence":
        return cls(**values)


@dataclass(frozen=True)
class ObservedFillEvidence:
    provider_execution_id: str
    partial_fill: bool
    evidence_ref: EvidenceRef

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_ref, EvidenceRef):
            raise TypeError("evidence_ref must be EvidenceRef")
        object.__setattr__(
            self,
            "provider_execution_id",
            _text(self.provider_execution_id, name="provider_execution_id"),
        )
        object.__setattr__(self, "partial_fill", _bool(self.partial_fill, name="partial_fill"))


@dataclass(frozen=True)
class BoundedRealObservations:
    source_sha: str
    envelope_id: str
    provider_id: str
    account_id: str
    fill_evidence: tuple[ObservedFillEvidence, ...]
    reconciliation_evidence: EvidenceRef
    revocation_evidence: EvidenceRef
    protection_evidence: EvidenceRef
    audit_evidence: EvidenceRef
    all_fills_reconciled: bool
    fees_reconciled: bool
    revocation_verified: bool
    protection_verified: bool
    unauthorized_action_count: int
    unresolved_unknown_count: int

    def __post_init__(self) -> None:
        fills = tuple(self.fill_evidence)
        if any(not isinstance(item, ObservedFillEvidence) for item in fills):
            raise TypeError("fill_evidence must contain ObservedFillEvidence")
        execution_ids = [item.provider_execution_id for item in fills]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("provider_execution_id must be unique")
        for value, name in (
            (self.reconciliation_evidence, "reconciliation_evidence"),
            (self.revocation_evidence, "revocation_evidence"),
            (self.protection_evidence, "protection_evidence"),
            (self.audit_evidence, "audit_evidence"),
        ):
            if not isinstance(value, EvidenceRef):
                raise TypeError(f"{name} must be EvidenceRef")
        for value, name in (
            (self.unauthorized_action_count, "unauthorized_action_count"),
            (self.unresolved_unknown_count, "unresolved_unknown_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "source_sha", _sha(self.source_sha, name="source_sha"))
        object.__setattr__(self, "envelope_id", _text(self.envelope_id, name="envelope_id"))
        object.__setattr__(self, "provider_id", _text(self.provider_id, name="provider_id"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "fill_evidence", fills)
        object.__setattr__(
            self,
            "all_fills_reconciled",
            _bool(self.all_fills_reconciled, name="all_fills_reconciled"),
        )
        object.__setattr__(self, "fees_reconciled", _bool(self.fees_reconciled, name="fees_reconciled"))
        object.__setattr__(
            self,
            "revocation_verified",
            _bool(self.revocation_verified, name="revocation_verified"),
        )
        object.__setattr__(
            self,
            "protection_verified",
            _bool(self.protection_verified, name="protection_verified"),
        )

    @property
    def observed_fill_count(self) -> int:
        return len(self.fill_evidence)

    @property
    def observed_partial_fill(self) -> bool:
        return any(item.partial_fill for item in self.fill_evidence)

    @classmethod
    def create(cls, **values) -> "BoundedRealObservations":
        return cls(**values)


@dataclass(frozen=True)
class BoundedRealQualificationResult:
    complete: bool
    reason_codes: tuple[str, ...]
    exact_source_sha: str
    envelope_id: str

    @property
    def authorizes_trading(self) -> bool:
        """Qualification evidence is never itself an authority token."""
        return False


_REQUIRED_EVIDENCE_KINDS = frozenset(
    {
        "RELEASE_CANDIDATE",
        "SCIENTIFIC_QUALIFICATION",
        "FORWARD_PAPER",
        "PROVIDER_CAPABILITY",
        "ACCESSIBILITY",
        "RECOVERY",
    }
)


def _scope_mismatch(ref: EvidenceRef, envelope: BoundedRealEnvelope) -> bool:
    return (
        ref.source_sha != envelope.source_sha
        or ref.envelope_id != envelope.envelope_id
        or ref.provider_id != envelope.provider_id
        or ref.account_id != envelope.account_id
    )


def _require_unique_artifacts(refs: Sequence[EvidenceRef]) -> None:
    artifact_ids: set[str] = set()
    digests: set[str] = set()
    for ref in refs:
        if ref.artifact_id in artifact_ids:
            raise ValueError("evidence artifact_id must be unique")
        if ref.sha256 in digests:
            raise ValueError("evidence sha256 must be unique across distinct artifacts")
        artifact_ids.add(ref.artifact_id)
        digests.add(ref.sha256)


def assess_bounded_real_qualification(
    *,
    envelope: BoundedRealEnvelope,
    prerequisite_evidence: Iterable[QualificationEvidence],
    observations: BoundedRealObservations,
) -> BoundedRealQualificationResult:
    """Validate a completed bounded-real evidence bundle without authorizing it."""

    if not isinstance(envelope, BoundedRealEnvelope):
        raise TypeError("envelope must be BoundedRealEnvelope")
    if not isinstance(observations, BoundedRealObservations):
        raise TypeError("observations must be BoundedRealObservations")

    reasons: list[str] = []
    if (
        observations.source_sha != envelope.source_sha
        or observations.envelope_id != envelope.envelope_id
        or observations.provider_id != envelope.provider_id
        or observations.account_id != envelope.account_id
    ):
        reasons.append("observation_scope_mismatch")

    evidence_by_kind: dict[str, QualificationEvidence] = {}
    prerequisites = tuple(prerequisite_evidence)
    for evidence in prerequisites:
        if not isinstance(evidence, QualificationEvidence):
            raise TypeError(
                "prerequisite_evidence must contain QualificationEvidence"
            )
        if evidence.evidence_kind in evidence_by_kind:
            raise ValueError(
                f"duplicate prerequisite evidence kind: {evidence.evidence_kind}"
            )
        evidence_by_kind[evidence.evidence_kind] = evidence

    refs = [item.evidence_ref for item in prerequisites]
    refs.extend(item.evidence_ref for item in observations.fill_evidence)
    refs.extend(
        (
            observations.reconciliation_evidence,
            observations.revocation_evidence,
            observations.protection_evidence,
            observations.audit_evidence,
        )
    )
    _require_unique_artifacts(refs)

    missing = sorted(_REQUIRED_EVIDENCE_KINDS - set(evidence_by_kind))
    if missing:
        reasons.append("missing_prerequisite_evidence:" + ",".join(missing))

    for kind in sorted(_REQUIRED_EVIDENCE_KINDS & set(evidence_by_kind)):
        evidence = evidence_by_kind[kind]
        if _scope_mismatch(evidence.evidence_ref, envelope):
            reasons.append(f"evidence_scope_mismatch:{kind}")
        if not evidence.passed:
            reasons.append(f"prerequisite_failed:{kind}")
        if evidence.unresolved_blockers:
            reasons.append(f"unresolved_blockers:{kind}")

    if observations.observed_fill_count < 1:
        reasons.append("no_real_fill_evidence")
    if not observations.observed_partial_fill:
        reasons.append("partial_fill_not_evidenced")
    for index, fill in enumerate(observations.fill_evidence):
        if _scope_mismatch(fill.evidence_ref, envelope):
            reasons.append(f"fill_evidence_scope_mismatch:{index}")

    for label, ref in (
        ("reconciliation", observations.reconciliation_evidence),
        ("revocation", observations.revocation_evidence),
        ("protection", observations.protection_evidence),
        ("audit", observations.audit_evidence),
    ):
        if _scope_mismatch(ref, envelope):
            reasons.append(f"{label}_evidence_scope_mismatch")

    if not observations.all_fills_reconciled:
        reasons.append("fills_not_fully_reconciled")
    if not observations.fees_reconciled:
        reasons.append("fees_not_reconciled")
    if not observations.revocation_verified:
        reasons.append("revocation_not_verified")
    if not observations.protection_verified:
        reasons.append("protection_not_verified")
    if observations.unauthorized_action_count != 0:
        reasons.append("unauthorized_action_observed")
    if observations.unresolved_unknown_count != 0:
        reasons.append("unresolved_submission_unknown")

    return BoundedRealQualificationResult(
        complete=not reasons,
        reason_codes=tuple(reasons),
        exact_source_sha=envelope.source_sha,
        envelope_id=envelope.envelope_id,
    )
