"""Deterministic operator explanation for an evidence-bound allocation decision.

The explanation is a read-only projection of the canonical WP-32 decision and
its already content-bound evidence.  It cannot authorize trading, change risk
admission, or turn model utility into evidence of economic edge.  Missing
operator-relevant facts are explicit instead of being synthesized.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Mapping

from .allocation import (
    AllocationResult,
    EvidenceBoundObjectiveAllocationResult,
    ImmutableAllocationEvidence,
    ObjectiveAllocationResult,
    _allocation_decision_digest,
    _allocation_evidence_valid_at,
    _allocation_payload_snapshot,
)


class IncompleteDecisionExplanationError(ValueError):
    """Raised when an operator-complete explanation was required but unavailable."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    value = str.strip(value)
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(f"{name} must be an exact Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _decimal_text(value: Decimal) -> str:
    sign, digits, exponent = value.as_tuple()
    coefficient = "".join(str(digit) for digit in digits) or "0"
    if exponent >= 0:
        text = coefficient + "0" * exponent
    else:
        split = len(coefficient) + exponent
        text = (
            coefficient[:split] + "." + coefficient[split:]
            if split > 0
            else "0." + "0" * (-split) + coefficient
        )
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or set(text) <= {"0", "."}:
        return "0"
    return ("-" if sign else "") + text


def _hash(payload: dict[str, object], _dumps=json.dumps, _sha=sha256) -> str:
    return _sha(
        _dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _string_list(value: object, *, name: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{name} must be a canonical JSON list")
    normalized = tuple(_text(item, name=f"{name} item") for item in value)
    if not normalized:
        raise ValueError(f"{name} must not be empty when supplied")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must not contain duplicates")
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class DecisionFactor:
    symbol: str
    expected_return_rate: str
    risk_penalty_rate: str
    desired_notional: str
    forecast_horizon_end: str
    evidence_id: str
    evidence_digest: str


@dataclass(frozen=True)
class DecisionAlternative:
    alternative: str
    disposition: str
    reason: str


@dataclass(frozen=True)
class DecisionExplanation:
    decision_digest: str
    explanation_digest: str
    decision_kind: str
    environment: str
    decision_time: str
    policy_version: str
    base_currency: str
    why: str
    selected_symbols: tuple[str, ...]
    decisive_factors: tuple[DecisionFactor, ...]
    horizon_ends: tuple[tuple[str, str], ...]
    expected_benefit_metric: str
    expected_net_utility: Decimal
    risks: tuple[str, ...]
    alternatives_considered: tuple[DecisionAlternative, ...]
    reversal_conditions: tuple[tuple[str, tuple[str, ...]], ...]
    missing_sections: tuple[str, ...]
    coverage_status: str
    evidence_refs: tuple[tuple[str, str], ...]
    economic_edge_status: str = "NOT_ESTABLISHED"
    grants_trading_authority: bool = False


def _safe_result(
    result: EvidenceBoundObjectiveAllocationResult,
    *,
    _result_type=EvidenceBoundObjectiveAllocationResult,
    _objective_type=ObjectiveAllocationResult,
    _allocation_type=AllocationResult,
    _get=object.__getattribute__,
    _decision_digest=_allocation_decision_digest,
):
    if type(result) is not _result_type:
        raise TypeError("result must be exact EvidenceBoundObjectiveAllocationResult")
    objective = _get(result, "objective")
    if type(objective) is not _objective_type:
        raise TypeError("result objective must be exact ObjectiveAllocationResult")
    allocation = _get(objective, "allocation")
    if type(allocation) is not _allocation_type:
        raise TypeError("objective allocation must be exact AllocationResult")

    selected = _get(objective, "selected_symbols")
    if type(selected) is not tuple or any(type(item) is not str for item in selected):
        raise TypeError("selected_symbols must be an exact tuple of strings")
    if tuple(sorted(selected)) != selected or len(selected) != len(set(selected)):
        raise ValueError("selected_symbols must be unique and sorted")
    expected_utility = _decimal(
        _get(objective, "expected_net_utility"),
        name="expected_net_utility",
    )
    objective_version = _text(_get(objective, "objective_version"), name="objective_version")
    reason = _text(_get(objective, "reason"), name="objective reason")

    evidence_refs = _get(result, "evidence_refs")
    if type(evidence_refs) is not tuple:
        raise TypeError("evidence_refs must be an exact tuple")
    normalized_refs = []
    seen_ids = set()
    for pair in evidence_refs:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError("evidence_refs entries must be exact 2-tuples")
        evidence_id = _text(pair[0], name="evidence_id")
        digest = _text(pair[1], name="evidence digest")
        if evidence_id in seen_ids:
            raise ValueError("evidence_refs must not repeat evidence ids")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("evidence digest must be lowercase sha256 hex")
        seen_ids.add(evidence_id)
        normalized_refs.append((evidence_id, digest))
    normalized_refs = tuple(sorted(normalized_refs))
    if normalized_refs != evidence_refs:
        raise ValueError("evidence_refs must use canonical sorted order")

    environment = _text(_get(result, "environment"), name="environment")
    policy_version = _text(_get(result, "policy_version"), name="policy_version")
    decision_time = _text(_get(result, "decision_time"), name="decision_time")
    provider_id = _text(_get(result, "provider_id"), name="provider_id")
    account_id = _text(_get(result, "account_id"), name="account_id")
    base_currency = _text(_get(result, "base_currency"), name="base_currency").upper()
    decision_digest = _text(_get(result, "decision_digest"), name="decision_digest")

    expected_digest = _decision_digest(
        objective,
        evidence_refs=normalized_refs,
        environment=environment,
        policy_version=policy_version,
        policy_config_digest=_get(result, "policy_config_digest"),
        objective_search_config_digest=_get(result, "objective_search_config_digest"),
        decision_time=decision_time,
        provider_id=provider_id,
        account_id=account_id,
        instrument_versions=_get(result, "instrument_versions"),
        capability_snapshot_ids=_get(result, "capability_snapshot_ids"),
        account_snapshot_id=_get(result, "account_snapshot_id"),
        reconciliation_run_id=_get(result, "reconciliation_run_id"),
        account_state_version=_get(result, "account_state_version"),
        reservation_state_version=_get(result, "reservation_state_version"),
        reservation_state_digest=_get(result, "reservation_state_digest"),
        base_currency=base_currency,
    )
    if expected_digest != decision_digest:
        raise ValueError("decision_digest does not match canonical decision content")

    return {
        "objective": objective,
        "allocation": allocation,
        "selected": selected,
        "expected_utility": expected_utility,
        "objective_version": objective_version,
        "reason": reason,
        "evidence_refs": normalized_refs,
        "environment": environment,
        "policy_version": policy_version,
        "decision_time": decision_time,
        "base_currency": base_currency,
        "decision_digest": decision_digest,
    }


def _resolved_records(
    evidence_refs: tuple[tuple[str, str], ...],
    resolved_evidence: Mapping[str, ImmutableAllocationEvidence],
    *,
    environment: str,
    decision_time: str,
    _evidence_type=ImmutableAllocationEvidence,
    _get=object.__getattribute__,
    _snapshot=_allocation_payload_snapshot,
    _valid_at=_allocation_evidence_valid_at,
):
    if not isinstance(resolved_evidence, Mapping):
        raise TypeError("resolved_evidence must be a mapping")
    resolver = dict(resolved_evidence)
    records = []
    for evidence_id, digest in evidence_refs:
        item = resolver.get(evidence_id)
        if type(item) is not _evidence_type:
            raise ValueError(f"decision evidence {evidence_id} cannot be resolved")
        actual_id = _text(_get(item, "evidence_id"), name="resolved evidence_id")
        actual_digest = _text(_get(item, "digest"), name="resolved evidence digest")
        actual_environment = _text(
            _get(item, "environment"), name="resolved evidence environment"
        )
        if actual_id != evidence_id or actual_digest != digest:
            raise ValueError(f"decision evidence {evidence_id} content identity mismatch")
        if actual_environment != environment:
            raise ValueError(f"decision evidence {evidence_id} environment mismatch")
        if not _valid_at(item, decision_time):
            raise ValueError(f"decision evidence {evidence_id} is not valid at decision_time")
        payload = _snapshot(item)
        records.append(
            {
                "id": evidence_id,
                "digest": digest,
                "kind": _text(_get(item, "kind"), name="resolved evidence kind"),
                "payload": payload,
            }
        )
    return tuple(records)


def build_decision_explanation(
    result: EvidenceBoundObjectiveAllocationResult,
    resolved_evidence: Mapping[str, ImmutableAllocationEvidence],
) -> DecisionExplanation:
    """Build a deterministic, explicitly non-authorizing operator explanation."""

    decision = _safe_result(result)
    records = _resolved_records(
        decision["evidence_refs"],
        resolved_evidence,
        environment=decision["environment"],
        decision_time=decision["decision_time"],
    )

    objective_by_symbol = {}
    for record in records:
        if record["kind"] != "OBJECTIVE":
            continue
        payload = record["payload"]
        symbol = _text(payload.get("symbol"), name="OBJECTIVE payload symbol")
        if symbol in objective_by_symbol:
            raise ValueError(f"multiple OBJECTIVE evidence records for {symbol}")
        objective_by_symbol[symbol] = record

    selected = decision["selected"]
    missing_objective = tuple(symbol for symbol in selected if symbol not in objective_by_symbol)
    if missing_objective:
        raise ValueError(
            "selected symbols lack OBJECTIVE evidence: " + ", ".join(missing_objective)
        )

    factors = []
    reversals = []
    missing_reversal = []
    for symbol in sorted(objective_by_symbol):
        record = objective_by_symbol[symbol]
        payload = record["payload"]
        if symbol in selected:
            factor = DecisionFactor(
                symbol=symbol,
                expected_return_rate=_text(
                    payload.get("expected_return_rate"),
                    name=f"{symbol} expected_return_rate",
                ),
                risk_penalty_rate=_text(
                    payload.get("risk_penalty_rate"),
                    name=f"{symbol} risk_penalty_rate",
                ),
                desired_notional=_text(
                    payload.get("desired_notional"),
                    name=f"{symbol} desired_notional",
                ),
                forecast_horizon_end=_text(
                    payload.get("forecast_horizon_end"),
                    name=f"{symbol} forecast_horizon_end",
                ),
                evidence_id=record["id"],
                evidence_digest=record["digest"],
            )
            factors.append(factor)

        raw_reversals = payload.get("reversal_conditions")
        if raw_reversals is None:
            relevant = symbol in selected or not selected
            if relevant:
                missing_reversal.append(symbol)
        else:
            reversals.append(
                (
                    symbol,
                    _string_list(
                        raw_reversals,
                        name=f"{symbol} reversal_conditions",
                    ),
                )
            )

    considered = tuple(sorted(objective_by_symbol))
    alternatives = [
        DecisionAlternative(
            alternative="NO_TRADE_BASELINE",
            disposition="NOT_SELECTED" if selected else "SELECTED",
            reason=(
                "canonical objective search selected a trading/de-risk subset over the "
                "admissible no-trade baseline"
                if selected
                else "canonical objective search preserved the no-trade/de-risk baseline"
            ),
        )
    ]
    for symbol in considered:
        if symbol not in selected:
            alternatives.append(
                DecisionAlternative(
                    alternative=f"SYMBOL:{symbol}",
                    disposition="NOT_SELECTED",
                    reason=(
                        "candidate was present in decision-time OBJECTIVE evidence but was "
                        "not selected; the canonical decision artifact does not preserve a "
                        "candidate-specific rejection reason"
                    ),
                )
            )

    allocation = decision["allocation"]
    worst_stress = object.__getattribute__(allocation, "worst_stress_loss")
    risks = [
        "portfolio_worst_stress_loss="
        + ("UNKNOWN" if worst_stress is None else _decimal_text(_decimal(worst_stress, name="worst_stress_loss"))),
        "portfolio_estimated_cost="
        + _decimal_text(
            _decimal(object.__getattribute__(allocation, "estimated_cost"), name="estimated_cost")
        ),
    ]
    for factor in factors:
        risks.append(
            f"{factor.symbol}:risk_penalty_rate={factor.risk_penalty_rate}"
        )

    missing_sections = []
    required_reversal_symbols = tuple(sorted(selected if selected else considered))
    reversal_by_symbol = {symbol: conditions for symbol, conditions in reversals}
    if any(symbol not in reversal_by_symbol for symbol in required_reversal_symbols):
        missing_sections.append("reversal_conditions")
    coverage = "COMPLETE" if not missing_sections else "PARTIAL"

    horizon_ends = tuple(
        (factor.symbol, factor.forecast_horizon_end) for factor in factors
    )
    digest_payload = {
        "schema_version": "decision-explanation.v1",
        "decision_digest": decision["decision_digest"],
        "decision_kind": "TRADE_OR_DE_RISK_PROPOSAL" if selected else "NO_TRADE_OR_FALLBACK",
        "environment": decision["environment"],
        "decision_time": decision["decision_time"],
        "policy_version": decision["policy_version"],
        "base_currency": decision["base_currency"],
        "why": decision["reason"],
        "selected_symbols": list(selected),
        "decisive_factors": [factor.__dict__ for factor in factors],
        "horizon_ends": [list(item) for item in horizon_ends],
        "expected_benefit_metric": "MODEL_EXPECTED_NET_UTILITY_NOT_EDGE_PROOF",
        "expected_net_utility": _decimal_text(decision["expected_utility"]),
        "risks": sorted(risks),
        "alternatives_considered": [item.__dict__ for item in alternatives],
        "reversal_conditions": [
            [symbol, list(conditions)] for symbol, conditions in sorted(reversals)
        ],
        "missing_sections": missing_sections,
        "coverage_status": coverage,
        "evidence_refs": [list(item) for item in decision["evidence_refs"]],
        "economic_edge_status": "NOT_ESTABLISHED",
        "grants_trading_authority": False,
    }
    explanation_digest = _hash(digest_payload)
    return DecisionExplanation(
        decision_digest=decision["decision_digest"],
        explanation_digest=explanation_digest,
        decision_kind=digest_payload["decision_kind"],
        environment=decision["environment"],
        decision_time=decision["decision_time"],
        policy_version=decision["policy_version"],
        base_currency=decision["base_currency"],
        why=decision["reason"],
        selected_symbols=selected,
        decisive_factors=tuple(factors),
        horizon_ends=horizon_ends,
        expected_benefit_metric="MODEL_EXPECTED_NET_UTILITY_NOT_EDGE_PROOF",
        expected_net_utility=decision["expected_utility"],
        risks=tuple(sorted(risks)),
        alternatives_considered=tuple(alternatives),
        reversal_conditions=tuple(sorted(reversals)),
        missing_sections=tuple(missing_sections),
        coverage_status=coverage,
        evidence_refs=decision["evidence_refs"],
    )


def require_complete_decision_explanation(
    result: EvidenceBoundObjectiveAllocationResult,
    resolved_evidence: Mapping[str, ImmutableAllocationEvidence],
) -> DecisionExplanation:
    """Fail closed if the evidence-bound decision cannot explain every §29 field."""

    explanation = build_decision_explanation(result, resolved_evidence)
    if explanation.coverage_status != "COMPLETE":
        raise IncompleteDecisionExplanationError(
            "decision explanation is incomplete: " + ", ".join(explanation.missing_sections)
        )
    return explanation


__all__ = [
    "DecisionAlternative",
    "DecisionExplanation",
    "DecisionFactor",
    "IncompleteDecisionExplanationError",
    "build_decision_explanation",
    "require_complete_decision_explanation",
]
