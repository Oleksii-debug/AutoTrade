"""Fail-closed correlation concentration guard for canonical portfolio allocations.

This module is deliberately a composition layer over ``allocation.py``. It does
not select orders, send provider requests, replace hard risk, or claim an
economic edge. It answers one narrower Product Specification section 25
question: can several individually acceptable positions reinforce the same
underlying risk strongly enough that their combined gross exposure breaches a
portfolio concentration limit?

The guard uses decision-time pairwise correlation evidence only as a conservative
clustering signal. It does not interpret correlation as causation or as a
profitability estimate. Missing, future, or expired pair evidence is
INCONCLUSIVE and therefore cannot authorize use of the allocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from itertools import combinations
import json

from .allocation import AllocationResult, AllocationTarget
from .exact_decimal import (
    ExactDecimalError,
    exact_abs,
    exact_add,
    exact_multiply,
    parse_bounded_exact_decimal,
)


class CorrelationConcentrationError(ValueError):
    """Raised when a portfolio allocation lacks correlation-safe evidence."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{name} must be a non-empty exact str")
    return value.strip()


def _decimal(value: object, *, name: str) -> Decimal:
    if type(value) not in (Decimal, str, int) or type(value) is bool:
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a bounded exact decimal") from error


def _decimal_text(value: Decimal) -> str:
    """Context-independent plain decimal identity for bounded finite values."""
    sign, digits, exponent = value.as_tuple()
    coefficient = "".join(str(digit) for digit in digits) or "0"
    if exponent >= 0:
        text = coefficient + ("0" * exponent)
    else:
        split = len(coefficient) + exponent
        if split > 0:
            text = coefficient[:split] + "." + coefficient[split:]
        else:
            text = "0." + ("0" * (-split)) + coefficient
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or set(text) <= {"0", "."}:
        return "0"
    return ("-" if sign else "") + text


def _utc_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class CorrelationEvidence:
    """One symmetric decision-time pairwise correlation observation.

    ``correlation`` is descriptive evidence in [-1, 1]. The source reference
    identifies the external/statistical evidence artifact; this value object
    does not authenticate that source by itself.
    """

    left_symbol: str
    right_symbol: str
    correlation: Decimal
    observed_at: str
    valid_until: str
    source_ref: str

    def __post_init__(self) -> None:
        left = _text(self.left_symbol, name="left_symbol")
        right = _text(self.right_symbol, name="right_symbol")
        if left == right:
            raise ValueError("correlation evidence requires two distinct symbols")
        left, right = sorted((left, right))
        correlation = _decimal(self.correlation, name="correlation")
        if correlation < Decimal("-1") or correlation > Decimal("1"):
            raise ValueError("correlation must be between -1 and 1")
        observed = _utc_text(self.observed_at, name="observed_at")
        valid_until = _utc_text(self.valid_until, name="valid_until")
        if _utc_instant(valid_until) < _utc_instant(observed):
            raise ValueError("valid_until must not precede observed_at")
        source = _text(self.source_ref, name="source_ref")
        object.__setattr__(self, "left_symbol", left)
        object.__setattr__(self, "right_symbol", right)
        object.__setattr__(self, "correlation", correlation)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "source_ref", source)

    @property
    def pair(self) -> tuple[str, str]:
        return (self.left_symbol, self.right_symbol)


@dataclass(frozen=True)
class CorrelationConcentrationPolicy:
    """Conservative cap for strongly reinforcing portfolio exposures."""

    max_correlated_gross_notional: Decimal
    reinforcing_threshold: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        maximum = _decimal(
            self.max_correlated_gross_notional,
            name="max_correlated_gross_notional",
        )
        if maximum <= 0:
            raise ValueError("max_correlated_gross_notional must be positive")
        threshold = _decimal(self.reinforcing_threshold, name="reinforcing_threshold")
        if threshold < 0 or threshold > 1:
            raise ValueError("reinforcing_threshold must be between 0 and 1")
        object.__setattr__(self, "max_correlated_gross_notional", maximum)
        object.__setattr__(self, "reinforcing_threshold", threshold)


@dataclass(frozen=True)
class CorrelationComponent:
    symbols: tuple[str, ...]
    gross_notional: Decimal


@dataclass(frozen=True)
class CorrelationConcentrationAssessment:
    status: str
    components: tuple[CorrelationComponent, ...]
    missing_pairs: tuple[tuple[str, str], ...]
    stale_pairs: tuple[tuple[str, str], ...]
    assessment_digest: str
    reason: str
    economic_edge_status: str = "NOT_ESTABLISHED"
    grants_trading_authority: bool = False


