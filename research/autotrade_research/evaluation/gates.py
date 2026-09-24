"""Registered scientific evaluation gate foundation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Iterable, Mapping


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

    @classmethod
    def create(cls, **kwargs) -> "EvaluationEvidence":
        converted = dict(kwargs)
        for name in ("dependence_aware_lower_bound", "estimated_power", "net_advantage", "drawdown", "adverse_cost_loss"):
            if converted.get(name) is not None:
                converted[name] = _decimal(converted[name], name=name)
        for name in (
            "profile_unchanged_after_results", "reproducible", "causal_audit_passed",
            "financial_invariants_passed", "trial_log_complete", "retention_passed",
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
            normalized_regimes = frozenset(str(value).strip() for value in coverage)
            if not normalized_regimes or any(not value for value in normalized_regimes):
                raise ValueError("regime_coverage cannot contain empty values")
            converted["regime_coverage"] = normalized_regimes
        return cls(**converted)


@dataclass(frozen=True)
class GateDecision:
    status: str
    reasons: tuple[str, ...]
    checks: Mapping[str, str]


def evaluate_gates(profile: GateProfile, evidence: EvaluationEvidence) -> GateDecision:
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
