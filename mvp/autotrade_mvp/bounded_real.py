"""Fail-closed evidence gate for bounded real-account qualification.

This module never grants trading authority and never sends provider requests.
A PASS requires immutable evidence resolved by an explicit verifier; caller
booleans or syntactically valid references are insufficient on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Callable, FrozenSet, Iterable
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
    result = _text(value, name=name)
    if _SHA_RE.fullmatch(result) is None:
        raise ValueError(
            f"{name} must be an exact 40-character lowercase Git SHA"
        )
    return result


def _artifact_id(value: str) -> str:
    try:
        return str(UUID(_text(value, name="artifact_id")))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("artifact_id must be a UUID") from error


def _digest(value: str) -> str:
    result = _text(value, name="sha256")
    if _DIGEST_RE.fullmatch(result) is None:
        raise ValueError("sha256 must be canonical lowercase hexadecimal")
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
        raise ValueError(
            f"{name} must be {'non-negative' if allow_zero else 'positive'}"
        )
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
        object.__setattr__(self, "allowed_actions", _actions(self.allowed_actions))
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
    def create(cls, **values) -> "BoundedRealEnvelope":
        return cls(**values)


@dataclass(frozen=True)
class ImmutableEvidenceRef:
    artifact_id: str
    sha256: str
    evidence_kind: str
    source_sha: str
    envelope_id: str
    provider_id: str
    account_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _artifact_id(self.artifact_id))
        object.__setattr__(self, "sha256", _digest(self.sha256))
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
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )


@dataclass(frozen=True)
class EvidenceVerification:
    valid: bool
    conflicted: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        _bool(self.valid, name="valid")
        _bool(self.conflicted, name="conflicted")
        if self.valid and self.conflicted:
            raise ValueError("evidence cannot be both valid and conflicted")


def artifact_store_evidence_verifier(
    store: object,
) -> Callable[[ImmutableEvidenceRef], EvidenceVerification]:
    """Resolve bounded-real evidence through the canonical immutable store."""

    def verify(ref: ImmutableEvidenceRef) -> EvidenceVerification:
        try:
            manifest = getattr(store, "load_manifest")(ref.artifact_id)
            payload = getattr(store, "read_bytes")(ref.artifact_id)
        except Exception:
            return EvidenceVerification(
                valid=False,
                reason="immutable evidence artifact is missing or unreadable",
            )
        if type(manifest) is not dict or not isinstance(payload, bytes):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence artifact representation is invalid",
            )
        actual_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if (
            manifest.get("artifact_id") != ref.artifact_id
            or manifest.get("sha256") != ref.sha256
            or actual_digest != ref.sha256
        ):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence identity or digest mismatch",
            )
        metadata = manifest.get("metadata")
        if type(metadata) is not dict:
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence metadata is missing",
            )
        expected = {
            "artifact_kind": "BOUNDED_REAL_EVIDENCE",
            "schema_version": 1,
            "evidence_kind": ref.evidence_kind,
            "source_sha": ref.source_sha,
            "envelope_id": ref.envelope_id,
            "provider_id": ref.provider_id,
            "account_id": ref.account_id,
            "outcome": "PASS",
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence semantics do not match qualification scope",
            )
        if (
            not isinstance(metadata.get("producer_id"), str)
            or not metadata["producer_id"].strip()
            or not isinstance(metadata.get("evidence_version"), str)
            or not metadata["evidence_version"].strip()
        ):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence producer/version identity is missing",
            )
        return EvidenceVerification(valid=True)

    return verify


@dataclass(frozen=True)
class QualificationEvidence:
    evidence_id: str
    evidence_kind: str
    source_sha: str
    envelope_id: str
    passed: bool
    evidence_ref: ImmutableEvidenceRef
    unresolved_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        blockers = tuple(
            _text(value, name="unresolved_blocker")
            for value in self.unresolved_blockers
        )
        if len(blockers) != len(set(blockers)):
            raise ValueError("unresolved_blockers must be unique")
        evidence_id = _text(self.evidence_id, name="evidence_id")
        evidence_kind = _text(self.evidence_kind, name="evidence_kind").upper()
        source_sha = _sha(self.source_sha, name="source_sha")
        envelope_id = _text(self.envelope_id, name="envelope_id")
        if not isinstance(self.evidence_ref, ImmutableEvidenceRef):
            raise TypeError("evidence_ref must be ImmutableEvidenceRef")
        if self.evidence_ref.evidence_kind != f"PREREQUISITE:{evidence_kind}":
            raise ValueError("prerequisite evidence_ref kind does not match evidence_kind")
        if (
            self.evidence_ref.source_sha != source_sha
            or self.evidence_ref.envelope_id != envelope_id
        ):
            raise ValueError("prerequisite evidence_ref scope does not match evidence")
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "evidence_kind", evidence_kind)
        object.__setattr__(self, "source_sha", source_sha)
        object.__setattr__(self, "envelope_id", envelope_id)
        object.__setattr__(self, "passed", _bool(self.passed, name="passed"))
        object.__setattr__(self, "unresolved_blockers", blockers)

    @classmethod
    def create(cls, **values) -> "QualificationEvidence":
        return cls(**values)


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
    evidence_refs: tuple[ImmutableEvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        source_sha = _sha(self.source_sha, name="source_sha")
        envelope_id = _text(self.envelope_id, name="envelope_id")
        provider_id = _text(self.provider_id, name="provider_id")
        account_id = _text(self.account_id, name="account_id")
        for value, name in (
            (self.observed_fill_count, "observed_fill_count"),
            (self.unauthorized_action_count, "unauthorized_action_count"),
            (self.unresolved_unknown_count, "unresolved_unknown_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for field_name in (
            "observed_partial_fill",
            "all_fills_reconciled",
            "fees_reconciled",
            "revocation_verified",
            "protection_verified",
        ):
            _bool(getattr(self, field_name), name=field_name)
        refs = tuple(self.evidence_refs)
        artifact_ids: set[str] = set()
        digests: set[str] = set()
        kinds: set[str] = set()
        for ref in refs:
            if not isinstance(ref, ImmutableEvidenceRef):
                raise TypeError("evidence_refs must contain ImmutableEvidenceRef")
            if (
                ref.source_sha != source_sha
                or ref.envelope_id != envelope_id
                or ref.provider_id != provider_id
                or ref.account_id != account_id
            ):
                raise ValueError("observation evidence_ref scope does not match observations")
            if ref.artifact_id in artifact_ids or ref.sha256 in digests:
                raise ValueError("observation evidence artifacts must be unique")
            if ref.evidence_kind in kinds:
                raise ValueError("observation evidence kinds must be unique")
            artifact_ids.add(ref.artifact_id)
            digests.add(ref.sha256)
            kinds.add(ref.evidence_kind)
        object.__setattr__(self, "source_sha", source_sha)
        object.__setattr__(self, "envelope_id", envelope_id)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "evidence_refs", refs)

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
_REQUIRED_OBSERVATION_KINDS = frozenset(
    {
        "ACTUAL_FILL",
        "PARTIAL_FILL",
        "FILL_RECONCILIATION",
        "FEE_RECONCILIATION",
        "REVOCATION",
        "PROTECTION",
        "AUTHORITY_AUDIT",
        "UNKNOWN_SUBMISSION_AUDIT",
    }
)


def assess_bounded_real_qualification(
    *,
    envelope: BoundedRealEnvelope,
    prerequisite_evidence: Iterable[QualificationEvidence],
    observations: BoundedRealObservations,
    evidence_verifier: Callable[[ImmutableEvidenceRef], EvidenceVerification] | None = None,
) -> BoundedRealQualificationResult:
    """Validate a bounded-real evidence bundle without granting authority."""

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
    all_refs: list[tuple[str, ImmutableEvidenceRef]] = []
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
        all_refs.append(
            (f"PREREQUISITE:{evidence.evidence_kind}", evidence.evidence_ref)
        )

    observation_by_kind = {
        ref.evidence_kind: ref for ref in observations.evidence_refs
    }
    all_refs.extend(
        (f"OBSERVATION:{kind}", ref)
        for kind, ref in observation_by_kind.items()
    )

    artifact_ids: set[str] = set()
    digests: set[str] = set()
    for label, ref in all_refs:
        if ref.artifact_id in artifact_ids:
            raise ValueError("immutable evidence artifact_id must be unique")
        if ref.sha256 in digests:
            raise ValueError("immutable evidence digest must be unique")
        artifact_ids.add(ref.artifact_id)
        digests.add(ref.sha256)
        if (
            ref.source_sha != envelope.source_sha
            or ref.envelope_id != envelope.envelope_id
            or ref.provider_id != envelope.provider_id
            or ref.account_id != envelope.account_id
        ):
            reasons.append(f"immutable_evidence_scope_mismatch:{label}")

    missing = sorted(_REQUIRED_EVIDENCE_KINDS - set(evidence_by_kind))
    if missing:
        reasons.append("missing_prerequisite_evidence:" + ",".join(missing))
    missing_observations = sorted(
        _REQUIRED_OBSERVATION_KINDS - set(observation_by_kind)
    )
    if missing_observations:
        reasons.append(
            "missing_observation_evidence:" + ",".join(missing_observations)
        )

    for kind in sorted(_REQUIRED_EVIDENCE_KINDS & set(evidence_by_kind)):
        evidence = evidence_by_kind[kind]
        if evidence.source_sha != envelope.source_sha:
            reasons.append(f"source_sha_mismatch:{kind}")
        if evidence.envelope_id != envelope.envelope_id:
            reasons.append(f"envelope_mismatch:{kind}")
        if (
            evidence.evidence_ref.provider_id != envelope.provider_id
            or evidence.evidence_ref.account_id != envelope.account_id
        ):
            reasons.append(f"provider_account_mismatch:{kind}")
        if not evidence.passed:
            reasons.append(f"prerequisite_failed:{kind}")
        if evidence.unresolved_blockers:
            reasons.append(f"unresolved_blockers:{kind}")

    if evidence_verifier is None:
        reasons.append("immutable_evidence_verifier_required")
    else:
        for label, ref in all_refs:
            try:
                verification = evidence_verifier(ref)
            except Exception:
                verification = EvidenceVerification(
                    valid=False,
                    reason="immutable evidence verifier raised",
                )
            if not isinstance(verification, EvidenceVerification):
                raise TypeError(
                    "evidence_verifier must return EvidenceVerification"
                )
            if not verification.valid:
                suffix = "conflicted" if verification.conflicted else "unverified"
                reasons.append(f"immutable_evidence_{suffix}:{label}")

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