def _canonical_allocation_exposures(allocation: AllocationResult) -> dict[str, Decimal]:
    if type(allocation) is not AllocationResult:
        raise TypeError("allocation must be exact AllocationResult")
    if type(allocation.status) is not str or allocation.status != "ALLOCATED":
        raise ValueError("correlation guard requires an allocated portfolio")
    if type(allocation.targets) is not tuple:
        raise TypeError("allocation targets must be an exact tuple")
    exposures: dict[str, Decimal] = {}
    for target in allocation.targets:
        if type(target) is not AllocationTarget:
            raise TypeError("allocation targets must contain exact AllocationTarget values")
        symbol = _text(target.symbol, name="allocation target symbol")
        if symbol in exposures:
            raise ValueError("allocation target symbols must be unique")
        quantity = _decimal(target.quantity, name=f"allocation quantity {symbol}")
        notional = _decimal(target.notional, name=f"allocation notional {symbol}")
        if (
            (quantity == 0) != (notional == 0)
            or (quantity > 0 and notional < 0)
            or (quantity < 0 and notional > 0)
        ):
            raise ValueError("allocation quantity and notional direction disagree")
        exposures[symbol] = notional
    gross = Decimal("0")
    signed_net = Decimal("0")
    for notional in exposures.values():
        gross = exact_add(gross, exact_abs(notional))
        signed_net = exact_add(signed_net, notional)
    if (
        _decimal(allocation.gross_notional, name="allocation gross_notional") != gross
        or _decimal(allocation.net_notional, name="allocation net_notional")
        != exact_abs(signed_net)
    ):
        raise ValueError("allocation aggregate notionals disagree with targets")
    return exposures


def _canonical_evidence(
    evidence: tuple[CorrelationEvidence, ...] | list[CorrelationEvidence],
    *,
    allowed_symbols: frozenset[str],
) -> dict[tuple[str, str], CorrelationEvidence]:
    if type(evidence) not in (tuple, list):
        raise TypeError("correlation evidence must be an exact tuple or list")
    result: dict[tuple[str, str], CorrelationEvidence] = {}
    for item in evidence:
        if type(item) is not CorrelationEvidence:
            raise TypeError("correlation evidence entries must be exact CorrelationEvidence")
        # Reconstruct at use time so object.__setattr__ mutation of a frozen
        # instance cannot bypass the value boundary.
        canonical = CorrelationEvidence(
            left_symbol=item.left_symbol,
            right_symbol=item.right_symbol,
            correlation=item.correlation,
            observed_at=item.observed_at,
            valid_until=item.valid_until,
            source_ref=item.source_ref,
        )
        if canonical.left_symbol not in allowed_symbols or canonical.right_symbol not in allowed_symbols:
            raise ValueError("correlation evidence references a symbol outside the allocation")
        if canonical.pair in result:
            raise ValueError("duplicate correlation evidence pair")
        result[canonical.pair] = canonical
    return result


