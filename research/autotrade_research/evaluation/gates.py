"""Registered scientific evaluation gate foundation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping


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
        require_complete_trials: bool = True,
        require_causal_audit: bool = True,
        require_financial_invariants: bool = True,
    ) -> "GateProfile":
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("profile_id is required")
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
        return cls(
            profile_id=profile_id.strip(),
            minimum_net_advantage=_decimal(minimum_net_advantage, name="minimum_net_advantage"),
            max_drawdown=drawdown,
            max_adverse_cost_loss=_decimal(max_adverse_cost_loss, name="max_adverse_cost_loss"),
            min_power=power,
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
