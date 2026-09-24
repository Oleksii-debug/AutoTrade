"""Fail-closed bounded online-learning envelope.

The module does not train a model and cannot change trading authority.  It decides
whether an already-computed parameter update stays inside a pre-authorized online
learning envelope.  Crossing a mutable-parameter boundary produces
CANDIDATE_REQUIRED; operational/risk/drift/resource violations STOP updates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Mapping


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


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


def _non_negative(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha256_identity(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    if not value.startswith("sha256:"):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    digest = value[7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return value


@dataclass(frozen=True)
class ParameterRule:
    name: str
    minimum: Decimal
    maximum: Decimal
    max_absolute_step: Decimal

    @classmethod
    def create(cls, *, name: str, minimum, maximum, max_absolute_step) -> "ParameterRule":
        low = _decimal(minimum, name="minimum")
        high = _decimal(maximum, name="maximum")
        step = _non_negative(max_absolute_step, name="max_absolute_step")
        if low > high:
            raise ValueError("minimum must not exceed maximum")
        return cls(
            name=_text(name, name="parameter name"),
            minimum=low,
            maximum=high,
            max_absolute_step=step,
        )


@dataclass(frozen=True)
class OnlineUpdateEnvelope:
    envelope_id: str
    champion_artifact_hash: str
    parameter_rules: tuple[ParameterRule, ...]
    eligible_label_versions: tuple[str, ...]
    min_seconds_between_updates: int
    max_updates_per_window: int
    max_compute_units_per_update: Decimal
    max_drift_score: Decimal

    @classmethod
    def create(
        cls,
        *,
        envelope_id: str,
        champion_artifact_hash: str,
        parameter_rules,
        eligible_label_versions,
        min_seconds_between_updates: int,
        max_updates_per_window: int,
        max_compute_units_per_update,
        max_drift_score,
    ) -> "OnlineUpdateEnvelope":
        rules = tuple(parameter_rules)
        if not rules:
            raise ValueError("at least one parameter rule is required")
        names = [rule.name for rule in rules]
        if len(names) != len(set(names)):
            raise ValueError("parameter rules must have unique names")
        labels = tuple(_text(item, name="label version") for item in eligible_label_versions)
        if not labels:
            raise ValueError("at least one eligible label version is required")
        if len(labels) != len(set(labels)):
            raise ValueError("eligible label versions must be unique")
        for name, value, minimum in (
            ("min_seconds_between_updates", min_seconds_between_updates, 0),
            ("max_updates_per_window", max_updates_per_window, 1),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} is invalid")
        return cls(
            envelope_id=_text(envelope_id, name="envelope_id"),
            champion_artifact_hash=_sha256_identity(champion_artifact_hash, name="champion_artifact_hash"),
            parameter_rules=rules,
            eligible_label_versions=labels,
            min_seconds_between_updates=min_seconds_between_updates,
            max_updates_per_window=max_updates_per_window,
            max_compute_units_per_update=_non_negative(
                max_compute_units_per_update, name="max_compute_units_per_update"
            ),
            max_drift_score=_non_negative(max_drift_score, name="max_drift_score"),
        )


@dataclass(frozen=True)
class OnlineUpdateInput:
    current_parameters: Mapping[str, Decimal]
    proposed_parameters: Mapping[str, Decimal]
    label_version: str
    observed_at: datetime
    last_update_at: datetime | None
    updates_in_window: int
    reserved_compute_units: Decimal
    drift_score: Decimal
    operational_invariant_failed: bool = False
    risk_envelope_violated: bool = False

    @classmethod
    def create(
        cls,
        *,
        current_parameters,
        proposed_parameters,
        label_version: str,
        observed_at: datetime,
        last_update_at: datetime | None,
        updates_in_window: int,
        reserved_compute_units,
        drift_score,
        operational_invariant_failed: bool = False,
        risk_envelope_violated: bool = False,
    ) -> "OnlineUpdateInput":
        if not isinstance(updates_in_window, int) or isinstance(updates_in_window, bool) or updates_in_window < 0:
            raise ValueError("updates_in_window must be a non-negative integer")
        for name, value in (
            ("operational_invariant_failed", operational_invariant_failed),
            ("risk_envelope_violated", risk_envelope_violated),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean")
        current = {
            _text(key, name="current parameter name"): _decimal(value, name=f"current[{key}]")
            for key, value in current_parameters.items()
        }
        proposed = {
            _text(key, name="proposed parameter name"): _decimal(value, name=f"proposed[{key}]")
            for key, value in proposed_parameters.items()
        }
        observed = _utc(observed_at, name="observed_at")
        previous = None if last_update_at is None else _utc(last_update_at, name="last_update_at")
        if previous is not None and previous > observed:
            raise ValueError("last_update_at cannot be after observed_at")
        return cls(
            current_parameters=current,
            proposed_parameters=proposed,
            label_version=_text(label_version, name="label_version"),
            observed_at=observed,
            last_update_at=previous,
            updates_in_window=updates_in_window,
            reserved_compute_units=_non_negative(reserved_compute_units, name="reserved_compute_units"),
            drift_score=_non_negative(drift_score, name="drift_score"),
            operational_invariant_failed=operational_invariant_failed,
            risk_envelope_violated=risk_envelope_violated,
        )


@dataclass(frozen=True)
class OnlineUpdateDecision:
    decision_id: str
    status: str
    reasons: tuple[str, ...]
    parameter_changes: tuple[tuple[str, Decimal, Decimal], ...]
    grants_trading_authority: bool = False


def evaluate_online_update(
    envelope: OnlineUpdateEnvelope,
    update: OnlineUpdateInput,
) -> OnlineUpdateDecision:
    """Return ALLOW, CANDIDATE_REQUIRED, or STOP without mutating model state."""
    reasons: list[str] = []
    candidate_reasons: list[str] = []
    rules = {rule.name: rule for rule in envelope.parameter_rules}

    if update.operational_invariant_failed:
        reasons.append("LEARNING.OPERATIONAL_INVARIANT_FAILED")
    if update.risk_envelope_violated:
        reasons.append("LEARNING.RISK_ENVELOPE_VIOLATED")
    if update.drift_score > envelope.max_drift_score:
        reasons.append("LEARNING.DRIFT_LIMIT_EXCEEDED")
    if update.reserved_compute_units > envelope.max_compute_units_per_update:
        reasons.append("LEARNING.COMPUTE_BUDGET_EXCEEDED")
    if update.updates_in_window >= envelope.max_updates_per_window:
        reasons.append("LEARNING.UPDATE_FREQUENCY_EXCEEDED")
    if update.last_update_at is not None:
        elapsed = (update.observed_at - update.last_update_at).total_seconds()
        if elapsed < envelope.min_seconds_between_updates:
            reasons.append("LEARNING.UPDATE_INTERVAL_TOO_SHORT")
    if update.label_version not in envelope.eligible_label_versions:
        candidate_reasons.append("LEARNING.LABEL_OUTSIDE_ENVELOPE")

    current_names = set(update.current_parameters)
    proposed_names = set(update.proposed_parameters)
    if current_names != proposed_names:
        candidate_reasons.append("LEARNING.PARAMETER_SET_CHANGED")
    if proposed_names - set(rules):
        candidate_reasons.append("LEARNING.UNAUTHORIZED_PARAMETER")

    changes: list[tuple[str, Decimal, Decimal]] = []
    for name in sorted(current_names & proposed_names):
        current = update.current_parameters[name]
        proposed = update.proposed_parameters[name]
        changes.append((name, current, proposed))
        rule = rules.get(name)
        if rule is None:
            continue
        if proposed < rule.minimum or proposed > rule.maximum:
            candidate_reasons.append(f"LEARNING.PARAMETER_RANGE_EXCEEDED:{name}")
        if abs(proposed - current) > rule.max_absolute_step:
            candidate_reasons.append(f"LEARNING.PARAMETER_STEP_EXCEEDED:{name}")

    if reasons:
        status = "STOP"
        final_reasons = tuple(dict.fromkeys(reasons + candidate_reasons))
    elif candidate_reasons:
        status = "CANDIDATE_REQUIRED"
        final_reasons = tuple(dict.fromkeys(candidate_reasons))
    else:
        status = "ALLOW"
        final_reasons = ()

    canonical = json.dumps(
        {
            "envelope_id": envelope.envelope_id,
            "champion_artifact_hash": envelope.champion_artifact_hash,
            "parameter_rules": [
                {
                    "name": rule.name,
                    "minimum": str(rule.minimum),
                    "maximum": str(rule.maximum),
                    "max_absolute_step": str(rule.max_absolute_step),
                }
                for rule in sorted(envelope.parameter_rules, key=lambda item: item.name)
            ],
            "eligible_label_versions": sorted(envelope.eligible_label_versions),
            "min_seconds_between_updates": envelope.min_seconds_between_updates,
            "max_updates_per_window": envelope.max_updates_per_window,
            "max_compute_units_per_update": str(envelope.max_compute_units_per_update),
            "max_drift_score": str(envelope.max_drift_score),
            "label_version": update.label_version,
            "observed_at": update.observed_at.isoformat(),
            "last_update_at": None if update.last_update_at is None else update.last_update_at.isoformat(),
            "updates_in_window": update.updates_in_window,
            "reserved_compute_units": str(update.reserved_compute_units),
            "drift_score": str(update.drift_score),
            "changes": [(name, str(old), str(new)) for name, old, new in changes],
            "status": status,
            "reasons": final_reasons,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return OnlineUpdateDecision(
        decision_id="online-update-" + sha256(canonical.encode("utf-8")).hexdigest()[:32],
        status=status,
        reasons=final_reasons,
        parameter_changes=tuple(changes),
    )