def _assessment_digest(
    *,
    exposures: dict[str, Decimal],
    evidence: dict[tuple[str, str], CorrelationEvidence],
    policy: CorrelationConcentrationPolicy,
    decision_time: str,
) -> str:
    payload = {
        "schema_version": "portfolio-correlation-concentration.v1",
        "decision_time": decision_time,
        "policy": {
            "max_correlated_gross_notional": _decimal_text(policy.max_correlated_gross_notional),
            "reinforcing_threshold": _decimal_text(policy.reinforcing_threshold),
        },
        "exposures": [
            {"symbol": symbol, "notional": _decimal_text(exposures[symbol])}
            for symbol in sorted(exposures)
        ],
        "evidence": [
            {
                "left_symbol": item.left_symbol,
                "right_symbol": item.right_symbol,
                "correlation": _decimal_text(item.correlation),
                "observed_at": item.observed_at,
                "valid_until": item.valid_until,
                "source_ref": item.source_ref,
            }
            for _pair, item in sorted(evidence.items())
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _components(
    exposures: dict[str, Decimal],
    evidence: dict[tuple[str, str], CorrelationEvidence],
    threshold: Decimal,
) -> tuple[CorrelationComponent, ...]:
    active = sorted(symbol for symbol, notional in exposures.items() if notional != 0)
    parent = {symbol: symbol for symbol in active}

    def find(symbol: str) -> str:
        while parent[symbol] != symbol:
            parent[symbol] = parent[parent[symbol]]
            symbol = parent[symbol]
        return symbol

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for left, right in combinations(active, 2):
        item = evidence[(left, right)]
        left_sign = Decimal("1") if exposures[left] > 0 else Decimal("-1")
        right_sign = Decimal("1") if exposures[right] > 0 else Decimal("-1")
        reinforcement = exact_multiply(item.correlation, left_sign, right_sign)
        if reinforcement >= threshold:
            union(left, right)

    grouped: dict[str, list[str]] = {}
    for symbol in active:
        grouped.setdefault(find(symbol), []).append(symbol)

    components: list[CorrelationComponent] = []
    for symbols in grouped.values():
        gross = Decimal("0")
        for symbol in sorted(symbols):
            gross = exact_add(gross, exact_abs(exposures[symbol]))
        components.append(
            CorrelationComponent(symbols=tuple(sorted(symbols)), gross_notional=gross)
        )
    return tuple(sorted(components, key=lambda component: component.symbols))


def assess_correlation_concentration(
    allocation: AllocationResult,
    evidence: tuple[CorrelationEvidence, ...] | list[CorrelationEvidence],
    policy: CorrelationConcentrationPolicy,
    *,
    decision_time: str,
) -> CorrelationConcentrationAssessment:
    """Assess joint concentration without upgrading any trading authority.

    Pair coverage is complete and fail-closed for every currently non-zero
    allocation target. A positive correlation reinforces same-direction
    exposures; a negative correlation reinforces opposite-direction exposures.
    Reinforcing relationships are transitively clustered and their absolute
    notionals are summed conservatively.
    """

    if type(policy) is not CorrelationConcentrationPolicy:
        raise TypeError("policy must be exact CorrelationConcentrationPolicy")
    policy = CorrelationConcentrationPolicy(
        max_correlated_gross_notional=policy.max_correlated_gross_notional,
        reinforcing_threshold=policy.reinforcing_threshold,
    )
    point_text = _utc_text(decision_time, name="decision_time")
    point = _utc_instant(point_text)
    exposures = _canonical_allocation_exposures(allocation)
    evidence_by_pair = _canonical_evidence(
        evidence,
        allowed_symbols=frozenset(exposures),
    )
    digest = _assessment_digest(
        exposures=exposures,
        evidence=evidence_by_pair,
        policy=policy,
        decision_time=point_text,
    )

    active = sorted(symbol for symbol, notional in exposures.items() if notional != 0)
    required_pairs = tuple(combinations(active, 2))
    missing = tuple(pair for pair in required_pairs if pair not in evidence_by_pair)
    stale: list[tuple[str, str]] = []
    for pair in required_pairs:
        item = evidence_by_pair.get(pair)
        if item is None:
            continue
        if point < _utc_instant(item.observed_at) or point > _utc_instant(item.valid_until):
            stale.append(pair)

    if missing or stale:
        reasons: list[str] = []
        if missing:
            reasons.append("missing decision-time pair evidence")
        if stale:
            reasons.append("future or expired pair evidence")
        return CorrelationConcentrationAssessment(
            status="INCONCLUSIVE",
            components=(),
            missing_pairs=missing,
            stale_pairs=tuple(stale),
            assessment_digest=digest,
            reason="; ".join(reasons),
        )

    components = _components(exposures, evidence_by_pair, policy.reinforcing_threshold)
    breached = tuple(
        component
        for component in components
        if component.gross_notional > policy.max_correlated_gross_notional
    )
    if breached:
        detail = ", ".join(
            f"{'/'.join(component.symbols)}={component.gross_notional}"
            for component in breached
        )
        return CorrelationConcentrationAssessment(
            status="FAIL",
            components=components,
            missing_pairs=(),
            stale_pairs=(),
            assessment_digest=digest,
            reason=f"correlated gross-notional cap exceeded: {detail}",
        )

    return CorrelationConcentrationAssessment(
        status="PASS",
        components=components,
        missing_pairs=(),
        stale_pairs=(),
        assessment_digest=digest,
        reason="complete fresh pair evidence is within the correlated gross-notional cap",
    )


def require_correlation_safe_allocation(
    allocation: AllocationResult,
    evidence: tuple[CorrelationEvidence, ...] | list[CorrelationEvidence],
    policy: CorrelationConcentrationPolicy,
    *,
    decision_time: str,
) -> AllocationResult:
    """Return the canonical allocation only when the correlation guard passes."""

    assessment = assess_correlation_concentration(
        allocation,
        evidence,
        policy,
        decision_time=decision_time,
    )
    if assessment.status != "PASS":
        raise CorrelationConcentrationError(
            f"correlation concentration {assessment.status.lower()}: {assessment.reason}"
        )
    return allocation


__all__ = [
    "CorrelationComponent",
    "CorrelationConcentrationAssessment",
    "CorrelationConcentrationError",
    "CorrelationConcentrationPolicy",
    "CorrelationEvidence",
    "assess_correlation_concentration",
    "require_correlation_safe_allocation",
]
