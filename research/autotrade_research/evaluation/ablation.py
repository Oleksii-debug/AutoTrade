"""Evidence-bound matched-input causal ablation primitives.

This module measures marginal contribution on matched causal shadow cases. It
never routes models, grants trading authority, or treats the result as proof of
economic edge by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import (
    Decimal,
    DecimalException,
    InvalidOperation,
    ROUND_HALF_EVEN,
    localcontext,
)
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Iterable
from weakref import ref as weakref_ref

from autotrade_numeric.exact_decimal import (
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    parse_bounded_exact_decimal,
    terminating_decimal,
)
from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.io.strict_json import strict_json_loads
from autotrade_research.memory.episodes import ExperienceMemory
from autotrade_research.science.registry import ProtocolViolation, ScientificRegistry


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _decimal(value: Decimal | int | str, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must use Decimal, string or integer input")
    # Decimal is subclassable and its semantic methods are virtual. Reject a
    # caller-controlled subtype before is_finite/as_tuple/format/comparison can
    # influence scientific or economic-evidence authority.
    if isinstance(value, Decimal) and type(value) is not Decimal:
        raise TypeError(
            f"{field} must use exact built-in Decimal, string or integer input"
        )
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(
            f"{field} must be a finite decimal within the shared exact numeric resource envelope"
        ) from error


_ABLATION_REPORT_QUANTUM = Decimal("1e-50")
_ABLATION_REPORT_PRECISION = 384
_ABLATION_REPORT_FAILURE_POLICY = (
    "report-unavailable-preserve-exact-decision-v1"
)
_ABLATION_DECISION_RULE = "exact-rational-d2-sample-variance-v1"
_REPORTING_AVAILABLE = "AVAILABLE"
_REPORTING_UNAVAILABLE = "UNAVAILABLE"
_REPORTING_NOT_APPLICABLE = "NOT_APPLICABLE"


def _bounded(value: Fraction) -> Fraction:
    return bounded_fraction(value)


def _fraction_add(left: Fraction, right: Fraction) -> Fraction:
    return _bounded(left + right)


def _fraction_subtract(left: Fraction, right: Fraction) -> Fraction:
    return _bounded(left - right)


def _fraction_multiply(left: Fraction, right: Fraction) -> Fraction:
    return _bounded(left * right)


def _fraction_divide(left: Fraction, right: Fraction) -> Fraction:
    if right == 0:
        raise ZeroDivisionError("exact rational divisor must be non-zero")
    return _bounded(left / right)


def _mean_fraction(values: list[Fraction]) -> Fraction | None:
    if not values:
        return None
    total = Fraction(0, 1)
    for value in values:
        total = _fraction_add(total, value)
    return _fraction_divide(total, Fraction(len(values), 1))


def _report_fraction(value: Fraction) -> Decimal:
    value = _bounded(value)
    with localcontext() as context:
        context.prec = _ABLATION_REPORT_PRECISION
        context.rounding = ROUND_HALF_EVEN
        projected = Decimal(value.numerator) / Decimal(value.denominator)
        return projected.quantize(_ABLATION_REPORT_QUANTUM)


def _report_sqrt(value: Fraction) -> Decimal:
    value = _bounded(value)
    if value < 0:
        raise ValueError("cannot project square root of a negative rational")
    with localcontext() as context:
        context.prec = _ABLATION_REPORT_PRECISION
        context.rounding = ROUND_HALF_EVEN
        projected = (
            Decimal(value.numerator) / Decimal(value.denominator)
        ).sqrt()
        return projected.quantize(_ABLATION_REPORT_QUANTUM)


def _report_lower_bound(
    mean: Fraction,
    variance: Fraction,
    multiplier: Fraction,
    pair_count: int,
) -> Decimal:
    standard_error_squared = _fraction_divide(
        variance,
        Fraction(pair_count, 1),
    )
    with localcontext() as context:
        context.prec = _ABLATION_REPORT_PRECISION
        context.rounding = ROUND_HALF_EVEN
        mean_decimal = Decimal(mean.numerator) / Decimal(mean.denominator)
        multiplier_decimal = (
            Decimal(multiplier.numerator) / Decimal(multiplier.denominator)
        )
        se_decimal = (
            Decimal(standard_error_squared.numerator)
            / Decimal(standard_error_squared.denominator)
        ).sqrt()
        lower = mean_decimal - multiplier_decimal * se_decimal
        return lower.quantize(_ABLATION_REPORT_QUANTUM)


def _digest(value: str, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be canonical sha256:<64 lowercase hex>")
    return value


def _identity_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must be a non-empty string")
    return normalized


def _utc(value: datetime, field: str) -> datetime:
    # Scientific/economic evidence timestamps are authority-bearing UtcInstant
    # values. Reject caller-controlled datetime/tzinfo subclasses before any
    # virtual method can run, and reject non-UTC offsets rather than silently
    # normalizing them into a canonical-looking timestamp.
    if type(value) is not datetime:
        raise TypeError(f"{field} must use exact built-in datetime")
    if value.tzinfo is None or type(value.tzinfo) is not timezone:
        raise ValueError(f"{field} must be canonical timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field} must be canonical timezone-aware UTC")
    return value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CausalInputEvidence:
    """One exact input fact with causal availability and syndication identity."""

    evidence_id: str
    content_digest: str
    component_id: str
    available_utc: datetime
    syndication_group: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "component_id"):
            object.__setattr__(
                self,
                field_name,
                _identity_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "content_digest",
            _digest(self.content_digest, "content_digest"),
        )
        object.__setattr__(
            self,
            "available_utc",
            _utc(self.available_utc, "available_utc"),
        )
        if self.syndication_group is not None:
            object.__setattr__(
                self,
                "syndication_group",
                _identity_text(self.syndication_group, "syndication_group"),
            )


@dataclass(frozen=True)
class AblationOutcome:
    case_id: str
    input_fingerprint: str
    variant: str
    utility: Decimal
    cost: Decimal
    elapsed_ms: int
    deadline_ms: int
    components: tuple[str, ...]
    input_cutoff_utc: datetime
    decision_utc: datetime
    outcome_available_utc: datetime
    population_unit_id: str | None = None
    input_evidence: tuple[CausalInputEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "case_id",
            _identity_text(self.case_id, "case_id"),
        )
        population_unit = self.population_unit_id
        if population_unit is None:
            population_unit = self.case_id
        object.__setattr__(
            self,
            "population_unit_id",
            _identity_text(population_unit, "population_unit_id"),
        )
        object.__setattr__(
            self,
            "input_fingerprint",
            _digest(self.input_fingerprint, "input_fingerprint"),
        )
        if type(self.variant) is not str or self.variant not in {"FULL", "ABLATED"}:
            raise ValueError("variant must be exact FULL or ABLATED text")
        if type(self.elapsed_ms) is not int or type(self.deadline_ms) is not int:
            raise TypeError("elapsed_ms and deadline_ms must be integers")
        if self.elapsed_ms < 0 or self.deadline_ms <= 0:
            raise ValueError("elapsed_ms must be non-negative and deadline_ms positive")
        if type(self.components) is not tuple:
            raise TypeError("components must be an exact immutable tuple")
        normalized_components = tuple(
            _identity_text(item, "component identity")
            for item in self.components
        )
        if len(normalized_components) != len(set(normalized_components)):
            raise ValueError("components must use deduplicated canonical identities")
        object.__setattr__(self, "components", normalized_components)
        object.__setattr__(self, "utility", _decimal(self.utility, "utility"))
        cost = _decimal(self.cost, "cost")
        if cost < 0:
            raise ValueError("cost must be non-negative")
        object.__setattr__(self, "cost", cost)

        cutoff = _utc(self.input_cutoff_utc, "input_cutoff_utc")
        decision = _utc(self.decision_utc, "decision_utc")
        outcome = _utc(self.outcome_available_utc, "outcome_available_utc")
        if decision < cutoff:
            raise ValueError(
                "decision cannot precede the declared input cutoff"
            )
        if outcome <= cutoff:
            raise ValueError("outcome must become available strictly after the input cutoff")
        if decision >= outcome:
            raise ValueError(
                "decision must occur strictly before outcome availability"
            )
        object.__setattr__(self, "input_cutoff_utc", cutoff)
        object.__setattr__(self, "decision_utc", decision)
        object.__setattr__(self, "outcome_available_utc", outcome)

        if type(self.input_evidence) is not tuple:
            raise TypeError("input_evidence must be an immutable tuple")
        if any(
            type(item) is not CausalInputEvidence
            for item in self.input_evidence
        ):
            raise TypeError(
                "input_evidence entries must be exact CausalInputEvidence"
            )
        evidence_ids = [item.evidence_id for item in self.input_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("input_evidence must use unique evidence_id values")
        content_digests = [
            item.content_digest for item in self.input_evidence
        ]
        if len(content_digests) != len(set(content_digests)):
            raise ValueError(
                "syndicated duplicate content must be deduplicated by digest"
            )
        syndication_groups = [
            item.syndication_group
            for item in self.input_evidence
            if item.syndication_group is not None
        ]
        if len(syndication_groups) != len(set(syndication_groups)):
            raise ValueError(
                "syndicated input groups must be deduplicated before ablation"
            )
        if any(item.available_utc > cutoff for item in self.input_evidence):
            raise ValueError(
                "input evidence cannot become available after the causal cutoff"
            )

    @property
    def met_deadline(self) -> bool:
        return self.elapsed_ms <= self.deadline_ms


@dataclass(frozen=True)
class AblationPair:
    target_component: str
    full: AblationOutcome
    ablated: AblationOutcome

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_component",
            _identity_text(self.target_component, "target_component"),
        )
        if type(self.full) is not AblationOutcome or type(self.ablated) is not AblationOutcome:
            raise TypeError("pair outcomes must be exact AblationOutcome values")
        if self.full.variant != "FULL" or self.ablated.variant != "ABLATED":
            raise ValueError("pair must contain FULL and ABLATED outcomes")
        if self.full.case_id != self.ablated.case_id:
            raise ValueError("matched outcomes must share case_id")
        if self.full.input_fingerprint != self.ablated.input_fingerprint:
            raise ValueError("matched outcomes must share exact input_fingerprint")
        if self.full.deadline_ms != self.ablated.deadline_ms:
            raise ValueError("matched outcomes must use the same deadline budget")
        if self.full.input_cutoff_utc != self.ablated.input_cutoff_utc:
            raise ValueError("matched outcomes must share exact causal input cutoff")
        if self.full.decision_utc != self.ablated.decision_utc:
            raise ValueError("matched outcomes must share exact decision time")
        if self.full.outcome_available_utc != self.ablated.outcome_available_utc:
            raise ValueError("matched outcomes must share outcome availability")
        if self.full.population_unit_id != self.ablated.population_unit_id:
            raise ValueError(
                "matched outcomes must share the same independent population unit"
            )

        full_evidence = {
            item.evidence_id: item for item in self.full.input_evidence
        }
        ablated_evidence = {
            item.evidence_id: item for item in self.ablated.input_evidence
        }
        for evidence_id, item in ablated_evidence.items():
            if full_evidence.get(evidence_id) != item:
                raise ValueError(
                    "shared causal input evidence must match exactly"
                )
        removed_evidence = [
            item
            for evidence_id, item in full_evidence.items()
            if evidence_id not in ablated_evidence
        ]
        if any(
            item.component_id != self.target_component
            for item in removed_evidence
        ):
            raise ValueError(
                "input evidence may differ only by target-component evidence"
            )
        if any(
            item.component_id == self.target_component
            for item in ablated_evidence.values()
        ):
            raise ValueError(
                "ABLATED outcome cannot retain target-component evidence"
            )

        full_components = set(self.full.components)
        ablated_components = set(self.ablated.components)
        if self.target_component not in full_components:
            raise ValueError("target component must be present in FULL outcome")
        if self.target_component in ablated_components:
            raise ValueError("target component must be absent from ABLATED outcome")
        if full_components - {self.target_component} != ablated_components:
            raise ValueError("matched pair may differ only by the target component")

    @property
    def deadline_comparable(self) -> bool:
        return self.full.met_deadline == self.ablated.met_deadline

    @property
    def utility_comparable(self) -> bool:
        return self.full.met_deadline and self.ablated.met_deadline

    @property
    def utility_delta(self) -> Decimal | None:
        if not self.utility_comparable:
            return None
        return terminating_decimal(_pair_utility_fraction(self))

    @property
    def cost_delta(self) -> Decimal:
        return terminating_decimal(_pair_cost_fraction(self))

    @property
    def net_value_delta(self) -> Decimal | None:
        if not self.utility_comparable:
            return None
        return terminating_decimal(_pair_net_value_fraction(self))

    @property
    def latency_delta_ms(self) -> int:
        return self.full.elapsed_ms - self.ablated.elapsed_ms


@dataclass(frozen=True)
class AblationSummary:
    target_component: str
    total_pairs: int
    comparable_pairs: int
    deadline_mismatch_pairs: int
    both_deadline_miss_pairs: int
    full_deadline_misses: int
    ablated_deadline_misses: int
    mean_utility_delta: Decimal | None
    mean_cost_delta: Decimal | None
    mean_latency_delta_ms: Decimal | None
    status: str
    reporting_status: str


@dataclass(frozen=True)
class ExactAblationDecision:
    """Exact rational operands that alone authorize the PASS/FAIL verdict."""

    pair_count: int
    mean: Fraction
    sample_variance: Fraction
    threshold_delta: Fraction
    uncertainty_multiplier: Fraction
    lhs: Fraction
    rhs: Fraction
    status: str

    def __post_init__(self) -> None:
        if type(self.pair_count) is not int or self.pair_count < 2:
            raise ValueError("exact decision pair_count must be an integer >= 2")
        for field_name in (
            "mean",
            "sample_variance",
            "threshold_delta",
            "uncertainty_multiplier",
            "lhs",
            "rhs",
        ):
            value = getattr(self, field_name)
            if type(value) is not Fraction:
                raise TypeError(f"{field_name} must be Fraction")
            bounded_fraction(value)
        if self.sample_variance < 0:
            raise ValueError("sample_variance must be non-negative")
        if self.uncertainty_multiplier < 0:
            raise ValueError("uncertainty_multiplier must be non-negative")
        expected_lhs = _fraction_multiply(
            self.threshold_delta,
            self.threshold_delta,
        )
        expected_rhs = _fraction_multiply(
            _fraction_multiply(
                self.uncertainty_multiplier,
                self.uncertainty_multiplier,
            ),
            _fraction_divide(
                self.sample_variance,
                Fraction(self.pair_count, 1),
            ),
        )
        if self.lhs != expected_lhs or self.rhs != expected_rhs:
            raise ValueError("exact decision comparison operands are inconsistent")
        expected_status = (
            "FAIL"
            if self.threshold_delta < 0
            else (
                "PASS"
                if self.uncertainty_multiplier == 0 or self.lhs >= self.rhs
                else "FAIL"
            )
        )
        if type(self.status) is not str:
            raise TypeError("exact decision status must be text")
        if self.status != expected_status:
            raise ValueError("exact decision status is inconsistent with operands")


@dataclass(frozen=True)
class AblationEvaluation:
    target_component: str
    pair_count: int
    mean_net_incremental_value: Decimal | None
    sample_stddev: Decimal | None
    lower_bound: Decimal | None
    required_lower_bound: Decimal
    uncertainty_multiplier: Decimal
    status: str
    reason: str
    decision_exact: ExactAblationDecision | None = None
    reporting_status: str = _REPORTING_NOT_APPLICABLE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_component",
            _identity_text(self.target_component, "target_component"),
        )
        if type(self.pair_count) is not int or self.pair_count < 0:
            raise ValueError("evaluation pair_count must be a non-negative integer")
        if type(self.status) is not str:
            raise TypeError("evaluation status must be text")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("evaluation reason must be non-empty text")
        if type(self.reporting_status) is not str:
            raise TypeError("reporting_status must be text")
        if (
            self.decision_exact is not None
            and type(self.decision_exact) is not ExactAblationDecision
        ):
            raise TypeError("decision_exact must be ExactAblationDecision or None")
        if self.reporting_status not in {
            _REPORTING_AVAILABLE,
            _REPORTING_UNAVAILABLE,
            _REPORTING_NOT_APPLICABLE,
        }:
            raise ValueError("reporting_status is not canonical")
        reports = (
            self.mean_net_incremental_value,
            self.sample_stddev,
            self.lower_bound,
        )
        if self.status in {"PASS", "FAIL"}:
            if self.decision_exact is None:
                raise ValueError("terminal evaluation requires exact decision material")
            if self.reporting_status == _REPORTING_AVAILABLE:
                if any(value is None for value in reports):
                    raise ValueError(
                        "available reporting projection requires all report values"
                    )
            elif self.reporting_status == _REPORTING_UNAVAILABLE:
                if any(value is not None for value in reports):
                    raise ValueError(
                        "unavailable reporting projection cannot carry report values"
                    )
            else:
                raise ValueError(
                    "terminal evaluation requires explicit reporting availability"
                )
        elif self.status == "INCONCLUSIVE":
            if self.decision_exact is not None:
                raise ValueError(
                    "inconclusive evaluation cannot carry terminal exact decision"
                )
            if self.reporting_status != _REPORTING_NOT_APPLICABLE:
                raise ValueError(
                    "inconclusive evaluation reporting must be not applicable"
                )
        else:
            raise ValueError("ablation evaluation status is not canonical")

def _validate_pairs(target_component: str, pairs: Iterable[AblationPair]) -> list[AblationPair]:
    target_component = _identity_text(target_component, "target_component")
    selected = list(pairs)
    if any(type(pair) is not AblationPair for pair in selected):
        raise TypeError("pairs must contain exact AblationPair values")
    if any(pair.target_component != target_component for pair in selected):
        raise ValueError("all pairs must target the requested component")

    seen_case_ids: set[str] = set()
    seen_input_fingerprints: set[str] = set()
    seen_population_units: set[str] = set()
    seen_target_evidence_ids: set[str] = set()
    seen_target_content_digests: set[str] = set()
    seen_target_syndication_groups: set[str] = set()
    for pair in selected:
        case_id = pair.full.case_id
        fingerprint = pair.full.input_fingerprint
        if case_id in seen_case_ids:
            raise ValueError("duplicate matched ablation case_id")
        if fingerprint in seen_input_fingerprints:
            raise ValueError("duplicate matched ablation input_fingerprint")
        seen_case_ids.add(case_id)
        seen_input_fingerprints.add(fingerprint)

        population_unit = pair.full.population_unit_id
        if population_unit in seen_population_units:
            raise ValueError(
                "duplicate independent population unit; syndicated or aliased "
                "cases must be deduplicated"
            )
        seen_population_units.add(population_unit)

        target_evidence = [
            item
            for item in pair.full.input_evidence
            if item.component_id == target_component
        ]
        for item in target_evidence:
            if item.evidence_id in seen_target_evidence_ids:
                raise ValueError(
                    "duplicate target evidence identity across matched cases; "
                    "cases are not independent"
                )
            seen_target_evidence_ids.add(item.evidence_id)

            if item.content_digest in seen_target_content_digests:
                raise ValueError(
                    "duplicate target evidence content across matched cases; "
                    "cases are not independent"
                )
            seen_target_content_digests.add(item.content_digest)

            group = item.syndication_group
            if group is not None:
                if group in seen_target_syndication_groups:
                    raise ValueError(
                        "duplicate target syndication group across matched cases; "
                        "cases are not independent"
                    )
                seen_target_syndication_groups.add(group)
    return selected


def _pair_utility_fraction(pair: AblationPair) -> Fraction:
    return _fraction_subtract(
        as_fraction(pair.full.utility),
        as_fraction(pair.ablated.utility),
    )


def _pair_cost_fraction(pair: AblationPair) -> Fraction:
    return _fraction_subtract(
        as_fraction(pair.full.cost),
        as_fraction(pair.ablated.cost),
    )


def _pair_net_value_fraction(pair: AblationPair) -> Fraction:
    full_net = _fraction_subtract(
        as_fraction(pair.full.utility),
        as_fraction(pair.full.cost),
    )
    ablated_net = _fraction_subtract(
        as_fraction(pair.ablated.utility),
        as_fraction(pair.ablated.cost),
    )
    return _fraction_subtract(full_net, ablated_net)


def _build_exact_decision(
    values: list[Fraction],
    *,
    required: Decimal,
    multiplier: Decimal,
) -> ExactAblationDecision:
    count = len(values)
    mean = _mean_fraction(values)
    if mean is None:
        raise ValueError("exact decision requires at least one value")

    squared_sum = Fraction(0, 1)
    for value in values:
        deviation = _fraction_subtract(value, mean)
        squared_sum = _fraction_add(
            squared_sum,
            _fraction_multiply(deviation, deviation),
        )
    variance = _fraction_divide(
        squared_sum,
        Fraction(count - 1, 1),
    )
    required_fraction = as_fraction(required)
    multiplier_fraction = as_fraction(multiplier)
    threshold_delta = _fraction_subtract(mean, required_fraction)
    lhs = _fraction_multiply(threshold_delta, threshold_delta)
    standard_error_squared = _fraction_divide(
        variance,
        Fraction(count, 1),
    )
    multiplier_squared = _fraction_multiply(
        multiplier_fraction,
        multiplier_fraction,
    )
    rhs = _fraction_multiply(
        multiplier_squared,
        standard_error_squared,
    )

    if threshold_delta < 0:
        status = "FAIL"
    elif multiplier_fraction == 0:
        status = "PASS"
    else:
        status = "PASS" if lhs >= rhs else "FAIL"

    return ExactAblationDecision(
        pair_count=count,
        mean=mean,
        sample_variance=variance,
        threshold_delta=threshold_delta,
        uncertainty_multiplier=multiplier_fraction,
        lhs=lhs,
        rhs=rhs,
        status=status,
    )


def summarize_ablation(target_component: str, pairs: Iterable[AblationPair]) -> AblationSummary:
    target_component = _identity_text(target_component, "target_component")
    selected = _validate_pairs(target_component, pairs)
    comparable = [pair for pair in selected if pair.utility_comparable]

    mean_utility_report: Decimal | None = None
    mean_cost_report: Decimal | None = None
    mean_latency_report: Decimal | None = None
    reporting_status = _REPORTING_AVAILABLE
    try:
        utility_fractions = [_pair_utility_fraction(pair) for pair in comparable]
        cost_fractions = [_pair_cost_fraction(pair) for pair in selected]
        latency_fractions = [
            _bounded(Fraction(pair.latency_delta_ms, 1))
            for pair in selected
        ]
        mean_utility_fraction = _mean_fraction(utility_fractions)
        mean_cost_fraction = _mean_fraction(cost_fractions) or Fraction(0, 1)
        mean_latency_fraction = (
            _mean_fraction(latency_fractions) or Fraction(0, 1)
        )
        mean_utility_report = (
            None
            if mean_utility_fraction is None
            else _report_fraction(mean_utility_fraction)
        )
        mean_cost_report = _report_fraction(mean_cost_fraction)
        mean_latency_report = _report_fraction(mean_latency_fraction)
    except (DecimalException, ExactDecimalError):
        reporting_status = _REPORTING_UNAVAILABLE

    return AblationSummary(
        target_component=target_component,
        total_pairs=len(selected),
        comparable_pairs=len(comparable),
        deadline_mismatch_pairs=sum(not pair.deadline_comparable for pair in selected),
        both_deadline_miss_pairs=sum(
            not pair.full.met_deadline and not pair.ablated.met_deadline
            for pair in selected
        ),
        full_deadline_misses=sum(not pair.full.met_deadline for pair in selected),
        ablated_deadline_misses=sum(not pair.ablated.met_deadline for pair in selected),
        mean_utility_delta=mean_utility_report,
        mean_cost_delta=mean_cost_report,
        mean_latency_delta_ms=mean_latency_report,
        status="DESCRIPTIVE_ONLY" if comparable else "INCONCLUSIVE",
        reporting_status=reporting_status,
    )



def evaluate_incremental_value(
    target_component: str,
    pairs: Iterable[AblationPair],
    *,
    minimum_pairs: int,
    required_lower_bound: Decimal,
    uncertainty_multiplier: Decimal = Decimal("2"),
) -> AblationEvaluation:
    """Measure conservative net marginal value with an exact rational verdict."""

    if type(minimum_pairs) is not int or minimum_pairs < 2:
        raise ValueError("minimum_pairs must be an integer >= 2")
    required = _decimal(required_lower_bound, "required_lower_bound")
    multiplier = _decimal(uncertainty_multiplier, "uncertainty_multiplier")
    if multiplier < 0:
        raise ValueError("uncertainty_multiplier must be non-negative")

    target = _identity_text(target_component, "target_component")
    selected = _validate_pairs(target, pairs)
    if any(not pair.full.input_evidence for pair in selected):
        return AblationEvaluation(
            target_component=target,
            pair_count=0,
            mean_net_incremental_value=None,
            sample_stddev=None,
            lower_bound=None,
            required_lower_bound=required,
            uncertainty_multiplier=multiplier,
            status="INCONCLUSIVE",
            reason="missing_causal_input_evidence",
        )

    if any(
        not any(
            item.component_id == target
            for item in pair.full.input_evidence
        )
        for pair in selected
    ):
        return AblationEvaluation(
            target_component=target,
            pair_count=0,
            mean_net_incremental_value=None,
            sample_stddev=None,
            lower_bound=None,
            required_lower_bound=required,
            uncertainty_multiplier=multiplier,
            status="INCONCLUSIVE",
            reason="missing_target_component_causal_evidence",
        )

    try:
        concrete = [
            _pair_net_value_fraction(pair)
            for pair in selected
            if pair.utility_comparable
        ]
    except ExactDecimalError:
        return AblationEvaluation(
            target_component=target,
            pair_count=0,
            mean_net_incremental_value=None,
            sample_stddev=None,
            lower_bound=None,
            required_lower_bound=required,
            uncertainty_multiplier=multiplier,
            status="INCONCLUSIVE",
            reason="exact_numeric_resource_envelope_exceeded",
        )

    if len(concrete) < minimum_pairs:
        return AblationEvaluation(
            target_component=target,
            pair_count=len(concrete),
            mean_net_incremental_value=None,
            sample_stddev=None,
            lower_bound=None,
            required_lower_bound=required,
            uncertainty_multiplier=multiplier,
            status="INCONCLUSIVE",
            reason="insufficient_comparable_matched_pairs",
        )

    try:
        decision = _build_exact_decision(
            concrete,
            required=required,
            multiplier=multiplier,
        )
    except ExactDecimalError:
        return AblationEvaluation(
            target_component=target,
            pair_count=len(concrete),
            mean_net_incremental_value=None,
            sample_stddev=None,
            lower_bound=None,
            required_lower_bound=required,
            uncertainty_multiplier=multiplier,
            status="INCONCLUSIVE",
            reason="exact_numeric_resource_envelope_exceeded",
        )

    reporting_status = _REPORTING_AVAILABLE
    mean_report: Decimal | None = None
    stddev_report: Decimal | None = None
    lower_report: Decimal | None = None
    try:
        mean_report = _report_fraction(decision.mean)
        stddev_report = _report_sqrt(decision.sample_variance)
        lower_report = _report_lower_bound(
            decision.mean,
            decision.sample_variance,
            decision.uncertainty_multiplier,
            decision.pair_count,
        )
    except (DecimalException, ExactDecimalError):
        reporting_status = _REPORTING_UNAVAILABLE
        mean_report = None
        stddev_report = None
        lower_report = None

    return AblationEvaluation(
        target_component=target,
        pair_count=len(concrete),
        mean_net_incremental_value=mean_report,
        sample_stddev=stddev_report,
        lower_bound=lower_report,
        required_lower_bound=required,
        uncertainty_multiplier=multiplier,
        status=decision.status,
        reason="matched_causal_ablation_net_of_cost_exact_rational",
        decision_exact=decision,
        reporting_status=reporting_status,
    )



@dataclass(frozen=True)
class AblationEvidenceBundle:
    """Deterministic, self-describing evidence artifact for one ablation evaluation."""

    target_component: str
    source_revision: str
    protocol_digest: str
    dataset_digest: str
    minimum_pairs: int
    required_lower_bound: Decimal
    uncertainty_multiplier: Decimal
    pair_count: int
    evaluation: AblationEvaluation
    payload: str
    content_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_component",
            _identity_text(self.target_component, "target_component"),
        )
        if type(self.source_revision) is not str or _GIT_SHA.fullmatch(self.source_revision) is None:
            raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
        object.__setattr__(
            self,
            "protocol_digest",
            _digest(self.protocol_digest, "protocol_digest"),
        )
        object.__setattr__(
            self,
            "dataset_digest",
            _digest(self.dataset_digest, "dataset_digest"),
        )
        if type(self.minimum_pairs) is not int or self.minimum_pairs < 2:
            raise ValueError("minimum_pairs must be an integer >= 2")
        required = _decimal(self.required_lower_bound, "required_lower_bound")
        multiplier = _decimal(self.uncertainty_multiplier, "uncertainty_multiplier")
        if multiplier < 0:
            raise ValueError("uncertainty_multiplier must be non-negative")
        object.__setattr__(self, "required_lower_bound", required)
        object.__setattr__(self, "uncertainty_multiplier", multiplier)
        if type(self.pair_count) is not int or self.pair_count < 0:
            raise ValueError("pair_count must be a non-negative integer")
        if type(self.evaluation) is not AblationEvaluation:
            raise TypeError("evaluation must be AblationEvaluation")
        if self.evaluation.target_component != self.target_component:
            raise ValueError("evaluation target_component must match the bundle")
        if self.evaluation.required_lower_bound != required:
            raise ValueError("evaluation required_lower_bound must match the bundle")
        if self.evaluation.uncertainty_multiplier != multiplier:
            raise ValueError("evaluation uncertainty_multiplier must match the bundle")
        if self.evaluation.pair_count > self.pair_count:
            raise ValueError("evaluation pair_count cannot exceed locked pair_count")
        decision = self.evaluation.decision_exact
        if self.evaluation.status in {"PASS", "FAIL"}:
            if decision is None:
                raise ValueError("terminal evaluation requires exact decision material")
            if decision.pair_count != self.evaluation.pair_count:
                raise ValueError("exact decision pair_count must match evaluation")
            if decision.status != self.evaluation.status:
                raise ValueError("exact decision status must match evaluation")
            if decision.uncertainty_multiplier != as_fraction(multiplier):
                raise ValueError(
                    "exact decision uncertainty multiplier must match evaluation"
                )
            expected_delta = _fraction_subtract(
                decision.mean,
                as_fraction(required),
            )
            if decision.threshold_delta != expected_delta:
                raise ValueError(
                    "exact decision threshold delta must match evaluation policy"
                )
        elif decision is not None:
            raise ValueError("inconclusive evaluation cannot carry terminal exact decision")
        if type(self.payload) is not str or not self.payload:
            raise ValueError("payload must be non-empty canonical JSON")
        object.__setattr__(
            self,
            "content_digest",
            _digest(self.content_digest, "content_digest"),
        )
        actual = "sha256:" + sha256(self.payload.encode("utf-8")).hexdigest()
        if actual != self.content_digest:
            raise ValueError("content_digest does not match payload bytes")
        try:
            decoded = json.loads(self.payload)
        except json.JSONDecodeError as error:
            raise ValueError("payload must be valid canonical JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("payload must be a canonical JSON object")
        try:
            canonical_payload = json.dumps(
                decoded,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("payload must be valid canonical JSON") from error
        if canonical_payload != self.payload:
            raise ValueError("payload must use canonical JSON serialization")
        if decoded.get("evaluation") != _evaluation_payload(self.evaluation):
            raise ValueError("payload evaluation does not match bundle evaluation")
        policy = decoded.get("evaluation_policy", {})
        if (
            decoded.get("schema_version") != "2.0.0"
            or decoded.get("target_component") != self.target_component
            or decoded.get("source_revision") != self.source_revision
            or decoded.get("protocol_digest") != self.protocol_digest
            or decoded.get("dataset_digest") != self.dataset_digest
            or decoded.get("pair_count") != self.pair_count
            or policy.get("minimum_pairs") != self.minimum_pairs
            or policy.get("required_lower_bound") != _canonical_decimal_text(required)
            or policy.get("uncertainty_multiplier") != _canonical_decimal_text(multiplier)
            or policy.get("decision_rule") != _ABLATION_DECISION_RULE
            or policy.get("reporting_projection") != {
                "failure_policy": _ABLATION_REPORT_FAILURE_POLICY,
                "precision": _ABLATION_REPORT_PRECISION,
                "quantum": _canonical_decimal_text(_ABLATION_REPORT_QUANTUM),
            }
        ):
            raise ValueError("payload metadata does not match bundle metadata")


def _canonical_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _fraction_payload(value: Fraction) -> dict[str, str]:
    value = _bounded(value)
    return {
        "denominator": str(value.denominator),
        "numerator": str(value.numerator),
    }


def _exact_decision_payload(
    decision: ExactAblationDecision | None,
) -> dict[str, object] | None:
    if decision is None:
        return None
    return {
        "lhs": _fraction_payload(decision.lhs),
        "mean": _fraction_payload(decision.mean),
        "pair_count": decision.pair_count,
        "rhs": _fraction_payload(decision.rhs),
        "sample_variance": _fraction_payload(decision.sample_variance),
        "status": decision.status,
        "threshold_delta": _fraction_payload(decision.threshold_delta),
        "uncertainty_multiplier": _fraction_payload(
            decision.uncertainty_multiplier
        ),
    }


def _canonical_utc_text(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


def _causal_input_payload(item: CausalInputEvidence) -> dict[str, object]:
    return {
        "available_utc": _canonical_utc_text(item.available_utc),
        "component_id": item.component_id,
        "content_digest": item.content_digest,
        "evidence_id": item.evidence_id,
        "syndication_group": item.syndication_group,
    }


def _outcome_payload(item: AblationOutcome) -> dict[str, object]:
    return {
        "case_id": item.case_id,
        "components": sorted(item.components),
        "cost": _canonical_decimal_text(item.cost),
        "deadline_ms": item.deadline_ms,
        "decision_utc": _canonical_utc_text(item.decision_utc),
        "elapsed_ms": item.elapsed_ms,
        "input_cutoff_utc": _canonical_utc_text(item.input_cutoff_utc),
        "input_evidence": [
            _causal_input_payload(evidence)
            for evidence in sorted(item.input_evidence, key=lambda evidence: evidence.evidence_id)
        ],
        "input_fingerprint": item.input_fingerprint,
        "outcome_available_utc": _canonical_utc_text(item.outcome_available_utc),
        "population_unit_id": item.population_unit_id,
        "utility": _canonical_decimal_text(item.utility),
        "variant": item.variant,
    }


def _evaluation_payload(item: AblationEvaluation) -> dict[str, object]:
    return {
        "decision_exact": _exact_decision_payload(item.decision_exact),
        "lower_bound": _canonical_decimal_text(item.lower_bound),
        "mean_net_incremental_value": _canonical_decimal_text(
            item.mean_net_incremental_value
        ),
        "pair_count": item.pair_count,
        "reason": item.reason,
        "reporting_status": item.reporting_status,
        "required_lower_bound": _canonical_decimal_text(item.required_lower_bound),
        "sample_stddev": _canonical_decimal_text(item.sample_stddev),
        "status": item.status,
        "target_component": item.target_component,
        "uncertainty_multiplier": _canonical_decimal_text(
            item.uncertainty_multiplier
        ),
    }



@dataclass(frozen=True)
class AblationOutcomeArtifactRef:
    """Immutable reference to one pre-existing canonical ablation outcome artifact."""

    artifact_id: str
    sha256: str

    def __post_init__(self) -> None:
        from uuid import UUID

        if type(self.artifact_id) is not str:
            raise ValueError("artifact_id must be a canonical UUID")
        try:
            canonical_id = str(UUID(self.artifact_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("artifact_id must be a canonical UUID") from error
        if canonical_id != self.artifact_id:
            raise ValueError("artifact_id must use canonical UUID text")
        object.__setattr__(self, "sha256", _digest(self.sha256, "sha256"))


_ABLATION_OUTCOME_MEDIA_TYPE = "application/vnd.autotrade.ablation-outcome+json"


def _parse_utc_text(value: object, field: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"{field} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{field} must be canonical UTC text") from error
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError(f"{field} must be canonical UTC text")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CanonicalAblationOutcomeEvidence:
    """Immutable binding from one matched outcome to canonical economic evidence."""

    case_id: str
    variant: str
    population_unit_id: str
    utility: Decimal
    cost: Decimal
    outcome_available_utc: datetime
    source_revision: str
    utility_evidence_digest: str
    cost_evidence_digest: str
    evidence_digest: str
    superseded_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("case_id", "population_unit_id"):
            value = getattr(self, name)
            canonical = _identity_text(value, name)
            if canonical != value:
                raise ValueError(f"{name} must use canonical text")
            object.__setattr__(self, name, canonical)
        if type(self.variant) is not str or self.variant not in {"FULL", "ABLATED"}:
            raise ValueError("variant must be FULL or ABLATED")
        object.__setattr__(self, "utility", _decimal(self.utility, "utility"))
        cost = _decimal(self.cost, "cost")
        if cost < 0:
            raise ValueError("cost must be non-negative")
        object.__setattr__(self, "cost", cost)
        object.__setattr__(
            self,
            "outcome_available_utc",
            _utc(self.outcome_available_utc, "outcome_available_utc"),
        )
        if type(self.source_revision) is not str or _GIT_SHA.fullmatch(self.source_revision) is None:
            raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
        for name in (
            "utility_evidence_digest",
            "cost_evidence_digest",
            "evidence_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if self.superseded_at_utc is not None:
            superseded = _utc(self.superseded_at_utc, "superseded_at_utc")
            if superseded <= self.outcome_available_utc:
                raise ValueError(
                    "superseded_at_utc must follow outcome availability"
                )
            object.__setattr__(self, "superseded_at_utc", superseded)


@dataclass(frozen=True)
class RegisteredAblationPopulation:
    """Frozen pre-outcome population/protocol identity for qualified ablation."""

    protocol_digest: str
    population_digest: str
    stopping_rule_digest: str
    source_revision: str
    registered_at_utc: datetime
    evaluation_cutoff_utc: datetime
    population_unit_ids: tuple[str, ...]
    complete: bool = True
    coverage_digest: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "protocol_digest",
            "population_digest",
            "stopping_rule_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if type(self.source_revision) is not str or _GIT_SHA.fullmatch(self.source_revision) is None:
            raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
        registered = _utc(self.registered_at_utc, "registered_at_utc")
        cutoff = _utc(self.evaluation_cutoff_utc, "evaluation_cutoff_utc")
        if cutoff < registered:
            raise ValueError("evaluation cutoff cannot precede population registration")
        object.__setattr__(self, "registered_at_utc", registered)
        object.__setattr__(self, "evaluation_cutoff_utc", cutoff)
        if type(self.population_unit_ids) is not tuple or not self.population_unit_ids:
            raise ValueError("population_unit_ids must be a non-empty immutable tuple")
        normalized = tuple(
            _identity_text(value, "population_unit_id")
            for value in self.population_unit_ids
        )
        if normalized != self.population_unit_ids:
            raise ValueError("population_unit_ids must contain canonical non-empty strings")
        if tuple(sorted(normalized)) != normalized:
            raise ValueError("population_unit_ids must be sorted canonically")
        if len(set(normalized)) != len(normalized):
            raise ValueError("population_unit_ids must be unique")
        object.__setattr__(self, "population_unit_ids", normalized)
        if type(self.complete) is not bool:
            raise TypeError("complete must be a boolean")
        if self.coverage_digest is not None:
            object.__setattr__(
                self,
                "coverage_digest",
                _digest(self.coverage_digest, "coverage_digest"),
            )


def _ablation_population_candidate_hash(
    target_component: str,
    pairs: tuple[AblationPair, ...],
    *,
    source_revision: str,
) -> str:
    """Bind one selected ablation population without trusting outcome economics."""

    target = _identity_text(target_component, "target_component")
    if type(source_revision) is not str or _GIT_SHA.fullmatch(source_revision) is None:
        raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
    material = {
        "schema_version": "ablation-population-candidate.v1",
        "source_revision": source_revision,
        "target_component": target,
        "pairs": [
            {
                "case_id": pair.full.case_id,
                "input_cutoff_utc": _canonical_utc_text(pair.full.input_cutoff_utc),
                "input_fingerprint": pair.full.input_fingerprint,
                "population_unit_id": pair.full.population_unit_id,
            }
            for pair in sorted(
                pairs,
                key=lambda item: (
                    item.full.population_unit_id,
                    item.full.case_id,
                    item.full.input_fingerprint,
                ),
            )
        ],
    }
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


def _canonical_path_value_binding(
    path: object,
    label: str,
    *,
    _path_type: type = type(Path()),
) -> tuple[str, str | None]:
    """Freeze one exact local pathname together with relative-path context."""

    if type(path) is not _path_type:
        raise ProtocolViolation(
            f"ablation qualification {label} is not canonical"
        )
    path_text = os.fspath(path)
    if type(path_text) is not str or not path_text:
        raise ProtocolViolation(
            f"ablation qualification {label} is not canonical"
        )
    cwd = None if os.path.isabs(path_text) else os.getcwd()
    return path_text, cwd


def _canonical_database_path_binding(
    value: object,
    label: str,
    *,
    path_value_binding=_canonical_path_value_binding,
) -> tuple[str, str | None]:
    """Bind one exact SQLite pathname without claiming physical-file identity."""

    state = object.__getattribute__(value, "__dict__")
    if type(state) is not dict or "path" not in state:
        raise ProtocolViolation(
            f"ablation qualification {label} database path is unavailable"
        )
    return path_value_binding(state["path"], f"{label} database path")


def _canonical_artifact_store_path_binding(
    value: object,
    *,
    path_value_binding=_canonical_path_value_binding,
) -> tuple[tuple[str, tuple[str, str | None]], ...]:
    """Freeze every pathname that selects the canonical artifact namespace."""

    state = object.__getattribute__(value, "__dict__")
    required = ("root", "objects", "manifests", "staging", "lock_path")
    if type(state) is not dict or any(name not in state for name in required):
        raise ProtocolViolation(
            "ablation qualification artifact store namespace is unavailable"
        )
    return tuple(
        (
            name,
            path_value_binding(
                state[name],
                "artifact store " + name,
            ),
        )
        for name in required
    )


def _make_ablation_authority_binding(
    path_binding=_canonical_database_path_binding,
    artifact_path_binding=_canonical_artifact_store_path_binding,
):
    """Create one process-local issuance ledger hidden behind closure-owned state."""

    lock = RLock()
    bindings: dict[int, tuple[object, ...]] = {}
    backing_paths: dict[
        int,
        tuple[
            tuple[str, str | None],
            tuple[str, str | None],
            tuple[tuple[str, tuple[str, str | None]], ...],
        ],
    ] = {}
    memory_correction_authorities: dict[int, object | None] = {}

    def register(
        authority: object,
        *,
        scientific_registry: ScientificRegistry,
        experience_memory: ExperienceMemory,
        artifact_store: ArtifactStore,
        protocol_id: str,
        protocol_hash: str,
        source_revision: str,
        causal_cutoff: datetime,
        permission_classes: tuple[str, ...],
        task: str | None,
        instrument_family: str | None,
    ) -> None:
        if type(authority) is not AblationQualificationAuthority:
            return
        authority_id = id(authority)
        registry_path_binding = path_binding(scientific_registry, "registry")
        memory_path_binding = path_binding(experience_memory, "memory")
        artifact_store_path_binding = artifact_path_binding(artifact_store)
        memory_state = object.__getattribute__(experience_memory, "__dict__")
        if (
            type(memory_state) is not dict
            or "_correction_evidence_resolver" not in memory_state
        ):
            raise ProtocolViolation(
                "ablation qualification memory correction authority is unavailable"
            )
        memory_correction_authority = memory_state["_correction_evidence_resolver"]

        def release(
            reference: object,
            *,
            authority_id: int = authority_id,
            lock: RLock = lock,
        ) -> None:
            with lock:
                current = bindings.get(authority_id)
                if current is not None and current[0] is reference:
                    bindings.pop(authority_id, None)
                    backing_paths.pop(authority_id, None)
                    memory_correction_authorities.pop(authority_id, None)

        reference = weakref_ref(authority, release)
        with lock:
            existing = bindings.get(authority_id)
            if existing is not None and existing[0]() is not None:
                raise RuntimeError(
                    "ablation qualification authority identity collision"
                )
            bindings[authority_id] = (
                reference,
                scientific_registry,
                experience_memory,
                artifact_store,
                protocol_id,
                protocol_hash,
                source_revision,
                causal_cutoff,
                permission_classes,
                task,
                instrument_family,
            )
            backing_paths[authority_id] = (
                registry_path_binding,
                memory_path_binding,
                artifact_store_path_binding,
            )
            memory_correction_authorities[authority_id] = memory_correction_authority

    def resolve(
        authority: object,
    ) -> tuple[
        ScientificRegistry,
        ExperienceMemory,
        ArtifactStore,
        str,
        str,
        str,
        datetime,
        tuple[str, ...],
        str | None,
        str | None,
    ]:
        if type(authority) is not AblationQualificationAuthority:
            raise ProtocolViolation(
                "ablation qualification authority type is invalid"
            )
        authority_id = id(authority)
        with lock:
            bound = bindings.get(authority_id)
            if bound is None or bound[0]() is not authority:
                raise ProtocolViolation(
                    "ablation qualification authority was not issued by "
                    "the canonical constructor"
                )
            bound_paths = backing_paths.get(authority_id)
            if bound_paths is None:
                raise ProtocolViolation(
                    "ablation qualification database path binding is unavailable"
                )

            scientific_registry = bound[1]
            experience_memory = bound[2]
            artifact_store = bound[3]
            protocol_id = bound[4]
            protocol_hash = bound[5]
            source_revision = bound[6]
            causal_cutoff = bound[7]
            permission_classes = bound[8]
            task = bound[9]
            instrument_family = bound[10]

            if path_binding(scientific_registry, "registry") != bound_paths[0]:
                raise ProtocolViolation(
                    "ablation qualification registry database path changed after issuance"
                )
            if path_binding(experience_memory, "memory") != bound_paths[1]:
                raise ProtocolViolation(
                    "ablation qualification memory database path changed after issuance"
                )
            if artifact_path_binding(artifact_store) != bound_paths[2]:
                raise ProtocolViolation(
                    "ablation qualification artifact store namespace changed after issuance"
                )

            memory_state = object.__getattribute__(experience_memory, "__dict__")
            if (
                authority_id not in memory_correction_authorities
                or type(memory_state) is not dict
                or "_correction_evidence_resolver" not in memory_state
                or memory_state["_correction_evidence_resolver"]
                is not memory_correction_authorities[authority_id]
            ):
                raise ProtocolViolation(
                    "ablation qualification memory correction authority changed after issuance"
                )

            try:
                current = (
                    object.__getattribute__(authority, "scientific_registry"),
                    object.__getattribute__(authority, "experience_memory"),
                    object.__getattribute__(authority, "artifact_store"),
                    object.__getattribute__(authority, "protocol_id"),
                    object.__getattribute__(authority, "protocol_hash"),
                    object.__getattribute__(authority, "source_revision"),
                    object.__getattribute__(authority, "causal_cutoff"),
                    object.__getattribute__(authority, "granted_permissions"),
                    object.__getattribute__(authority, "task"),
                    object.__getattribute__(authority, "instrument_family"),
                )
            except AttributeError as error:
                raise ProtocolViolation(
                    "ablation qualification authority state is unavailable"
                ) from error

            if current[0] is not scientific_registry:
                raise ProtocolViolation(
                    "ablation qualification authority registry binding changed after issuance"
                )
            if current[1] is not experience_memory:
                raise ProtocolViolation(
                    "ablation qualification authority memory binding changed after issuance"
                )
            if current[2] is not artifact_store:
                raise ProtocolViolation(
                    "ablation qualification authority artifact binding changed after issuance"
                )
            if current[3] != protocol_id or current[4] != protocol_hash:
                raise ProtocolViolation(
                    "ablation qualification authority protocol binding changed after issuance"
                )
            if current[5] != source_revision or current[6] != causal_cutoff:
                raise ProtocolViolation(
                    "ablation qualification authority causal/source binding changed after issuance"
                )
            permissions = current[7]
            if (
                type(permissions) is not set
                or any(type(value) is not str for value in permissions)
                or tuple(sorted(permissions)) != permission_classes
            ):
                raise ProtocolViolation(
                    "ablation qualification authority permissions changed after issuance"
                )
            if current[8] != task or current[9] != instrument_family:
                raise ProtocolViolation(
                    "ablation qualification authority query binding changed after issuance"
                )

            for owner, owner_type, label in (
                (scientific_registry, ScientificRegistry, "registry"),
                (experience_memory, ExperienceMemory, "memory"),
                (artifact_store, ArtifactStore, "artifact store"),
            ):
                if type(owner) is not owner_type:
                    raise ProtocolViolation(
                        f"ablation qualification {label} is not canonical"
                    )
                state = object.__getattribute__(owner, "__dict__")
                if type(state) is not dict:
                    raise ProtocolViolation(
                        f"ablation qualification {label} state is not canonical"
                    )
                shadowed = tuple(
                    sorted(
                        name
                        for name in state
                        if type(name) is str
                        and name in owner_type.__dict__
                        and callable(getattr(owner_type, name, None))
                    )
                )
                if shadowed:
                    raise ProtocolViolation(
                        f"ablation qualification {label} shadows canonical methods: "
                        + ", ".join(shadowed)
                    )

            return (
                scientific_registry,
                experience_memory,
                artifact_store,
                protocol_id,
                protocol_hash,
                source_revision,
                causal_cutoff,
                permission_classes,
                task,
                instrument_family,
            )

    return register, resolve


(
    _register_ablation_authority_binding,
    _resolve_ablation_authority_binding,
) = _make_ablation_authority_binding()


def _make_ablation_qualification_authority_init(register_binding):
    def __init__(
        self,
        *,
        scientific_registry: ScientificRegistry,
        experience_memory: ExperienceMemory,
        artifact_store: ArtifactStore,
        protocol_id: str,
        protocol_hash: str,
        source_revision: str,
        causal_cutoff: datetime,
        granted_permissions: set[str],
        task: str | None = None,
        instrument_family: str | None = None,
    ) -> None:
        if type(scientific_registry) is not ScientificRegistry:
            raise TypeError("scientific_registry must be exact ScientificRegistry")
        if type(experience_memory) is not ExperienceMemory:
            raise TypeError("experience_memory must be exact ExperienceMemory")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be exact ArtifactStore")
        canonical_protocol_id = _identity_text(protocol_id, "protocol_id")
        if canonical_protocol_id != protocol_id:
            raise ValueError("protocol_id must use canonical text")
        protocol_hash = _digest(protocol_hash, "protocol_hash")
        if type(source_revision) is not str or _GIT_SHA.fullmatch(source_revision) is None:
            raise ValueError(
                "source_revision must be an exact 40-character lowercase git SHA"
            )
        if type(granted_permissions) is not set or not granted_permissions:
            raise ValueError("granted_permissions must be a non-empty exact set")
        canonical_permissions: set[str] = set()
        for value in granted_permissions:
            canonical_permission = _identity_text(value, "granted_permission")
            if canonical_permission != value:
                raise ValueError(
                    "granted_permissions must contain canonical text"
                )
            canonical_permissions.add(canonical_permission)
        permission_classes = tuple(sorted(canonical_permissions))
        canonical_task = None
        if task is not None:
            canonical_task = _identity_text(task, "task")
            if canonical_task != task:
                raise ValueError("task must use canonical text")
        canonical_instrument_family = None
        if instrument_family is not None:
            canonical_instrument_family = _identity_text(
                instrument_family,
                "instrument_family",
            )
            if canonical_instrument_family != instrument_family:
                raise ValueError("instrument_family must use canonical text")
        canonical_cutoff = _utc(causal_cutoff, "causal_cutoff")

        self.scientific_registry = scientific_registry
        self.experience_memory = experience_memory
        self.artifact_store = artifact_store
        self.protocol_id = canonical_protocol_id
        self.protocol_hash = protocol_hash
        self.source_revision = source_revision
        self.causal_cutoff = canonical_cutoff
        self.granted_permissions = set(permission_classes)
        self.task = canonical_task
        self.instrument_family = canonical_instrument_family

        register_binding(
            self,
            scientific_registry=scientific_registry,
            experience_memory=experience_memory,
            artifact_store=artifact_store,
            protocol_id=canonical_protocol_id,
            protocol_hash=protocol_hash,
            source_revision=source_revision,
            causal_cutoff=canonical_cutoff,
            permission_classes=permission_classes,
            task=canonical_task,
            instrument_family=canonical_instrument_family,
        )

    return __init__


class AblationQualificationAuthority:
    """Resolve qualification evidence only through canonical persistent authorities.

    The authority never publishes evidence. It consumes an append-only scientific
    protocol, recomputes the complete ExperienceMemory population at the frozen
    causal cutoff, and reads pre-existing immutable outcome artifacts from the
    canonical ArtifactStore.
    """

    __init__ = _make_ablation_qualification_authority_init(
        _register_ablation_authority_binding
    )

    def _bound_context(
        self,
        _resolver=_resolve_ablation_authority_binding,
    ) -> tuple[
        ScientificRegistry,
        ExperienceMemory,
        ArtifactStore,
        str,
        str,
        str,
        datetime,
        tuple[str, ...],
        str | None,
        str | None,
    ]:
        return _resolver(self)

    def _load_outcome(
        self,
        reference: AblationOutcomeArtifactRef,
        *,
        population_root: str,
    ) -> CanonicalAblationOutcomeEvidence:
        (
            _science,
            _memory,
            artifact_store,
            protocol_id,
            protocol_hash,
            source_revision,
            _cutoff,
            _permissions,
            _task,
            _family,
        ) = AblationQualificationAuthority._bound_context(self)
        if type(reference) is not AblationOutcomeArtifactRef:
            raise TypeError("outcome_refs must contain AblationOutcomeArtifactRef")
        manifest, data = ArtifactStore.read_authenticated_snapshot(
            artifact_store,
            reference.artifact_id,
        )
        if manifest.get("sha256") != reference.sha256:
            raise ValueError("ablation outcome artifact digest mismatch")
        if manifest.get("media_type") != _ABLATION_OUTCOME_MEDIA_TYPE:
            raise ValueError("ablation outcome artifact media type is not qualified")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("ablation outcome artifact must be UTF-8 JSON") from error
        try:
            payload = strict_json_loads(text)
        except ValueError as error:
            raise ValueError("ablation outcome artifact JSON is invalid") from error
        required = {
            "schema_version",
            "case_id",
            "variant",
            "population_unit_id",
            "utility",
            "cost",
            "outcome_available_utc",
            "source_revision",
            "protocol_id",
            "protocol_hash",
            "population_root",
            "utility_evidence_digest",
            "cost_evidence_digest",
            "superseded_at_utc",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("ablation outcome artifact schema is not canonical")
        if payload.get("schema_version") != 1:
            raise ValueError("ablation outcome artifact schema version is unsupported")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if canonical != text:
            raise ValueError("ablation outcome artifact JSON must be canonical")
        if (
            payload.get("protocol_id") != protocol_id
            or payload.get("protocol_hash") != protocol_hash
            or payload.get("population_root") != population_root
            or payload.get("source_revision") != source_revision
        ):
            raise ValueError("ablation outcome artifact authority binding mismatch")
        superseded_raw = payload.get("superseded_at_utc")
        superseded = (
            None
            if superseded_raw is None
            else _parse_utc_text(superseded_raw, "superseded_at_utc")
        )
        return CanonicalAblationOutcomeEvidence(
            case_id=payload.get("case_id"),
            variant=payload.get("variant"),
            population_unit_id=payload.get("population_unit_id"),
            utility=payload.get("utility"),
            cost=payload.get("cost"),
            outcome_available_utc=_parse_utc_text(
                payload.get("outcome_available_utc"),
                "outcome_available_utc",
            ),
            source_revision=payload.get("source_revision"),
            utility_evidence_digest=payload.get("utility_evidence_digest"),
            cost_evidence_digest=payload.get("cost_evidence_digest"),
            evidence_digest=reference.sha256,
            superseded_at_utc=superseded,
        )

    def resolve(
        self,
        pairs: Iterable[AblationPair],
        *,
        outcome_refs: Iterable[AblationOutcomeArtifactRef],
    ) -> tuple[RegisteredAblationPopulation, tuple[CanonicalAblationOutcomeEvidence, ...]]:
        (
            scientific_registry,
            experience_memory,
            _artifact_store,
            protocol_id,
            protocol_hash,
            source_revision,
            causal_cutoff,
            permission_classes,
            task,
            instrument_family,
        ) = AblationQualificationAuthority._bound_context(self)
        if type(pairs) not in {list, tuple}:
            raise TypeError("pairs must be an exact list or tuple")
        if type(outcome_refs) not in {list, tuple}:
            raise TypeError("outcome_refs must be an exact list or tuple")
        selected = tuple(pairs)
        registration = ScientificRegistry.protocol_registration(
            scientific_registry,
            protocol_id,
        )
        if registration.protocol_hash != protocol_hash:
            raise ValueError("registered protocol hash does not match qualification binding")
        try:
            registered_raw = datetime.fromisoformat(registration.created_at)
        except (TypeError, ValueError) as error:
            raise ValueError("protocol registered_at is invalid") from error
        registered_at = _utc(registered_raw, "protocol registered_at")
        if registered_at.isoformat() != registration.created_at:
            raise ValueError("protocol registered_at is not canonical")
        snapshot = ExperienceMemory.coverage_population_snapshot(
            experience_memory,
            causal_cutoff=causal_cutoff,
            granted_permissions=set(permission_classes),
            task=task,
            instrument_family=instrument_family,
        )
        snapshot.verify_integrity()
        completeness = ScientificRegistry.trial_completeness_evidence(
            scientific_registry,
            protocol_id,
        )
        population = RegisteredAblationPopulation(
            protocol_digest=protocol_hash,
            population_digest=snapshot.root_hash,
            stopping_rule_digest=completeness.stopping_rules_hash,
            source_revision=source_revision,
            registered_at_utc=registered_at,
            evaluation_cutoff_utc=causal_cutoff,
            population_unit_ids=tuple(
                sorted(row["episode_id"] for row in snapshot.rows)
            ),
            complete=completeness.complete,
        )
        outcomes = tuple(
            AblationQualificationAuthority._load_outcome(
                self,
                reference,
                population_root=snapshot.root_hash,
            )
            for reference in outcome_refs
        )
        if selected:
            earliest_cutoff = min(pair.full.input_cutoff_utc for pair in selected)
            if registered_at > earliest_cutoff:
                # Keep the normal evaluator's fail-closed reason deterministic.
                population = RegisteredAblationPopulation(
                    protocol_digest=population.protocol_digest,
                    population_digest=population.population_digest,
                    stopping_rule_digest=population.stopping_rule_digest,
                    source_revision=population.source_revision,
                    registered_at_utc=registered_at,
                    evaluation_cutoff_utc=population.evaluation_cutoff_utc,
                    population_unit_ids=population.population_unit_ids,
                    complete=population.complete,
                )
        return population, outcomes


# Stable module-level accessor for owner-provenance composition.  Capturing the
# unbound method preserves the closure-owned issuance resolver even if a later
# module-global or class attribute is rebound.
_registered_policy_context = AblationQualificationAuthority._bound_context

_REGISTERED_ABLATION_DECISION_POLICY = ScientificRegistry.ablation_decision_policy
_REGISTERED_ABLATION_VALUE_POLICY = ScientificRegistry.ablation_value_policy

del _register_ablation_authority_binding
del _resolve_ablation_authority_binding
del _make_ablation_qualification_authority_init
del _make_ablation_authority_binding
del _canonical_artifact_store_path_binding
del _canonical_database_path_binding
del _canonical_path_value_binding


def _qualified_inconclusive(
    *,
    target_component: str,
    required_lower_bound: Decimal,
    uncertainty_multiplier: Decimal,
    reason: str,
) -> AblationEvaluation:
    return AblationEvaluation(
        target_component=target_component,
        pair_count=0,
        mean_net_incremental_value=None,
        sample_stddev=None,
        lower_bound=None,
        required_lower_bound=required_lower_bound,
        uncertainty_multiplier=uncertainty_multiplier,
        status="INCONCLUSIVE",
        reason=reason,
    )


def _registered_decision_policy(authority: object):
    registry, _memory, _artifacts, protocol_id, protocol_hash, *_rest = (
        _registered_policy_context(authority)
    )
    policy = _REGISTERED_ABLATION_DECISION_POLICY(registry, protocol_id)
    if policy.protocol_id != protocol_id or policy.protocol_hash != protocol_hash:
        raise ProtocolViolation(
            "registered ablation decision policy does not match qualification binding"
        )
    return policy


def _registered_value_policy(authority: object):
    registry, _memory, _artifacts, protocol_id, protocol_hash, *_rest = (
        _registered_policy_context(authority)
    )
    policy = _REGISTERED_ABLATION_VALUE_POLICY(registry, protocol_id)
    if policy.protocol_id != protocol_id or policy.protocol_hash != protocol_hash:
        raise ProtocolViolation(
            "registered ablation value policy does not match qualification binding"
        )
    return policy


def evaluate_qualified_incremental_value(
    target_component: str,
    pairs: Iterable[AblationPair],
    *,
    minimum_pairs: int,
    required_lower_bound: Decimal,
    uncertainty_multiplier: Decimal = Decimal("2"),
    authority: AblationQualificationAuthority | None = None,
    outcome_refs: Iterable[AblationOutcomeArtifactRef] = (),
    population: RegisteredAblationPopulation | None = None,
    canonical_outcomes: Iterable[CanonicalAblationOutcomeEvidence] = (),
) -> AblationEvaluation:
    """Evaluate only evidence-bound, pre-registered, complete matched populations.

    The existing evaluate_incremental_value() remains a descriptive/statistical
    primitive. Caller-authored dataclasses may still be inspected through this
    function for diagnostic incompatibilities, but they can never produce a
    terminal qualification. Authority-backed qualification currently fails
    closed before persistent authority resolution until canonical utility/cost
    owner evidence and one immutable historical economic cut are available.
    """

    trusted = authority is not None
    if trusted:
        if type(authority) is not AblationQualificationAuthority:
            raise TypeError(
                "authority must be the canonical AblationQualificationAuthority or None"
            )
        if type(pairs) not in {list, tuple}:
            raise TypeError(
                "pairs must be an exact list or tuple for authority-backed qualification"
            )
        if type(canonical_outcomes) not in {list, tuple}:
            raise TypeError(
                "canonical_outcomes must be an exact list or tuple for authority-backed qualification"
            )
        if type(outcome_refs) not in {list, tuple}:
            raise TypeError(
                "outcome_refs must be an exact list or tuple for authority-backed qualification"
            )
        selected_input = tuple(pairs)
        if population is not None or len(canonical_outcomes) != 0:
            raise ValueError(
                "authority-backed qualification does not accept caller-authored population/outcomes"
            )
        refs = tuple(outcome_refs)
        if any(type(reference) is not AblationOutcomeArtifactRef for reference in refs):
            raise TypeError(
                "outcome_refs must contain canonical AblationOutcomeArtifactRef values"
            )
        target = _identity_text(target_component, "target_component")
        _validate_pairs(target, selected_input)

        # Terminal statistical geometry is protocol authority. Caller thresholds
        # remain API-compatibility inputs only and cannot make qualification
        # easier or harder after the protocol is registered.
        try:
            registered_decision = _registered_decision_policy(authority)
        except (ProtocolViolation, KeyError, TypeError, ValueError):
            return _qualified_inconclusive(
                target_component=target,
                required_lower_bound=Decimal("0"),
                uncertainty_multiplier=Decimal("0"),
                reason="registered_ablation_decision_policy_unavailable",
            )
        required = _decimal(
            registered_decision.required_lower_bound,
            "registered required_lower_bound",
        )
        multiplier = _decimal(
            registered_decision.uncertainty_multiplier,
            "registered uncertainty_multiplier",
        )
        if registered_decision.decision_rule != _ABLATION_DECISION_RULE:
            return _qualified_inconclusive(
                target_component=target,
                required_lower_bound=required,
                uncertainty_multiplier=multiplier,
                reason="registered_ablation_decision_rule_unsupported",
            )
        if (
            type(registered_decision.minimum_pairs) is not int
            or registered_decision.minimum_pairs < 2
        ):
            return _qualified_inconclusive(
                target_component=target,
                required_lower_bound=required,
                uncertainty_multiplier=multiplier,
                reason="registered_ablation_decision_policy_unavailable",
            )

        try:
            registered_value = _registered_value_policy(authority)
        except (ProtocolViolation, KeyError, TypeError, ValueError):
            return _qualified_inconclusive(
                target_component=target,
                required_lower_bound=required,
                uncertainty_multiplier=multiplier,
                reason="registered_ablation_value_policy_unavailable",
            )
        if registered_value.fx_valuation_ref is not None:
            return _qualified_inconclusive(
                target_component=target,
                required_lower_bound=required,
                uncertainty_multiplier=multiplier,
                reason="registered_ablation_fx_valuation_evidence_unavailable",
            )

        # Policy selection is now independent and preregistered. Numeric utility
        # and the complete registered cost composite remain separately owned and
        # are deliberately not inferred from caller outcome fields.
        return _qualified_inconclusive(
            target_component=target,
            required_lower_bound=required,
            uncertainty_multiplier=multiplier,
            reason="canonical_utility_cost_owner_evidence_unavailable",
        )
    else:
        selected_input = tuple(pairs)
        if tuple(outcome_refs):
            raise ValueError("outcome_refs require AblationQualificationAuthority")
        if type(population) is not RegisteredAblationPopulation:
            raise TypeError(
                "population must be RegisteredAblationPopulation for diagnostic evaluation"
            )

    required = _decimal(required_lower_bound, "required_lower_bound")
    multiplier = _decimal(uncertainty_multiplier, "uncertainty_multiplier")
    if multiplier < 0:
        raise ValueError("uncertainty_multiplier must be non-negative")
    target = _identity_text(target_component, "target_component")
    selected = _validate_pairs(target, selected_input)

    def inconclusive(reason: str) -> AblationEvaluation:
        return _qualified_inconclusive(
            target_component=target,
            required_lower_bound=required,
            uncertainty_multiplier=multiplier,
            reason=reason,
        )

    if not population.complete:
        return inconclusive("incomplete_registered_population")

    selected_units = tuple(sorted(pair.full.population_unit_id for pair in selected))
    if selected_units != population.population_unit_ids:
        return inconclusive("incomplete_registered_population")

    if selected:
        earliest_cutoff = min(pair.full.input_cutoff_utc for pair in selected)
        if population.registered_at_utc > earliest_cutoff:
            return inconclusive("post_hoc_population_or_protocol_registration")

    evidence_index: dict[tuple[str, str], CanonicalAblationOutcomeEvidence] = {}
    for evidence in canonical_outcomes:
        if type(evidence) is not CanonicalAblationOutcomeEvidence:
            raise TypeError(
                "canonical_outcomes must contain CanonicalAblationOutcomeEvidence"
            )
        key = (evidence.case_id, evidence.variant)
        if key in evidence_index:
            return inconclusive("duplicate_canonical_outcome_identity")
        evidence_index[key] = evidence

    for pair in selected:
        for item in (pair.full, pair.ablated):
            evidence = evidence_index.get((item.case_id, item.variant))
            if evidence is None:
                return inconclusive("missing_canonical_outcome_evidence")
            if (
                evidence.population_unit_id != item.population_unit_id
                or evidence.source_revision != population.source_revision
            ):
                return inconclusive("canonical_outcome_identity_mismatch")
            if (
                evidence.utility != item.utility
                or evidence.cost != item.cost
                or evidence.outcome_available_utc != item.outcome_available_utc
            ):
                return inconclusive("canonical_outcome_economic_mismatch")
            if (
                evidence.superseded_at_utc is not None
                and evidence.superseded_at_utc <= population.evaluation_cutoff_utc
            ):
                return inconclusive("stale_canonical_outcome_revision")
    # Caller-authored outcome dataclasses are diagnostic evidence only.  The
    # trusted branch has already failed closed above, so no terminal PASS/FAIL
    # implementation remains reachable until the canonical operand owners land.
    return inconclusive("untrusted_caller_authored_qualification_evidence")


def build_ablation_evidence_bundle(
    target_component: str,
    pairs: Iterable[AblationPair],
    *,
    source_revision: str,
    protocol_digest: str,
    dataset_digest: str,
    minimum_pairs: int,
    required_lower_bound: Decimal,
    uncertainty_multiplier: Decimal = Decimal("2"),
) -> AblationEvidenceBundle:
    """Lock the exact causal population and result into deterministic artifact bytes."""

    if type(source_revision) is not str or _GIT_SHA.fullmatch(source_revision) is None:
        raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
    protocol = _digest(protocol_digest, "protocol_digest")
    dataset = _digest(dataset_digest, "dataset_digest")
    target = _identity_text(target_component, "target_component")
    selected = sorted(
        _validate_pairs(target, pairs),
        key=lambda pair: (pair.full.case_id, pair.full.input_fingerprint),
    )
    evaluation = evaluate_incremental_value(
        target,
        selected,
        minimum_pairs=minimum_pairs,
        required_lower_bound=required_lower_bound,
        uncertainty_multiplier=uncertainty_multiplier,
    )
    required = evaluation.required_lower_bound
    multiplier = evaluation.uncertainty_multiplier
    payload_object = {
        "dataset_digest": dataset,
        "evaluation": _evaluation_payload(evaluation),
        "evaluation_policy": {
            "decision_rule": _ABLATION_DECISION_RULE,
            "minimum_pairs": minimum_pairs,
            "reporting_projection": {
                "failure_policy": _ABLATION_REPORT_FAILURE_POLICY,
                "precision": _ABLATION_REPORT_PRECISION,
                "quantum": _canonical_decimal_text(_ABLATION_REPORT_QUANTUM),
            },
            "required_lower_bound": _canonical_decimal_text(required),
            "uncertainty_multiplier": _canonical_decimal_text(multiplier),
        },
        "pair_count": len(selected),
        "pairs": [
            {
                "ablated": _outcome_payload(pair.ablated),
                "full": _outcome_payload(pair.full),
                "target_component": pair.target_component,
            }
            for pair in selected
        ],
        "protocol_digest": protocol,
        "schema_version": "2.0.0",
        "source_revision": source_revision,
        "target_component": target,
    }
    payload = json.dumps(
        payload_object,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    content_digest = "sha256:" + sha256(payload.encode("utf-8")).hexdigest()
    return AblationEvidenceBundle(
        target_component=target,
        source_revision=source_revision,
        protocol_digest=protocol,
        dataset_digest=dataset,
        minimum_pairs=minimum_pairs,
        required_lower_bound=required,
        uncertainty_multiplier=multiplier,
        pair_count=len(selected),
        evaluation=evaluation,
        payload=payload,
        content_digest=content_digest,
    )


def verify_ablation_evidence_bundle(
    bundle: AblationEvidenceBundle,
    pairs: Iterable[AblationPair],
) -> bool:
    """Rebuild a locked bundle and fail closed on any source/population/result drift."""

    if type(bundle) is not AblationEvidenceBundle:
        raise TypeError("bundle must be AblationEvidenceBundle")
    rebuilt = build_ablation_evidence_bundle(
        bundle.target_component,
        pairs,
        source_revision=bundle.source_revision,
        protocol_digest=bundle.protocol_digest,
        dataset_digest=bundle.dataset_digest,
        minimum_pairs=bundle.minimum_pairs,
        required_lower_bound=bundle.required_lower_bound,
        uncertainty_multiplier=bundle.uncertainty_multiplier,
    )
    if rebuilt != bundle:
        raise ValueError("locked ablation evidence does not match the supplied causal population")
    return True
