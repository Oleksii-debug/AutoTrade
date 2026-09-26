"""Registered scientific evaluation gate foundation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from types import MappingProxyType
from typing import Iterable, Mapping
from uuid import UUID

from ..artifacts.store import ArtifactIntegrityError, ArtifactStore


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result




_REPORT_EVIDENCE_KINDS = frozenset({
    "profile",
    "trial_log",
    "causal_audit",
    "financial_invariants",
    "retention",
    "locked_evaluation",
    "metrics",
    "independent_review",
})


_REQUIRED_EVIDENCE_KINDS = frozenset({
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
})


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
            "none", "no", "unadjusted", "disabled", "n/a", "na"
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
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("profile_id is required")
        practical_advantage = _decimal(
            minimum_net_advantage,
            name="minimum_net_advantage",
        )
        if practical_advantage < 0:
            raise ValueError("minimum_net_advantage must be non-negative")
        drawdown = _decimal(max_drawdown, name="max_drawdown")
        power = _decimal(min_power, name="min_power")
        if drawdown < 0 or drawdown > 1:
            raise ValueError("max_drawdown must be between zero and one")
        if power <= 0 or power > 1:
            raise ValueError("min_power must be in (0, 1]")
        for name, value in (
            ("require_complete_trials", require_complete_trials),
            ("require_causal_audit", require_causal_audit),
            ("require_financial_invariants", require_financial_invariants),
            ("require_untouched_holdout", require_untouched_holdout),
            ("require_walk_forward", require_walk_forward),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean")
        adverse_cost_limit = _decimal(max_adverse_cost_loss, name="max_adverse_cost_loss")
        if adverse_cost_limit < 0:
            raise ValueError("max_adverse_cost_loss must be non-negative")

        if not isinstance(primary_baseline_id, str) or not primary_baseline_id.strip():
            raise ValueError("primary_baseline_id is required")
        primary_baseline = primary_baseline_id.strip()
        if isinstance(baseline_ids, (str, bytes)):
            raise TypeError("baseline_ids must be a collection")
        materialized_baselines = tuple(baseline_ids)
        if any(not isinstance(value, str) for value in materialized_baselines):
            raise TypeError("baseline_ids must contain text values")
        baselines = tuple(value.strip() for value in materialized_baselines)
        if not baselines or any(not value for value in baselines):
            raise ValueError("baseline_ids must be non-empty")
        if len(set(baselines)) != len(baselines):
            raise ValueError("baseline_ids must be unique")
        if primary_baseline not in baselines:
            raise ValueError("primary_baseline_id must be registered in baseline_ids")
        if not isinstance(selection_correction, str) or not selection_correction.strip():
            raise ValueError("selection_correction is required")
        correction = selection_correction.strip()
        if type(max_trials) is not int or max_trials <= 0:
            raise ValueError("max_trials must be a positive integer")
        if max_trials > 1 and correction.lower() in {
            "none", "no", "unadjusted", "disabled", "n/a", "na"
        }:
            raise ValueError(
                "multi-trial protocols require an explicit multiplicity treatment"
            )
        if isinstance(required_regimes, (str, bytes)):
            raise TypeError("required_regimes must be a collection")
        materialized_regimes = tuple(required_regimes)
        if any(not isinstance(value, str) for value in materialized_regimes):
            raise TypeError("required_regimes must contain text values")
        regimes = tuple(value.strip() for value in materialized_regimes)
        if not regimes or any(not value for value in regimes):
            raise ValueError("required_regimes must be non-empty")
        if len(set(regimes)) != len(regimes):
            raise ValueError("required_regimes must be unique")

        return cls(
            profile_id=profile_id.strip(),
            minimum_net_advantage=practical_advantage,
            max_drawdown=drawdown,
            max_adverse_cost_loss=adverse_cost_limit,
            min_power=power,
            primary_baseline_id=primary_baseline,
            baseline_ids=baselines,
            selection_correction=correction,
            max_trials=max_trials,
            required_regimes=regimes,
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
                if not isinstance(raw_ref, GateEvidenceRef):
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
        converted = dict(kwargs)
        for name in ("dependence_aware_lower_bound", "estimated_power", "net_advantage", "drawdown", "adverse_cost_loss"):
            if converted.get(name) is not None:
                converted[name] = _decimal(converted[name], name=name)
        for name in (
            "profile_unchanged_after_results", "reproducible", "causal_audit_passed",
            "financial_invariants_passed", "trial_log_complete", "retention_passed",
            "untouched_holdout_passed", "valid_sequential_evaluation_passed",
            "walk_forward_passed",
        ):
            value = converted.get(name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean or None")
        power = converted.get("estimated_power")
        if power is not None and (power < 0 or power > 1):
            raise ValueError("estimated_power must be between zero and one")
        drawdown = converted.get("drawdown")
        if drawdown is not None and drawdown < 0:
            raise ValueError("drawdown must be non-negative")
        adverse_cost_loss = converted.get("adverse_cost_loss")
        if adverse_cost_loss is not None and adverse_cost_loss < 0:
            raise ValueError("adverse_cost_loss must be non-negative")

        baseline_advantages = converted.get("baseline_advantages")
        if baseline_advantages is not None:
            if not isinstance(baseline_advantages, Mapping):
                raise TypeError("baseline_advantages must be a mapping or None")
            normalized_baselines: dict[str, Decimal] = {}
            for key, value in baseline_advantages.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("baseline advantage id is required")
                normalized = key.strip()
                if normalized in normalized_baselines:
                    raise ValueError("duplicate normalized baseline advantage id")
                normalized_baselines[normalized] = _decimal(
                    value, name=f"baseline_advantage[{normalized}]"
                )
            converted["baseline_advantages"] = MappingProxyType(normalized_baselines)

        correction = converted.get("selection_correction_applied")
        if correction is not None:
            if not isinstance(correction, str) or not correction.strip():
                raise ValueError("selection_correction_applied must be text or None")
            converted["selection_correction_applied"] = correction.strip()

        trials = converted.get("trials_attempted")
        if trials is not None and (type(trials) is not int or trials < 0):
            raise ValueError("trials_attempted must be a non-negative integer or None")

        coverage = converted.get("regime_coverage")
        if coverage is not None:
            if isinstance(coverage, (str, bytes)):
                raise TypeError("regime_coverage must be a collection or None")
            coverage_values = tuple(coverage)
            if any(not isinstance(value, str) for value in coverage_values):
                raise TypeError("regime_coverage must contain text values")
            normalized_regimes = frozenset(value.strip() for value in coverage_values)
            if not normalized_regimes or any(not value for value in normalized_regimes):
                raise ValueError("regime_coverage cannot contain empty values")
            converted["regime_coverage"] = normalized_regimes
        return cls(**converted)


@dataclass(frozen=True)
class GateDecision:
    status: str
    reasons: tuple[str, ...]
    checks: Mapping[str, str]

    def __post_init__(self) -> None:
        status = self.status.strip() if isinstance(self.status, str) else ""
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ValueError("GateDecision status must be PASS, FAIL or INCONCLUSIVE")
        if isinstance(self.reasons, (str, bytes)):
            raise TypeError("GateDecision reasons must be a collection")
        reasons = tuple(self.reasons)
        if not reasons or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in reasons
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
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reasons", tuple(reason.strip() for reason in reasons))
        object.__setattr__(self, "checks", MappingProxyType(frozen_checks))



def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def gate_report_payload(
    kind: str,
    profile: GateProfile,
    evidence: EvaluationEvidence,
) -> bytes:
    """Canonical semantic payload for PASS-critical scientific report artifacts."""

    if kind not in _REPORT_EVIDENCE_KINDS:
        raise ValueError("kind is not a scientific report evidence kind")
    if not isinstance(profile, GateProfile):
        raise TypeError("profile must be GateProfile")
    if not isinstance(evidence, EvaluationEvidence):
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
    elif kind == "metrics":
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
    else:
        value = {
            "profile_id": profile_id,
            "profile_unchanged_after_results": (
                evidence.profile_unchanged_after_results
            ),
            "status": "PASS",
        }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _verify_evidence_bundle(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    artifact_store: ArtifactStore | None,
) -> bool | None:
    """Verify the immutable G0/G1/G2/G3/G4 evidence references.

    Missing verifier context is INCONCLUSIVE. Once a bundle is supplied,
    missing/corrupt/mismatched provenance is a hard failure rather than a
    caller-asserted PASS.
    """

    if artifact_store is None or evidence.evidence_refs is None:
        return None
    refs = evidence.evidence_refs
    if set(refs) != _REQUIRED_EVIDENCE_KINDS:
        return False
    for kind in sorted(_REQUIRED_EVIDENCE_KINDS):
        ref = refs[kind]
        try:
            manifest = artifact_store.load_manifest(ref.artifact_id)
            payload = artifact_store.read_bytes(ref.artifact_id)
        except (FileNotFoundError, ArtifactIntegrityError, ValueError, OSError):
            return False
        if manifest.get("manifest_hash") is None:
            return False
        if manifest.get("sha256") != ref.sha256:
            return False
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            return False
        if metadata.get("evidence_kind") != kind:
            return False
        if metadata.get("profile_id") != profile.profile_id:
            return False
        rights = manifest.get("rights")
        if not isinstance(rights, dict) or rights.get("storage") is not True:
            return False
        if kind in _REPORT_EVIDENCE_KINDS:
            if manifest.get("media_type") != "application/json":
                return False
            if payload != gate_report_payload(kind, profile, evidence):
                return False
    return True


def evaluate_gates(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    *,
    artifact_store: ArtifactStore | None = None,
) -> GateDecision:
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
    check(
        "evidence_bundle",
        _verify_evidence_bundle(profile, evidence, artifact_store),
        "immutable scientific evidence bundle is missing, corrupt, or mismatched",
        "immutable scientific evidence bundle has not been verified",
    )
    check(
        "profile_lock",
        evidence.profile_unchanged_after_results,
        "gate profile changed after results",
        "profile immutability is unproven",
    )
    check("reproducibility", evidence.reproducible, "reproducibility failed", "reproducibility is unproven")

    if profile.require_causal_audit:
        check("causality", evidence.causal_audit_passed, "causal audit failed", "causal audit evidence is missing")
    if profile.require_financial_invariants:
        check(
            "financial_invariants",
            evidence.financial_invariants_passed,
            "financial/operational invariants failed",
            "financial/operational invariant evidence is missing",
        )
    if profile.require_complete_trials:
        check("trial_completeness", evidence.trial_log_complete, "trial log is incomplete", "trial completeness is unknown")
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
                primary_advantage == evidence.net_advantage
                if evidence.net_advantage is not None
                else None,
                "primary-baseline advantage does not match net_advantage",
                "net advantage is missing for primary-baseline binding",
            )

    if evidence.estimated_power is None:
        check("power", None, "", "power/precision evidence is missing")
    else:
        check("power", evidence.estimated_power >= profile.min_power,
              "registered minimum power/precision was not met", "")

    if evidence.dependence_aware_lower_bound is None:
        check("statistical_rule", None, "", "dependence-aware uncertainty bound is missing")
    else:
        check("statistical_rule",
              evidence.dependence_aware_lower_bound > profile.minimum_net_advantage,
              "registered lower-bound economic rule failed", "")

    if evidence.net_advantage is None:
        check("net_advantage", None, "", "net advantage is missing")
    else:
        check("net_advantage", evidence.net_advantage > profile.minimum_net_advantage,
              "net advantage did not exceed the registered practical effect", "")

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
        check("drawdown", evidence.drawdown <= profile.max_drawdown, "drawdown limit failed", "")

    if evidence.adverse_cost_loss is None:
        check("cost_stress", None, "", "adverse cost stress evidence is missing")
    else:
        check("cost_stress",
              evidence.adverse_cost_loss <= profile.max_adverse_cost_loss,
              "adverse cost stress exceeded the registered limit", "")

    check("retention", evidence.retention_passed, "retention gate failed", "retention evidence is missing")

    if failures:
        status = "FAIL"
        reasons = tuple(failures + unknowns)
    elif unknowns:
        status = "INCONCLUSIVE"
        reasons = tuple(unknowns)
    else:
        status = "PASS"
        reasons = ("all registered gates passed",)
    return GateDecision(status=status, reasons=reasons, checks=checks)
