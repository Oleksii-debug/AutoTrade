"""Fail-closed evidence gate for bounded real-account qualification.

This module never grants trading authority and never sends provider requests.
It only evaluates whether a completed, separately authorized bounded-real
qualification bundle is internally consistent with one exact build/envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import FrozenSet, Iterable


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
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


def _decimal(value, *, name: str, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


def _bool(value, *, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be boolean")
    return value


def _actions(values: Iterable[str]) -> FrozenSet[str]:
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
        object.__setattr__(
            self, "envelope_id", _text(self.envelope_id, name="envelope_id")
        )
        object.__setattr__(
            self, "source_sha", _sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id")
        )
        object.__setattr__(
            self, "policy_id", _text(self.policy_id, name="policy_id")
        )
        object.__setattr__(
            self, "allowed_actions", _actions(self.allowed_actions)
        )
        object.__setattr__(
            self, "max_capital", _decimal(self.max_capital, name="max_capital")
        )
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
    def create(
        cls,
        *,
        envelope_id: str,
        source_sha: str,
        account_id: str,
        provider_id: str,
        policy_id: str,
        allowed_actions: Iterable[str],
        max_capital,
        max_single_notional,
        max_gross_leverage,
    ) -> "BoundedRealEnvelope":
        return cls(
            envelope_id=_text(envelope_id, name="envelope_id"),
            source_sha=_sha(source_sha, name="source_sha"),
            account_id=_text(account_id, name="account_id"),
            provider_id=_text(provider_id, name="provider_id"),
            policy_id=_text(policy_id, name="policy_id"),
            allowed_actions=_actions(allowed_actions),
            max_capital=_decimal(max_capital, name="max_capital"),
            max_single_notional=_decimal(
                max_single_notional, name="max_single_notional"
            ),
            max_gross_leverage=_decimal(
                max_gross_leverage, name="max_gross_leverage"
            ),
        )


@dataclass(frozen=True)
class QualificationEvidence:
    evidence_id: str
    evidence_kind: str
    source_sha: str
    envelope_id: str
    passed: bool
    unresolved_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        blockers = tuple(
            _text(value, name="unresolved_blocker")
            for value in self.unresolved_blockers
        )
        if len(blockers) != len(set(blockers)):
            raise ValueError("unresolved_blockers must be unique")
        object.__setattr__(
            self, "evidence_id", _text(self.evidence_id, name="evidence_id")
        )
        object.__setattr__(
            self,
            "evidence_kind",
            _text(self.evidence_kind, name="evidence_kind").upper(),
        )
        object.__setattr__(
            self, "source_sha", _sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self, "envelope_id", _text(self.envelope_id, name="envelope_id")
        )
        object.__setattr__(self, "passed", _bool(self.passed, name="passed"))
        object.__setattr__(self, "unresolved_blockers", blockers)

    @classmethod
    def create(
        cls,
        *,
        evidence_id: str,
        evidence_kind: str,
        source_sha: str,
        envelope_id: str,
        passed: bool,
        unresolved_blockers: Iterable[str] = (),
    ) -> "QualificationEvidence":
        kind = _text(evidence_kind, name="evidence_kind").upper()
        blockers = tuple(
            _text(value, name="unresolved_blocker")
            for value in unresolved_blockers
        )
        if len(blockers) != len(set(blockers)):
            raise ValueError("unresolved_blockers must be unique")
        return cls(
            evidence_id=_text(evidence_id, name="evidence_id"),
            evidence_kind=kind,
            source_sha=_sha(source_sha, name="source_sha"),
            envelope_id=_text(envelope_id, name="envelope_id"),
            passed=_bool(passed, name="passed"),
            unresolved_blockers=blockers,
        )


@dataclass(frozen=True)
class BoundedRealObservations:
    source_sha: str
    envelope_id: str
    provider_id: str
    account_id: str
    observed_fill_count: int
    observed_partial_fill: bool
    all_fills_reconciled: bool
    fees_reconciled: bool
    revocation_verified: bool
    protection_verified: bool
    unauthorized_action_count: int
    unresolved_unknown_count: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.observed_fill_count, "observed_fill_count"),
            (self.unauthorized_action_count, "unauthorized_action_count"),
            (self.unresolved_unknown_count, "unresolved_unknown_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(
            self, "source_sha", _sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self, "envelope_id", _text(self.envelope_id, name="envelope_id")
        )
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        for field_name in (
            "observed_partial_fill",
            "all_fills_reconciled",
            "fees_reconciled",
            "revocation_verified",
            "protection_verified",
        ):
            object.__setattr__(
                self,
                field_name,
                _bool(getattr(self, field_name), name=field_name),
            )

    @classmethod
    def create(
        cls,
        *,
        source_sha: str,
        envelope_id: str,
        provider_id: str,
        account_id: str,
        observed_fill_count: int,
        observed_partial_fill: bool,
        all_fills_reconciled: bool,
        fees_reconciled: bool,
        revocation_verified: bool,
        protection_verified: bool,
        unauthorized_action_count: int,
        unresolved_unknown_count: int,
    ) -> "BoundedRealObservations":
        for value, name in (
            (observed_fill_count, "observed_fill_count"),
            (unauthorized_action_count, "unauthorized_action_count"),
            (unresolved_unknown_count, "unresolved_unknown_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        return cls(
            source_sha=_sha(source_sha, name="source_sha"),
            envelope_id=_text(envelope_id, name="envelope_id"),
            provider_id=_text(provider_id, name="provider_id"),
            account_id=_text(account_id, name="account_id"),
            observed_fill_count=observed_fill_count,
            observed_partial_fill=_bool(
                observed_partial_fill, name="observed_partial_fill"
            ),
            all_fills_reconciled=_bool(
                all_fills_reconciled, name="all_fills_reconciled"
            ),
            fees_reconciled=_bool(fees_reconciled, name="fees_reconciled"),
            revocation_verified=_bool(
                revocation_verified, name="revocation_verified"
            ),
            protection_verified=_bool(
                protection_verified, name="protection_verified"
            ),
            unauthorized_action_count=unauthorized_action_count,
            unresolved_unknown_count=unresolved_unknown_count,
        )


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

    scope_matches = (
        observations.source_sha == envelope.source_sha
        and observations.envelope_id == envelope.envelope_id
        and observations.provider_id == envelope.provider_id
        and observations.account_id == envelope.account_id
    )
    if not scope_matches:
        reasons.append("observation_scope_mismatch")

    evidence_by_kind: dict[str, QualificationEvidence] = {}
    evidence_ids: set[str] = set()
    for evidence in prerequisite_evidence:
        if not isinstance(evidence, QualificationEvidence):
            raise TypeError(
                "prerequisite_evidence must contain QualificationEvidence"
            )
        if evidence.evidence_id in evidence_ids:
            raise ValueError("evidence_id must be unique")
        evidence_ids.add(evidence.evidence_id)
        if evidence.evidence_kind in evidence_by_kind:
            raise ValueError(
                f"duplicate prerequisite evidence kind: {evidence.evidence_kind}"
            )
        evidence_by_kind[evidence.evidence_kind] = evidence

    missing = sorted(_REQUIRED_EVIDENCE_KINDS - set(evidence_by_kind))
    if missing:
        reasons.append("missing_prerequisite_evidence:" + ",".join(missing))

    for kind in sorted(_REQUIRED_EVIDENCE_KINDS & set(evidence_by_kind)):
        evidence = evidence_by_kind[kind]
        if evidence.source_sha != envelope.source_sha:
            reasons.append(f"source_sha_mismatch:{kind}")
        if evidence.envelope_id != envelope.envelope_id:
            reasons.append(f"envelope_mismatch:{kind}")
        if not evidence.passed:
            reasons.append(f"prerequisite_failed:{kind}")
        if evidence.unresolved_blockers:
            reasons.append(f"unresolved_blockers:{kind}")

    if observations.observed_fill_count < 1:
        reasons.append("no_real_fill_evidence")
    if not observations.observed_partial_fill:
        reasons.append("partial_fill_not_evidenced")
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
