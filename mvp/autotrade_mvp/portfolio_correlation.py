"""Fail-closed correlation concentration guard for WP-32 allocations.

This composes the canonical evidence-bound allocation proposal.  It never sends
orders, replaces independent hard risk, authenticates a caller resolver, or
establishes economic edge.  Correlation is an uncertain descriptive estimate:
every active pair needs fresh content-bound interval evidence, and all policy
thresholds are explicit user/pre-registered values.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from itertools import combinations
import json
from typing import Mapping

from .allocation import (
    AllocationResult,
    AllocationTarget,
    EvidenceBoundObjectiveAllocationResult,
    ObjectiveAllocationResult,
    _allocation_decision_digest,
)
from .exact_decimal import (
    ExactDecimalError,
    exact_abs,
    exact_add,
    exact_multiply,
    parse_bounded_exact_decimal,
)

_ALLOWED_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


class CorrelationConcentrationError(ValueError):
    """The proposal did not pass this non-authorizing portfolio guard."""


def _text(value, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    value = str.strip(value)
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _dec(value, *, name: str) -> Decimal:
    if type(value) not in (Decimal, str, int) or type(value) is bool:
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a bounded exact decimal") from error


def _dec_text(value: Decimal) -> str:
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


def _utc(value, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(str.replace(text, "Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str.replace(value, "Z", "+00:00")).astimezone(
        timezone.utc
    )


def _hash(payload: dict[str, object], _dumps=json.dumps, _sha=sha256) -> str:
    return _sha(
        _dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _normalized_evidence_fields(raw: Mapping[str, object]) -> dict[str, object]:
    environment = _text(raw["environment"], name="correlation environment").upper()
    if environment not in _ALLOWED_ENVIRONMENTS:
        raise ValueError(f"unsupported correlation environment: {environment}")
    left = _text(raw["left_symbol"], name="left_symbol")
    right = _text(raw["right_symbol"], name="right_symbol")
    if left == right:
        raise ValueError("correlation evidence requires two distinct symbols")
    left, right = sorted((left, right))
    lower = _dec(raw["correlation_lower"], name="correlation_lower")
    upper = _dec(raw["correlation_upper"], name="correlation_upper")
    if lower < Decimal("-1") or upper > Decimal("1") or lower > upper:
        raise ValueError("correlation interval must satisfy -1 <= lower <= upper <= 1")
    observed = _utc(raw["observed_at"], name="observed_at")
    valid_until = _utc(raw["valid_until"], name="valid_until")
    sample_start = _utc(raw["sample_start"], name="sample_start")
    sample_end = _utc(raw["sample_end"], name="sample_end")
    if _instant(valid_until) < _instant(observed):
        raise ValueError("valid_until must not precede observed_at")
    if _instant(sample_end) < _instant(sample_start):
        raise ValueError("sample_end must not precede sample_start")
    if _instant(sample_end) > _instant(observed):
        raise ValueError("sample_end must not follow observed_at")
    return {
        "evidence_id": _text(raw["evidence_id"], name="correlation evidence_id"),
        "environment": environment,
        "left_symbol": left,
        "right_symbol": right,
        "correlation_lower": lower,
        "correlation_upper": upper,
        "observed_at": observed,
        "valid_until": valid_until,
        "sample_start": sample_start,
        "sample_end": sample_end,
        "estimator_id": _text(raw["estimator_id"], name="estimator_id"),
        "uncertainty_method": _text(
            raw["uncertainty_method"], name="uncertainty_method"
        ),
        "source_ref": _text(raw["source_ref"], name="source_ref"),
    }


def _evidence_payload(fields: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": "portfolio-correlation-evidence.v2",
        "evidence_id": fields["evidence_id"],
        "environment": fields["environment"],
        "left_symbol": fields["left_symbol"],
        "right_symbol": fields["right_symbol"],
        "correlation_lower": _dec_text(fields["correlation_lower"]),
        "correlation_upper": _dec_text(fields["correlation_upper"]),
        "observed_at": fields["observed_at"],
        "valid_until": fields["valid_until"],
        "sample_start": fields["sample_start"],
        "sample_end": fields["sample_end"],
        "estimator_id": fields["estimator_id"],
        "uncertainty_method": fields["uncertainty_method"],
        "source_ref": fields["source_ref"],
    }


@dataclass(frozen=True)
class CorrelationEvidence:
    """Content-bound interval; source authentication remains external."""

    evidence_id: str
    environment: str
    left_symbol: str
    right_symbol: str
    correlation_lower: Decimal
    correlation_upper: Decimal
    observed_at: str
    valid_until: str
    sample_start: str
    sample_end: str
    estimator_id: str
    uncertainty_method: str
    source_ref: str
    digest: str

    def __post_init__(self, _get=object.__getattribute__) -> None:
        fields = _normalized_evidence_fields(
            {
                name: _get(self, name)
                for name in (
                    "evidence_id",
                    "environment",
                    "left_symbol",
                    "right_symbol",
                    "correlation_lower",
                    "correlation_upper",
                    "observed_at",
                    "valid_until",
                    "sample_start",
                    "sample_end",
                    "estimator_id",
                    "uncertainty_method",
                    "source_ref",
                )
            }
        )
        supplied = _text(_get(self, "digest"), name="correlation digest")
        if supplied != _hash(_evidence_payload(fields)):
            raise ValueError("correlation evidence digest does not match canonical content")
        for name, value in fields.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "digest", supplied)

    @classmethod
    def create(cls, **values) -> "CorrelationEvidence":
        fields = _normalized_evidence_fields(values)
        return cls(**fields, digest=_hash(_evidence_payload(fields)))


@dataclass(frozen=True)
class CorrelationConcentrationPolicy:
    """Explicit user/pre-registered concentration envelope; no architecture defaults."""

    policy_id: str
    reporting_currency: str
    max_correlated_gross_notional: Decimal
    reinforcing_threshold: Decimal

    def __post_init__(self, _get=object.__getattribute__) -> None:
        policy_id = _text(_get(self, "policy_id"), name="policy_id")
        currency = _text(_get(self, "reporting_currency"), name="reporting_currency").upper()
        maximum = _dec(
            _get(self, "max_correlated_gross_notional"),
            name="max_correlated_gross_notional",
        )
        threshold = _dec(_get(self, "reinforcing_threshold"), name="reinforcing_threshold")
        if maximum <= 0:
            raise ValueError("max_correlated_gross_notional must be positive")
        if threshold < 0 or threshold > 1:
            raise ValueError("reinforcing_threshold must be between 0 and 1")
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "reporting_currency", currency)
        object.__setattr__(self, "max_correlated_gross_notional", maximum)
        object.__setattr__(self, "reinforcing_threshold", threshold)


@dataclass(frozen=True)
class CorrelationComponent:
    symbols: tuple[str, ...]
    gross_notional: Decimal


@dataclass(frozen=True)
class CorrelationConcentrationAssessment:
    status: str
    base_currency: str
    allocation_decision_digest: str
    policy_id: str
    components: tuple[CorrelationComponent, ...]
    evidence_refs: tuple[tuple[str, str], ...]
    missing_pairs: tuple[tuple[str, str], ...]
    stale_pairs: tuple[tuple[str, str], ...]
    assessment_digest: str
    reason: str
    economic_edge_status: str = "NOT_ESTABLISHED"
    evidence_authority_status: str = "RESOLVER_NOT_FINANCIAL_AUTHORITY"
    grants_trading_authority: bool = False


def _evidence_payload_field_names() -> tuple[str, ...]:
    return (
        "evidence_id",
        "environment",
        "left_symbol",
        "right_symbol",
        "correlation_lower",
        "correlation_upper",
        "observed_at",
        "valid_until",
        "sample_start",
        "sample_end",
        "estimator_id",
        "uncertainty_method",
        "source_ref",
    )


def _record(item: CorrelationEvidence, _type=CorrelationEvidence, _get=object.__getattribute__):
    if type(item) is not _type:
        raise TypeError("correlation evidence entries must be exact CorrelationEvidence")
    fields = _normalized_evidence_fields(
        {name: _get(item, name) for name in _evidence_payload_field_names()}
    )
    digest = _text(_get(item, "digest"), name="correlation digest")
    if digest != _hash(_evidence_payload(fields)):
        raise ValueError("correlation evidence digest does not match canonical content")
    return {**fields, "digest": digest}


def _allocation_context(
    result: EvidenceBoundObjectiveAllocationResult,
    _result_type=EvidenceBoundObjectiveAllocationResult,
    _objective_type=ObjectiveAllocationResult,
    _allocation_type=AllocationResult,
    _target_type=AllocationTarget,
    _get=object.__getattribute__,
    _decision_digest=_allocation_decision_digest,
    _decision_digest_code=getattr(_allocation_decision_digest, "__code__", None),
):
    if type(result) is not _result_type:
        raise TypeError("result must be exact EvidenceBoundObjectiveAllocationResult")
    objective = _get(result, "objective")
    if type(objective) is not _objective_type:
        raise TypeError("result objective must be exact ObjectiveAllocationResult")
    allocation = _get(objective, "allocation")
    if type(allocation) is not _allocation_type:
        raise TypeError("objective allocation must be exact AllocationResult")
    if type(_get(allocation, "status")) is not str or _get(allocation, "status") != "ALLOCATED":
        raise ValueError("correlation guard requires an allocated portfolio")
    targets = _get(allocation, "targets")
    if type(targets) is not tuple:
        raise TypeError("allocation targets must be an exact tuple")
    exposures: dict[str, Decimal] = {}
    for target in targets:
        if type(target) is not _target_type:
            raise TypeError("allocation targets must contain exact AllocationTarget values")
        symbol = _text(_get(target, "symbol"), name="allocation target symbol")
        if symbol in exposures:
            raise ValueError("allocation target symbols must be unique")
        quantity = _dec(_get(target, "quantity"), name=f"allocation quantity {symbol}")
        notional = _dec(_get(target, "notional"), name=f"allocation notional {symbol}")
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
        _dec(_get(allocation, "gross_notional"), name="allocation gross_notional") != gross
        or _dec(_get(allocation, "net_notional"), name="allocation net_notional")
        != exact_abs(signed_net)
    ):
        raise ValueError("allocation aggregate notionals disagree with targets")
    environment = _text(_get(result, "environment"), name="allocation environment").upper()
    if environment not in _ALLOWED_ENVIRONMENTS:
        raise ValueError(f"unsupported allocation environment: {environment}")
    decision_time = _utc(_get(result, "decision_time"), name="allocation decision_time")
    base_currency = _text(_get(result, "base_currency"), name="allocation base_currency").upper()
    decision_digest = _text(_get(result, "decision_digest"), name="allocation decision_digest")
    if (
        len(decision_digest) != 64
        or decision_digest.lower() != decision_digest
        or any(ch not in "0123456789abcdef" for ch in decision_digest)
    ):
        raise ValueError("allocation decision_digest must be lowercase sha256 hex")
    if getattr(_decision_digest, "__code__", None) is not _decision_digest_code:
        raise ValueError("allocation decision digest verifier executable changed after binding")
    expected_decision_digest = _decision_digest(
        objective,
        evidence_refs=_get(result, "evidence_refs"),
        environment=_get(result, "environment"),
        policy_version=_get(result, "policy_version"),
        policy_config_digest=_get(result, "policy_config_digest"),
        objective_search_config_digest=_get(result, "objective_search_config_digest"),
        decision_time=_get(result, "decision_time"),
        provider_id=_get(result, "provider_id"),
        account_id=_get(result, "account_id"),
        instrument_versions=_get(result, "instrument_versions"),
        capability_snapshot_ids=_get(result, "capability_snapshot_ids"),
        account_snapshot_id=_get(result, "account_snapshot_id"),
        reconciliation_run_id=_get(result, "reconciliation_run_id"),
        account_state_version=_get(result, "account_state_version"),
        reservation_state_version=_get(result, "reservation_state_version"),
        reservation_state_digest=_get(result, "reservation_state_digest"),
        base_currency=_get(result, "base_currency"),
    )
    if expected_decision_digest != decision_digest:
        raise ValueError("allocation decision_digest does not match result content")
    return exposures, environment, decision_time, base_currency, decision_digest


def _policy(policy: CorrelationConcentrationPolicy, _type=CorrelationConcentrationPolicy, _get=object.__getattribute__):
    if type(policy) is not _type:
        raise TypeError("policy must be exact CorrelationConcentrationPolicy")
    normalized = {
        "policy_id": _text(_get(policy, "policy_id"), name="policy_id"),
        "reporting_currency": _text(
            _get(policy, "reporting_currency"), name="reporting_currency"
        ).upper(),
        "max_correlated_gross_notional": _dec(
            _get(policy, "max_correlated_gross_notional"),
            name="max_correlated_gross_notional",
        ),
        "reinforcing_threshold": _dec(
            _get(policy, "reinforcing_threshold"), name="reinforcing_threshold"
        ),
    }
    if normalized["max_correlated_gross_notional"] <= 0:
        raise ValueError("max_correlated_gross_notional must be positive")
    if not Decimal("0") <= normalized["reinforcing_threshold"] <= Decimal("1"):
        raise ValueError("reinforcing_threshold must be between 0 and 1")
    return normalized


def _evidence_by_pair(evidence, resolved_evidence, *, active, environment, decision_time):
    if type(evidence) is tuple:
        evidence_snapshot = evidence
    elif type(evidence) is list:
        evidence_snapshot = tuple(list.copy(evidence))
    else:
        raise TypeError("correlation evidence must be an exact tuple or list")
    if type(resolved_evidence) is not dict:
        raise TypeError("resolved_evidence must be an exact dict")
    # Detach both caller-owned containers before any evidence traversal.  This
    # keeps resolver/container callbacks outside the assessment boundary and
    # prevents a resolver conversion from mutating the evidence sequence that
    # will be assessed.
    resolver = dict.copy(resolved_evidence)
    for key, value in dict.items(resolver):
        if type(key) is not str:
            raise TypeError("resolved_evidence keys must be exact str")
        if type(value) is not CorrelationEvidence:
            raise TypeError(
                "resolved_evidence values must be exact CorrelationEvidence"
            )
    point = _instant(decision_time)
    result = {}
    stale = []
    for item in evidence_snapshot:
        record = _record(item)
        evidence_id = record["evidence_id"]
        resolved = resolver.get(evidence_id)
        if type(resolved) is not CorrelationEvidence:
            raise ValueError(
                f"correlation evidence {evidence_id} cannot be resolved authoritatively"
            )
        if _record(resolved)["digest"] != record["digest"]:
            raise ValueError(
                f"correlation evidence {evidence_id} does not match resolved content"
            )
        if record["environment"] != environment:
            raise ValueError(f"correlation evidence {evidence_id} environment mismatch")
        pair = (record["left_symbol"], record["right_symbol"])
        if pair[0] not in active or pair[1] not in active:
            raise ValueError("correlation evidence references a symbol outside the allocation")
        if pair in result:
            raise ValueError("duplicate correlation evidence pair")
        result[pair] = record
        if point < _instant(record["observed_at"]) or point > _instant(record["valid_until"]):
            stale.append(pair)
    return result, tuple(sorted(stale))


def _components(exposures, evidence, threshold):
    active = sorted(symbol for symbol, amount in exposures.items() if amount != 0)
    parent = {symbol: symbol for symbol in active}

    def find(symbol):
        while parent[symbol] != symbol:
            parent[symbol] = parent[parent[symbol]]
            symbol = parent[symbol]
        return symbol

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    for left, right in combinations(active, 2):
        record = evidence[(left, right)]
        same_direction = (exposures[left] > 0) == (exposures[right] > 0)
        reinforcement = (
            record["correlation_upper"]
            if same_direction
            else exact_multiply(Decimal("-1"), record["correlation_lower"])
        )
        if reinforcement >= threshold:
            union(left, right)
    groups = {}
    for symbol in active:
        groups.setdefault(find(symbol), []).append(symbol)
    components = []
    for symbols in groups.values():
        gross = Decimal("0")
        for symbol in sorted(symbols):
            gross = exact_add(gross, exact_abs(exposures[symbol]))
        components.append(CorrelationComponent(tuple(sorted(symbols)), gross))
    return tuple(sorted(components, key=lambda item: item.symbols))


def _assessment_digest(*, decision_digest, environment, decision_time, currency, exposures, evidence, policy):
    return _hash(
        {
            "schema_version": "portfolio-correlation-concentration.v2",
            "allocation_decision_digest": decision_digest,
            "environment": environment,
            "decision_time": decision_time,
            "base_currency": currency,
            "policy": {
                "policy_id": policy["policy_id"],
                "reporting_currency": policy["reporting_currency"],
                "max_correlated_gross_notional": _dec_text(
                    policy["max_correlated_gross_notional"]
                ),
                "reinforcing_threshold": _dec_text(policy["reinforcing_threshold"]),
            },
            "exposures": [
                {"symbol": symbol, "notional": _dec_text(exposures[symbol])}
                for symbol in sorted(exposures)
            ],
            "evidence_refs": [
                {"evidence_id": record["evidence_id"], "digest": record["digest"]}
                for _, record in sorted(evidence.items())
            ],
        }
    )


def assess_correlation_concentration(
    result: EvidenceBoundObjectiveAllocationResult,
    evidence: tuple[CorrelationEvidence, ...] | list[CorrelationEvidence],
    resolved_evidence: Mapping[str, CorrelationEvidence],
    policy: CorrelationConcentrationPolicy,
) -> CorrelationConcentrationAssessment:
    """Assess the normalized proposal cut; PASS still grants no trading authority."""

    exposures, environment, decision_time, currency, decision_digest = _allocation_context(result)
    normalized_policy = _policy(policy)
    if normalized_policy["reporting_currency"] != currency:
        raise ValueError(
            "correlation policy reporting_currency must match allocation base_currency"
        )
    active = sorted(symbol for symbol, amount in exposures.items() if amount != 0)
    by_pair, stale = _evidence_by_pair(
        evidence,
        resolved_evidence,
        active=frozenset(active),
        environment=environment,
        decision_time=decision_time,
    )
    required = tuple(combinations(active, 2))
    missing = tuple(pair for pair in required if pair not in by_pair)
    refs = tuple(
        sorted((record["evidence_id"], record["digest"]) for record in by_pair.values())
    )
    digest = _assessment_digest(
        decision_digest=decision_digest,
        environment=environment,
        decision_time=decision_time,
        currency=currency,
        exposures=exposures,
        evidence=by_pair,
        policy=normalized_policy,
    )
    common = dict(
        base_currency=currency,
        allocation_decision_digest=decision_digest,
        policy_id=normalized_policy["policy_id"],
        evidence_refs=refs,
        assessment_digest=digest,
    )
    if missing or stale:
        reason = []
        if missing:
            reason.append("missing decision-time pair evidence")
        if stale:
            reason.append("future or expired pair evidence")
        return CorrelationConcentrationAssessment(
            status="INCONCLUSIVE",
            components=(),
            missing_pairs=missing,
            stale_pairs=stale,
            reason="; ".join(reason),
            **common,
        )
    components = _components(
        exposures, by_pair, normalized_policy["reinforcing_threshold"]
    )
    breached = tuple(
        item
        for item in components
        if item.gross_notional > normalized_policy["max_correlated_gross_notional"]
    )
    if breached:
        detail = ", ".join(
            f"{'/'.join(item.symbols)}={item.gross_notional} {currency}"
            for item in breached
        )
        return CorrelationConcentrationAssessment(
            status="FAIL",
            components=components,
            missing_pairs=(),
            stale_pairs=(),
            reason=f"correlated gross-notional cap exceeded: {detail}",
            **common,
        )
    return CorrelationConcentrationAssessment(
        status="PASS",
        components=components,
        missing_pairs=(),
        stale_pairs=(),
        reason=(
            "complete fresh uncertainty-bounded pair evidence is within the "
            "explicit correlated gross-notional policy cap"
        ),
        **common,
    )


def require_correlation_safe_proposal(
    result,
    evidence,
    resolved_evidence,
    policy,
    _assess=assess_correlation_concentration,
    _assess_code=getattr(assess_correlation_concentration, "__code__", None),
    _get=object.__getattribute__,
):
    """Return the unchanged proposal only after this non-authorizing guard passes."""
    # Retain the reviewed assessment function instead of resolving its public
    # module name at trust use. Otherwise a post-import rebinding can replace
    # the complete correlation assessment while this guard's own code remains
    # unchanged. Reject in-place mutation before the retained authority runs.
    if _get(_assess, "__code__") is not _assess_code:
        raise CorrelationConcentrationError(
            "correlation assessment executable changed after binding"
        )
    assessment = _assess(result, evidence, resolved_evidence, policy)
    status = _get(assessment, "status")
    if status != "PASS":
        raise CorrelationConcentrationError(
            f"correlation concentration {status.lower()}: "
            f"{object.__getattribute__(assessment, 'reason')}"
        )
    return result


__all__ = [
    "CorrelationComponent",
    "CorrelationConcentrationAssessment",
    "CorrelationConcentrationError",
    "CorrelationConcentrationPolicy",
    "CorrelationEvidence",
    "assess_correlation_concentration",
    "require_correlation_safe_proposal",
]
