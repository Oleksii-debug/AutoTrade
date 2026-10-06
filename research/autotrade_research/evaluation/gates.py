"""Registered scientific evaluation gate foundation."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterable, Mapping
from uuid import UUID

from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
)
from mvp.autotrade_mvp.qualification_attestation import (
    QualificationTrustError,
    QualificationTrustUnavailable,
    parse_signed_qualification_attestation,
    verify_canonical_qualification_attestation,
)

from ..artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from ..io.strict_json import strict_json_loads


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_REVIEW_DOMAIN = "SCIENCE"
_REVIEW_GATE = "EVALUATION-GATES"
_REVIEW_PACKAGE_ID = "WP-36"
_REVIEW_PROTOCOL_VERSION = "1.0.0"
_REVIEW_REQUIREMENT_PREFIX = "WP36-INDEPENDENT-REVIEW-"


def _qualification_time(value: str, *, name: str) -> datetime:
    if type(value) is not str or value != value.strip():
        raise ValueError(f"{name} must be exact timezone-aware ISO text")
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be timezone-aware ISO text") from error
    if point.tzinfo is None or point.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return point.astimezone(timezone.utc)


def _detached_gate_input(value, expected_type):
    if type(value) is not expected_type:
        raise TypeError(f"scientific gate requires exact {expected_type.__name__}")
    state = object.__getattribute__(value, "__dict__")
    if type(state) is not dict or set(state) != {field.name for field in fields(expected_type)}:
        raise TypeError("scientific gate input has unexpected state fields")
    # Frozen dataclasses can still be changed through object.__setattr__.
    # Reconstruct before consulting any scalar or nested authority-bearing fact.
    return replace(value)


def _decimal(value, *, name: str) -> Decimal:
    # Decimal is subclassable and a subclass can override methods consulted by
    # normalization/formatting. Gate authority accepts only exact scalar input
    # types before any Decimal semantics are dispatched.
    if type(value) not in {Decimal, str, int}:
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    try:
        canonical_decimal_text(result)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    return result


_REPORT_EVIDENCE_KINDS = frozenset(
    {
        "profile",
        "trial_log",
        "causal_audit",
        "financial_invariants",
        "retention",
        "locked_evaluation",
        "metrics",
    }
)

_REQUIRED_EVIDENCE_KINDS = frozenset(
    {
        "profile",
        "code",
        "data",
        "model",
        "config",
        "cost",
        "rights",
        "environment",
        "trial_log",
        "causal_audit",
        "financial_invariants",
        "retention",
        "locked_evaluation",
        "metrics",
        "independent_review",
    }
)
_REVIEWED_EVIDENCE_KINDS = _REQUIRED_EVIDENCE_KINDS - {"independent_review"}


@dataclass(frozen=True)
class GateEvidenceRef:
    artifact_id: str
    sha256: str

    def __post_init__(self) -> None:
        try:
            artifact_id = str(UUID(self.artifact_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("evidence artifact_id must be a UUID") from error
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 71
            or not self.sha256.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in self.sha256[7:])
        ):
            raise ValueError("evidence sha256 must be a canonical SHA-256 digest")
        object.__setattr__(self, "artifact_id", artifact_id)


@dataclass(frozen=True)
class GateProfile:
    profile_id: str
    minimum_net_advantage: Decimal
    max_drawdown: Decimal
    max_adverse_cost_loss: Decimal
    min_power: Decimal
    primary_baseline_id: str
    baseline_ids: tuple[str, ...]
    selection_correction: str
    max_trials: int
    required_regimes: tuple[str, ...]
    require_complete_trials: bool
    require_causal_audit: bool
    require_financial_invariants: bool
    require_untouched_holdout: bool = True
    require_walk_forward: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id is required")
        practical_advantage = _decimal(
            self.minimum_net_advantage,
            name="minimum_net_advantage",
        )
        drawdown = _decimal(self.max_drawdown, name="max_drawdown")
        adverse_cost_limit = _decimal(
            self.max_adverse_cost_loss,
            name="max_adverse_cost_loss",
        )
        power = _decimal(self.min_power, name="min_power")
        if practical_advantage < 0:
            raise ValueError("minimum_net_advantage must be non-negative")
        if drawdown < 0 or drawdown > 1:
            raise ValueError("max_drawdown must be between zero and one")
        if adverse_cost_limit < 0:
            raise ValueError("max_adverse_cost_loss must be non-negative")
        if power <= 0 or power > 1:
            raise ValueError("min_power must be in (0, 1]")

        if not isinstance(self.primary_baseline_id, str) or not self.primary_baseline_id.strip():
            raise ValueError("primary_baseline_id is required")
        if isinstance(self.baseline_ids, (str, bytes)):
            raise TypeError("baseline_ids must be a collection")
        baselines_raw = tuple(self.baseline_ids)
        if any(not isinstance(value, str) for value in baselines_raw):
            raise TypeError("baseline_ids must contain text values")
        baselines = tuple(value.strip() for value in baselines_raw)
        if not baselines or any(not value for value in baselines):
            raise ValueError("baseline_ids must be non-empty")
        if len(set(baselines)) != len(baselines):
            raise ValueError("baseline_ids must be unique")
        primary_baseline = self.primary_baseline_id.strip()
        if primary_baseline not in baselines:
            raise ValueError("primary_baseline_id must be registered in baseline_ids")

        if not isinstance(self.selection_correction, str) or not self.selection_correction.strip():
            raise ValueError("selection_correction is required")
        correction = self.selection_correction.strip()
        if type(self.max_trials) is not int or self.max_trials <= 0:
            raise ValueError("max_trials must be a positive integer")
        if self.max_trials > 1 and correction.lower() in {
            "none",
            "no",
            "unadjusted",
            "disabled",
            "n/a",
            "na",
        }:
            raise ValueError(
                "multi-trial protocols require an explicit multiplicity treatment"
            )
        if isinstance(self.required_regimes, (str, bytes)):
            raise TypeError("required_regimes must be a collection")
        regimes_raw = tuple(self.required_regimes)
        if any(not isinstance(value, str) for value in regimes_raw):
            raise TypeError("required_regimes must contain text values")
        regimes = tuple(value.strip() for value in regimes_raw)
        if not regimes or any(not value for value in regimes):
            raise ValueError("required_regimes must be non-empty")
        if len(set(regimes)) != len(regimes):
            raise ValueError("required_regimes must be unique")

        for name in (
            "require_complete_trials",
            "require_causal_audit",
            "require_financial_invariants",
            "require_untouched_holdout",
            "require_walk_forward",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be boolean")

        object.__setattr__(self, "profile_id", self.profile_id.strip())
        object.__setattr__(self, "minimum_net_advantage", practical_advantage)
        object.__setattr__(self, "max_drawdown", drawdown)
        object.__setattr__(self, "max_adverse_cost_loss", adverse_cost_limit)
        object.__setattr__(self, "min_power", power)
        object.__setattr__(self, "primary_baseline_id", primary_baseline)
        object.__setattr__(self, "baseline_ids", baselines)
        object.__setattr__(self, "selection_correction", correction)
        object.__setattr__(self, "required_regimes", regimes)

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        minimum_net_advantage,
        max_drawdown,
        max_adverse_cost_loss,
        min_power,
        primary_baseline_id: str,
        baseline_ids: Iterable[str],
        selection_correction: str,
        max_trials: int,
        required_regimes: Iterable[str],
        require_complete_trials: bool = True,
        require_causal_audit: bool = True,
        require_financial_invariants: bool = True,
        require_untouched_holdout: bool = True,
        require_walk_forward: bool = True,
    ) -> "GateProfile":
        return cls(
            profile_id=profile_id,
            minimum_net_advantage=minimum_net_advantage,
            max_drawdown=max_drawdown,
            max_adverse_cost_loss=max_adverse_cost_loss,
            min_power=min_power,
            primary_baseline_id=primary_baseline_id,
            baseline_ids=(
                tuple(baseline_ids)
                if not isinstance(baseline_ids, (str, bytes))
                else baseline_ids
            ),
            selection_correction=selection_correction,
            max_trials=max_trials,
            required_regimes=(
                tuple(required_regimes)
                if not isinstance(required_regimes, (str, bytes))
                else required_regimes
            ),
            require_complete_trials=require_complete_trials,
            require_causal_audit=require_causal_audit,
            require_financial_invariants=require_financial_invariants,
            require_untouched_holdout=require_untouched_holdout,
            require_walk_forward=require_walk_forward,
        )


@dataclass(frozen=True)
class EvaluationEvidence:
    registered_profile_id: str
    profile_unchanged_after_results: bool
    reproducible: bool
    causal_audit_passed: bool | None
    financial_invariants_passed: bool | None
    trial_log_complete: bool | None
    dependence_aware_lower_bound: Decimal | None
    estimated_power: Decimal | None
    net_advantage: Decimal | None
    drawdown: Decimal | None
    adverse_cost_loss: Decimal | None
    retention_passed: bool | None
    baseline_advantages: Mapping[str, Decimal] | None
    selection_correction_applied: str | None
    trials_attempted: int | None
    regime_coverage: frozenset[str] | None
    untouched_holdout_passed: bool | None = None
    valid_sequential_evaluation_passed: bool | None = None
    walk_forward_passed: bool | None = None
    evidence_bundle_id: str | None = None
    evidence_refs: Mapping[str, GateEvidenceRef] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.registered_profile_id, str)
            or not self.registered_profile_id.strip()
        ):
            raise ValueError("registered_profile_id must be a non-empty string")
        object.__setattr__(
            self,
            "registered_profile_id",
            self.registered_profile_id.strip(),
        )

        if self.evidence_bundle_id is not None:
            if type(self.evidence_bundle_id) is not str:
                raise ValueError("evidence_bundle_id must be a canonical UUID")
            try:
                bundle_id = str(UUID(self.evidence_bundle_id))
            except (ValueError, AttributeError, TypeError) as error:
                raise ValueError("evidence_bundle_id must be a canonical UUID") from error
            if bundle_id != self.evidence_bundle_id:
                raise ValueError("evidence_bundle_id must be a canonical UUID")
            object.__setattr__(self, "evidence_bundle_id", bundle_id)

        for name in (
            "dependence_aware_lower_bound",
            "estimated_power",
            "net_advantage",
            "drawdown",
            "adverse_cost_loss",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name=name))

        for name in (
            "profile_unchanged_after_results",
            "reproducible",
            "causal_audit_passed",
            "financial_invariants_passed",
            "trial_log_complete",
            "retention_passed",
            "untouched_holdout_passed",
            "valid_sequential_evaluation_passed",
            "walk_forward_passed",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be boolean or None")

        if self.estimated_power is not None and not (0 <= self.estimated_power <= 1):
            raise ValueError("estimated_power must be between zero and one")
        if self.drawdown is not None and self.drawdown < 0:
            raise ValueError("drawdown must be non-negative")
        if self.adverse_cost_loss is not None and self.adverse_cost_loss < 0:
            raise ValueError("adverse_cost_loss must be non-negative")

        if self.baseline_advantages is not None:
            if not isinstance(self.baseline_advantages, Mapping):
                raise TypeError("baseline_advantages must be a mapping or None")
            normalized_baselines: dict[str, Decimal] = {}
            for key, value in self.baseline_advantages.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("baseline advantage id is required")
                normalized = key.strip()
                if normalized in normalized_baselines:
                    raise ValueError("duplicate normalized baseline advantage id")
                normalized_baselines[normalized] = _decimal(
                    value,
                    name=f"baseline_advantage[{normalized}]",
                )
            object.__setattr__(
                self,
                "baseline_advantages",
                MappingProxyType(normalized_baselines),
            )

        if self.evidence_refs is not None:
            if not isinstance(self.evidence_refs, Mapping):
                raise TypeError("evidence_refs must be a mapping or None")
            normalized_refs: dict[str, GateEvidenceRef] = {}
            for raw_kind, raw_ref in self.evidence_refs.items():
                if not isinstance(raw_kind, str) or not raw_kind.strip():
                    raise ValueError("evidence ref kind is required")
                kind = raw_kind.strip()
                if kind in normalized_refs:
                    raise ValueError("duplicate normalized evidence ref kind")
                if type(raw_ref) is not GateEvidenceRef:
                    raise TypeError("evidence_refs values must be GateEvidenceRef")
                normalized_refs[kind] = raw_ref
            object.__setattr__(
                self,
                "evidence_refs",
                MappingProxyType(normalized_refs),
            )

        if self.selection_correction_applied is not None:
            if (
                not isinstance(self.selection_correction_applied, str)
                or not self.selection_correction_applied.strip()
            ):
                raise ValueError(
                    "selection_correction_applied must be text or None"
                )
            object.__setattr__(
                self,
                "selection_correction_applied",
                self.selection_correction_applied.strip(),
            )

        if self.trials_attempted is not None and (
            type(self.trials_attempted) is not int or self.trials_attempted < 0
        ):
            raise ValueError(
                "trials_attempted must be a non-negative integer or None"
            )

        if self.regime_coverage is not None:
            if isinstance(self.regime_coverage, (str, bytes)):
                raise TypeError("regime_coverage must be a collection or None")
            coverage_raw = tuple(self.regime_coverage)
            if any(not isinstance(value, str) for value in coverage_raw):
                raise TypeError("regime_coverage must contain text values")
            normalized_regimes = frozenset(value.strip() for value in coverage_raw)
            if not normalized_regimes or any(not value for value in normalized_regimes):
                raise ValueError("regime_coverage cannot contain empty values")
            object.__setattr__(self, "regime_coverage", normalized_regimes)

    @classmethod
    def create(cls, **kwargs) -> "EvaluationEvidence":
        return cls(**kwargs)


@dataclass(frozen=True)
class GateDecision:
    status: str
    reasons: tuple[str, ...]
    checks: Mapping[str, str]
    provenance: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        status = self.status.strip() if isinstance(self.status, str) else ""
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ValueError("GateDecision status must be PASS, FAIL or INCONCLUSIVE")
        if isinstance(self.reasons, (str, bytes)):
            raise TypeError("GateDecision reasons must be a collection")
        reasons = tuple(self.reasons)
        if not reasons or any(
            not isinstance(reason, str) or not reason.strip() for reason in reasons
        ):
            raise ValueError("GateDecision reasons must contain non-empty text")
        if not isinstance(self.checks, Mapping):
            raise TypeError("GateDecision checks must be a mapping")
        frozen_checks: dict[str, str] = {}
        for raw_name, raw_value in self.checks.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError("GateDecision check name is required")
            if not isinstance(raw_value, str):
                raise TypeError("GateDecision check status must be text")
            name = raw_name.strip()
            value = raw_value.strip()
            if name in frozen_checks:
                raise ValueError("duplicate normalized GateDecision check name")
            if value not in {"PASS", "FAIL", "INCONCLUSIVE"}:
                raise ValueError("unsupported GateDecision check status")
            frozen_checks[name] = value
        if not frozen_checks:
            raise ValueError("GateDecision checks cannot be empty")

        if self.provenance is None:
            frozen_provenance: dict[str, str] = {}
        else:
            if not isinstance(self.provenance, Mapping):
                raise TypeError("GateDecision provenance must be a mapping")
            frozen_provenance = {}
            for raw_name, raw_value in self.provenance.items():
                if not isinstance(raw_name, str) or not raw_name.strip():
                    raise ValueError("GateDecision provenance name is required")
                if not isinstance(raw_value, str) or not raw_value.strip():
                    raise ValueError("GateDecision provenance value is required")
                name = raw_name.strip()
                if name in frozen_provenance:
                    raise ValueError("duplicate normalized GateDecision provenance name")
                frozen_provenance[name] = raw_value.strip()

        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reasons", tuple(reason.strip() for reason in reasons))
        object.__setattr__(self, "checks", MappingProxyType(frozen_checks))
        object.__setattr__(self, "provenance", MappingProxyType(frozen_provenance))


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise ValueError("scientific report Decimal is outside exact authority") from error


def gate_report_payload(
    kind: str,
    profile: GateProfile,
    evidence: EvaluationEvidence,
) -> bytes:
    """Canonical semantic payload for PASS-critical scientific report artifacts.

    Independent review is intentionally excluded. A gate evaluator cannot
    manufacture evidence that claims to be independent of itself.
    """

    if kind not in _REPORT_EVIDENCE_KINDS:
        raise ValueError("kind is not a scientific report evidence kind")
    if type(profile) is not GateProfile:
        raise TypeError("profile must be GateProfile")
    if type(evidence) is not EvaluationEvidence:
        raise TypeError("evidence must be EvaluationEvidence")

    profile_id = profile.profile_id
    if kind == "profile":
        value = {
            "profile_id": profile_id,
            "minimum_net_advantage": _decimal_text(profile.minimum_net_advantage),
            "max_drawdown": _decimal_text(profile.max_drawdown),
            "max_adverse_cost_loss": _decimal_text(profile.max_adverse_cost_loss),
            "min_power": _decimal_text(profile.min_power),
            "primary_baseline_id": profile.primary_baseline_id,
            "baseline_ids": list(profile.baseline_ids),
            "selection_correction": profile.selection_correction,
            "max_trials": profile.max_trials,
            "required_regimes": list(profile.required_regimes),
            "require_complete_trials": profile.require_complete_trials,
            "require_causal_audit": profile.require_causal_audit,
            "require_financial_invariants": profile.require_financial_invariants,
            "require_untouched_holdout": profile.require_untouched_holdout,
            "require_walk_forward": profile.require_walk_forward,
        }
    elif kind == "trial_log":
        value = {
            "profile_id": profile_id,
            "complete": evidence.trial_log_complete,
            "trials_attempted": evidence.trials_attempted,
            "selection_correction_applied": evidence.selection_correction_applied,
        }
    elif kind == "causal_audit":
        value = {
            "profile_id": profile_id,
            "passed": evidence.causal_audit_passed,
        }
    elif kind == "financial_invariants":
        value = {
            "profile_id": profile_id,
            "passed": evidence.financial_invariants_passed,
        }
    elif kind == "retention":
        value = {
            "profile_id": profile_id,
            "passed": evidence.retention_passed,
        }
    elif kind == "locked_evaluation":
        value = {
            "profile_id": profile_id,
            "untouched_holdout_passed": evidence.untouched_holdout_passed,
            "valid_sequential_evaluation_passed": evidence.valid_sequential_evaluation_passed,
            "walk_forward_passed": evidence.walk_forward_passed,
        }
    else:  # metrics
        value = {
            "profile_id": profile_id,
            "reproducible": evidence.reproducible,
            "dependence_aware_lower_bound": _decimal_text(
                evidence.dependence_aware_lower_bound
            ),
            "estimated_power": _decimal_text(evidence.estimated_power),
            "net_advantage": _decimal_text(evidence.net_advantage),
            "drawdown": _decimal_text(evidence.drawdown),
            "adverse_cost_loss": _decimal_text(evidence.adverse_cost_loss),
            "baseline_advantages": (
                None
                if evidence.baseline_advantages is None
                else {
                    key: _decimal_text(amount)
                    for key, amount in sorted(evidence.baseline_advantages.items())
                }
            ),
            "regime_coverage": (
                None
                if evidence.regime_coverage is None
                else sorted(evidence.regime_coverage)
            ),
        }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reviewed_graph_identity(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    manifests: Mapping[str, Mapping[str, object]],
    *,
    expected_source_sha: str,
    qualification_at: str | None = None,
) -> tuple[str, str]:
    artifacts: list[dict[str, str]] = []
    for kind in sorted(_REVIEWED_EVIDENCE_KINDS):
        ref = evidence.evidence_refs[kind]  # type: ignore[index]
        manifest = manifests[kind]
        manifest_hash = manifest.get("manifest_hash")
        media_type = manifest.get("media_type")
        if not isinstance(manifest_hash, str) or not isinstance(media_type, str):
            raise ValueError("evidence graph lacks immutable manifest identity")
        artifacts.append(
            {
                "artifact_id": ref.artifact_id,
                "kind": kind,
                "manifest_hash": manifest_hash,
                "media_type": media_type,
                "sha256": ref.sha256,
            }
        )
    payload = {
        "artifacts": artifacts,
        "evidence_bundle_id": evidence.evidence_bundle_id,
        "profile_id": profile.profile_id,
        "schema_version": "1.0.0",
        "source_sha": expected_source_sha,
    }
    if qualification_at is not None:
        payload["qualification_at"] = qualification_at
    digest = "sha256:" + sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    requirement_id = _REVIEW_REQUIREMENT_PREFIX + digest[7:]
    return digest, requirement_id


def _verify_independent_review(
    payload: bytes,
    *,
    artifact_store: ArtifactStore,
    evidence_root: str | Path,
    expected_source_sha: str,
    evidence_bundle_id: str,
    requirement_id: str,
    qualification_at: str | None = None,
) -> tuple[bool | None, Mapping[str, str]]:
    """Verify externally issued review through canonical qualification trust."""

    try:
        decoded = payload.decode("utf-8")
        receipt = parse_signed_qualification_attestation(strict_json_loads(decoded))
        if qualification_at is not None:
            cutoff = _qualification_time(qualification_at, name="qualification_at")
            attestation = receipt.attestation
            if any(_qualification_time(value, name="review chronology") > cutoff for value in (
                attestation.started_at, attestation.completed_at, attestation.signed_at,
            )):
                return False, MappingProxyType({})
            # Review evidence is a nested graph: a backdated signature cannot
            # turn an artifact first published after the frozen cut into past truth.
            reader = trusted_authenticated_reader(evidence_root, publication_store=artifact_store)
            for ref in attestation.evidence_refs:
                manifest, _ = reader(ref.artifact_id)
                if _qualification_time(manifest.get("created_at"), name="review evidence created_at") > cutoff:
                    return False, MappingProxyType({})
    except (UnicodeError, ValueError, TypeError, QualificationTrustError, ArtifactIntegrityError, OSError):
        return False, MappingProxyType({})

    try:
        accepted = verify_canonical_qualification_attestation(
            receipt,
            evidence_store=artifact_store,
            evidence_root=evidence_root,
            expected_source_sha=expected_source_sha,
            expected_domain=_REVIEW_DOMAIN,
            expected_gate=_REVIEW_GATE,
            expected_package_id=_REVIEW_PACKAGE_ID,
            expected_protocol_id=evidence_bundle_id,
            expected_protocol_version=_REVIEW_PROTOCOL_VERSION,
            expected_requirement_id=requirement_id,
        )
    except QualificationTrustUnavailable:
        return None, MappingProxyType({})
    except (QualificationTrustError, ArtifactIntegrityError, OSError, TypeError, ValueError):
        return False, MappingProxyType({})

    if accepted.result != "PASS":
        return False, MappingProxyType({})

    attestation = receipt.attestation
    provenance = MappingProxyType(
        {
            "review_attestation_id": accepted.attestation_id,
            "review_attestation_digest": accepted.attestation_digest,
            "review_policy_id": accepted.policy_id,
            "review_policy_version": accepted.policy_version,
            "review_trust_root_id": accepted.trust_root_id,
            "review_producer_id": attestation.producer_id,
            "review_verifier_id": attestation.verifier_id,
            "review_requirement_id": requirement_id,
            "review_source_sha": accepted.source_sha,
        }
    )
    return True, provenance


def _verify_evidence_bundle(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    artifact_store: ArtifactStore | None,
    *,
    evidence_root: str | Path | None = None,
    expected_source_sha: str | None = None,
    qualification_at: str | None = None,
) -> tuple[bool | None, Mapping[str, str]]:
    """Verify the immutable G0/G1/G2/G3/G4 evidence graph and review authority.

    Invalid supplied evidence fails closed. A locally valid graph without an
    independently selected artifact root and canonical signed review remains
    INCONCLUSIVE and can never authorize terminal PASS.
    """

    if artifact_store is None or evidence.evidence_refs is None:
        return None, MappingProxyType({})
    if type(artifact_store) is not ArtifactStore:
        return False, MappingProxyType({})
    if evidence.evidence_bundle_id is None:
        return False, MappingProxyType({})

    refs = evidence.evidence_refs
    if set(refs) != _REQUIRED_EVIDENCE_KINDS:
        return False, MappingProxyType({})
    if expected_source_sha is not None and (
        type(expected_source_sha) is not str
        or _GIT_SHA.fullmatch(expected_source_sha) is None
    ):
        return False, MappingProxyType({})

    trusted_root_bound = False
    if evidence_root is None:
        read_snapshot = artifact_store.read_authenticated_snapshot
    else:
        try:
            read_snapshot = trusted_authenticated_reader(
                evidence_root,
                publication_store=artifact_store,
            )
        except (ArtifactIntegrityError, OSError, TypeError, ValueError):
            return False, MappingProxyType({})
        trusted_root_bound = True

    manifests: dict[str, Mapping[str, object]] = {}
    independent_review_payload: bytes | None = None
    for kind in sorted(_REQUIRED_EVIDENCE_KINDS):
        ref = refs[kind]
        try:
            manifest, payload = read_snapshot(ref.artifact_id)
        except (FileNotFoundError, ArtifactIntegrityError, ValueError, OSError):
            return False, MappingProxyType({})
        if type(manifest) is not dict or type(payload) is not bytes:
            return False, MappingProxyType({})
        if manifest.get("manifest_hash") is None:
            return False, MappingProxyType({})
        if qualification_at is not None:
            try:
                if _qualification_time(manifest.get("created_at"), name="artifact created_at") > _qualification_time(qualification_at, name="qualification_at"):
                    return False, MappingProxyType({})
            except (TypeError, ValueError):
                return False, MappingProxyType({})
        if manifest.get("sha256") != ref.sha256:
            return False, MappingProxyType({})
        metadata = manifest.get("metadata")
        if type(metadata) is not dict:
            return False, MappingProxyType({})
        if metadata.get("evidence_kind") != kind:
            return False, MappingProxyType({})
        if metadata.get("profile_id") != profile.profile_id:
            return False, MappingProxyType({})
        if metadata.get("evidence_bundle_id") != evidence.evidence_bundle_id:
            return False, MappingProxyType({})
        rights = manifest.get("rights")
        if type(rights) is not dict or rights.get("storage") is not True:
            return False, MappingProxyType({})
        if expected_source_sha is not None:
            source_refs = manifest.get("source_refs")
            if (
                type(source_refs) is not list
                or f"git:{expected_source_sha}" not in source_refs
            ):
                return False, MappingProxyType({})
        if kind in _REPORT_EVIDENCE_KINDS:
            if manifest.get("media_type") != "application/json":
                return False, MappingProxyType({})
            if payload != gate_report_payload(kind, profile, evidence):
                return False, MappingProxyType({})
        elif kind == "independent_review":
            if manifest.get("media_type") != "application/json":
                return False, MappingProxyType({})
            independent_review_payload = payload
        manifests[kind] = manifest

    # Local integrity evidence is useful diagnostically, but no independently
    # selected root/source authority means terminal provenance is unavailable.
    if not trusted_root_bound or expected_source_sha is None:
        return None, MappingProxyType({})
    if independent_review_payload is None:
        return False, MappingProxyType({})

    try:
        graph_digest, requirement_id = _reviewed_graph_identity(
            profile,
            evidence,
            manifests,
            expected_source_sha=expected_source_sha,
            qualification_at=qualification_at,
        )
    except (KeyError, TypeError, ValueError):
        return False, MappingProxyType({})

    review_status, review_provenance = _verify_independent_review(
        independent_review_payload,
        artifact_store=artifact_store,
        evidence_root=evidence_root,
        expected_source_sha=expected_source_sha,
        evidence_bundle_id=evidence.evidence_bundle_id,
        requirement_id=requirement_id,
        qualification_at=qualification_at,
    )
    if review_status is not True:
        return review_status, review_provenance

    provenance = {
        "evidence_bundle_id": evidence.evidence_bundle_id,
        "evidence_graph_digest": graph_digest,
        "independent_review_artifact_id": refs["independent_review"].artifact_id,
        "independent_review_artifact_sha256": refs["independent_review"].sha256,
        **dict(review_provenance),
    }
    if qualification_at is not None:
        provenance["qualification_at"] = qualification_at
    return True, MappingProxyType(provenance)


def evaluate_gates(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    *,
    artifact_store: ArtifactStore | None = None,
    evidence_root: str | Path | None = None,
    expected_source_sha: str | None = None,
    qualification_at: str | None = None,
) -> GateDecision:
    profile = _detached_gate_input(profile, GateProfile)
    evidence = _detached_gate_input(evidence, EvaluationEvidence)
    if qualification_at is not None:
        qualification_at = _qualification_time(qualification_at, name="qualification_at").isoformat().replace("+00:00", "Z")
    checks: dict[str, str] = {}
    failures: list[str] = []
    unknowns: list[str] = []

    def check(name: str, condition: bool | None, failure: str, unknown: str) -> None:
        if condition is True:
            checks[name] = "PASS"
        elif condition is False:
            checks[name] = "FAIL"
            failures.append(failure)
        else:
            checks[name] = "INCONCLUSIVE"
            unknowns.append(unknown)

    check(
        "profile_identity",
        evidence.registered_profile_id == profile.profile_id,
        "evaluation used a different gate profile",
        "gate profile identity is unavailable",
    )
    evidence_status, evidence_provenance = _verify_evidence_bundle(
        profile,
        evidence,
        artifact_store,
        evidence_root=evidence_root,
        expected_source_sha=expected_source_sha,
        qualification_at=qualification_at,
    )
    check(
        "frozen_qualification_cut",
        True if qualification_at is not None else None,
        "",
        "scientific qualification time has not been frozen",
    )
    check(
        "evidence_bundle",
        evidence_status,
        "immutable scientific evidence bundle is missing, corrupt, mismatched, or not independently authorized",
        "immutable scientific evidence bundle lacks independent trusted authorization",
    )
    # #1116: graph/root/reviewer integrity is necessary but does not establish
    # that PASS-critical trial/holdout/causal/financial/retention facts came
    # from their canonical owning authorities. Until the accepted semantic-owner
    # composition exists, terminal PASS must remain unavailable rather than
    # promoting caller-authored EvaluationEvidence.
    check(
        "semantic_owner_evidence",
        None,
        "",
        "terminal scientific/economic owner evidence has not been reconstructed from canonical authorities",
    )
    check(
        "profile_lock",
        evidence.profile_unchanged_after_results,
        "gate profile changed after results",
        "profile immutability is unproven",
    )
    check(
        "reproducibility",
        evidence.reproducible,
        "reproducibility failed",
        "reproducibility is unproven",
    )

    if profile.require_causal_audit:
        check(
            "causality",
            evidence.causal_audit_passed,
            "causal audit failed",
            "causal audit evidence is missing",
        )
    if profile.require_financial_invariants:
        check(
            "financial_invariants",
            evidence.financial_invariants_passed,
            "financial/operational invariants failed",
            "financial/operational invariant evidence is missing",
        )
    if profile.require_complete_trials:
        check(
            "trial_completeness",
            evidence.trial_log_complete,
            "trial log is incomplete",
            "trial completeness is unknown",
        )
    if profile.require_walk_forward:
        check(
            "walk_forward",
            evidence.walk_forward_passed,
            "registered walk-forward evaluation failed",
            "walk-forward evaluation evidence is missing",
        )
    if profile.require_untouched_holdout:
        locked_evaluation_passed = (
            True
            if (
                evidence.untouched_holdout_passed is True
                or evidence.valid_sequential_evaluation_passed is True
            )
            else (
                False
                if (
                    evidence.untouched_holdout_passed is False
                    and evidence.valid_sequential_evaluation_passed is False
                )
                else None
            )
        )
        check(
            "locked_evaluation",
            locked_evaluation_passed,
            "neither untouched holdout nor valid sequential evaluation passed",
            "locked evaluation requires untouched holdout or valid sequential evidence",
        )

    if evidence.selection_correction_applied is None:
        check(
            "selection_correction",
            None,
            "",
            "selection-correction evidence is missing",
        )
    else:
        check(
            "selection_correction",
            evidence.selection_correction_applied == profile.selection_correction,
            "evaluation used a different selection correction",
            "",
        )

    if evidence.trials_attempted is None:
        check("trial_budget", None, "", "attempted trial count is missing")
    else:
        check(
            "trial_budget",
            0 < evidence.trials_attempted <= profile.max_trials,
            "registered trial budget was exceeded or no trial was recorded",
            "",
        )

    if evidence.regime_coverage is None:
        check("regime_coverage", None, "", "required regime coverage is missing")
    else:
        check(
            "regime_coverage",
            set(profile.required_regimes).issubset(evidence.regime_coverage),
            "registered regime coverage was not met",
            "",
        )

    if evidence.baseline_advantages is None:
        check("baselines", None, "", "registered baseline comparisons are missing")
    else:
        actual_baselines = set(evidence.baseline_advantages)
        registered_baselines = set(profile.baseline_ids)
        check(
            "baselines",
            actual_baselines == registered_baselines,
            "baseline comparison set differs from the registered set",
            "",
        )
        if actual_baselines == registered_baselines:
            primary_advantage = evidence.baseline_advantages[profile.primary_baseline_id]
            check(
                "primary_baseline",
                (
                    primary_advantage == evidence.net_advantage
                    if evidence.net_advantage is not None
                    else None
                ),
                "primary-baseline advantage does not match net_advantage",
                "net advantage is missing for primary-baseline binding",
            )

    if evidence.estimated_power is None:
        check("power", None, "", "power/precision evidence is missing")
    else:
        check(
            "power",
            evidence.estimated_power >= profile.min_power,
            "registered minimum power/precision was not met",
            "",
        )

    if evidence.dependence_aware_lower_bound is None:
        check(
            "statistical_rule",
            None,
            "",
            "dependence-aware uncertainty bound is missing",
        )
    else:
        check(
            "statistical_rule",
            evidence.dependence_aware_lower_bound > profile.minimum_net_advantage,
            "registered lower-bound economic rule failed",
            "",
        )

    if evidence.net_advantage is None:
        check("net_advantage", None, "", "net advantage is missing")
    else:
        check(
            "net_advantage",
            evidence.net_advantage > profile.minimum_net_advantage,
            "net advantage did not exceed the registered practical effect",
            "",
        )

    if (
        evidence.dependence_aware_lower_bound is None
        or evidence.net_advantage is None
    ):
        check(
            "uncertainty_consistency",
            None,
            "",
            "lower-bound/point-estimate consistency cannot be verified",
        )
    else:
        check(
            "uncertainty_consistency",
            evidence.dependence_aware_lower_bound <= evidence.net_advantage,
            "dependence-aware lower bound exceeds the reported net-advantage point estimate",
            "",
        )

    if evidence.drawdown is None:
        check("drawdown", None, "", "drawdown evidence is missing")
    else:
        check(
            "drawdown",
            evidence.drawdown <= profile.max_drawdown,
            "drawdown limit failed",
            "",
        )

    if evidence.adverse_cost_loss is None:
        check("cost_stress", None, "", "adverse cost stress evidence is missing")
    else:
        check(
            "cost_stress",
            evidence.adverse_cost_loss <= profile.max_adverse_cost_loss,
            "adverse cost stress exceeded the registered limit",
            "",
        )

    check(
        "retention",
        evidence.retention_passed,
        "retention gate failed",
        "retention evidence is missing",
    )

    if failures:
        status = "FAIL"
        reasons = tuple(failures + unknowns)
    elif unknowns:
        status = "INCONCLUSIVE"
        reasons = tuple(unknowns)
    else:
        status = "PASS"
        reasons = ("all registered gates passed",)
    return GateDecision(
        status=status,
        reasons=reasons,
        checks=checks,
        provenance=evidence_provenance,
    )
