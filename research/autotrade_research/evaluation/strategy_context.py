"""Section 21: context-bound strategy-family evidence.

Strategies are tools, not dogma. This research boundary freezes an ex-ante
strategy assignment per case, keeps market context explicit, and compares exact
execution-adjusted outcomes only within that context. It never promotes a
strategy or establishes economic/trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import json


def _text(value, name):
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in text")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    return value


def _utc(value, name):
    if type(value) is not datetime:
        raise TypeError(f"{name} must be an exact datetime")
    if value.tzinfo is not timezone.utc:
        raise ValueError(f"{name} must use exact UTC timezone")
    return value


def _sha(value, name):
    value = _text(value, name)
    digest = value[7:] if value.startswith("sha256:") else ""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return value


def _decimal(value, name):
    if type(value) is Decimal:
        result = value
    elif type(value) is int:
        result = Decimal(value)
    elif type(value) is str:
        try:
            result = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"{name} must be a finite exact decimal") from error
    else:
        raise TypeError(f"{name} must use Decimal, string, or integer input")
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite exact decimal")
    return result


def _frac(value):
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def _digest(value):
    def clean(item):
        if item is None or type(item) in {str, bool, int}:
            return item
        if type(item) is Decimal:
            return _frac(item)
        if type(item) is dict:
            return {
                _text(key, "digest key"): clean(item[key])
                for key in sorted(item)
            }
        if type(item) in {tuple, list}:
            return [clean(v) for v in item]
        raise TypeError(f"unsupported digest type: {type(item).__name__}")

    raw = json.dumps(
        clean(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True, order=True)
class StrategyContext:
    asset_class: str
    regime_id: str
    horizon_seconds: int

    def __post_init__(self):
        object.__setattr__(self, "asset_class", _text(self.asset_class, "asset_class"))
        object.__setattr__(self, "regime_id", _text(self.regime_id, "regime_id"))
        if type(self.horizon_seconds) is not int or self.horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be a positive exact integer")

    @property
    def key(self):
        return f"{self.asset_class}::{self.regime_id}::{self.horizon_seconds}"


@dataclass(frozen=True, slots=True)
class RegisteredStrategyCase:
    case_id: str
    strategy_family: str
    context: StrategyContext
    decision_time: datetime
    assignment_time: datetime
    input_population_hash: str

    def __post_init__(self):
        object.__setattr__(self, "case_id", _text(self.case_id, "case_id"))
        object.__setattr__(
            self, "strategy_family", _text(self.strategy_family, "strategy_family")
        )
        if type(self.context) is not StrategyContext:
            raise TypeError("context must be an exact StrategyContext")
        decision = _utc(self.decision_time, "decision_time")
        assignment = _utc(self.assignment_time, "assignment_time")
        if assignment > decision:
            raise ValueError("strategy assignment cannot occur after decision_time")
        object.__setattr__(self, "decision_time", decision)
        object.__setattr__(self, "assignment_time", assignment)
        object.__setattr__(
            self,
            "input_population_hash",
            _sha(self.input_population_hash, "input_population_hash"),
        )


@dataclass(frozen=True, slots=True)
class StrategyFamilyStudy:
    study_id: str
    registered_at: datetime
    evaluation_cutoff: datetime
    economic_unit: str
    cases: tuple[RegisteredStrategyCase, ...]
    minimum_cases_per_strategy_context: int = 1
    study_hash: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "study_id", _text(self.study_id, "study_id"))
        registered = _utc(self.registered_at, "registered_at")
        cutoff = _utc(self.evaluation_cutoff, "evaluation_cutoff")
        unit = _text(self.economic_unit, "economic_unit")
        if type(self.cases) is not tuple or not self.cases:
            raise TypeError("cases must be a non-empty exact tuple")
        if any(type(case) is not RegisteredStrategyCase for case in self.cases):
            raise TypeError("cases must contain exact RegisteredStrategyCase values")
        if tuple(sorted(self.cases, key=lambda x: x.case_id)) != self.cases:
            raise ValueError("cases must use canonical case_id ordering")
        ids = tuple(case.case_id for case in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("registered case identities must be unique")
        if registered > min(case.decision_time for case in self.cases):
            raise ValueError("study must be registered before the first strategy decision")
        if cutoff <= max(case.decision_time for case in self.cases):
            raise ValueError("evaluation_cutoff must be after every strategy decision")
        if len(self.strategy_families) < 2:
            raise ValueError("study requires at least two strategy families")
        if type(self.minimum_cases_per_strategy_context) is not int or (
            self.minimum_cases_per_strategy_context <= 0
        ):
            raise ValueError(
                "minimum_cases_per_strategy_context must be a positive exact integer"
            )
        object.__setattr__(self, "registered_at", registered)
        object.__setattr__(self, "evaluation_cutoff", cutoff)
        object.__setattr__(self, "economic_unit", unit)
        object.__setattr__(
            self,
            "study_hash",
            _digest({
                "study_id": self.study_id,
                "registered_at": registered.isoformat(),
                "evaluation_cutoff": cutoff.isoformat(),
                "economic_unit": unit,
                "minimum": self.minimum_cases_per_strategy_context,
                "cases": [{
                    "case_id": case.case_id,
                    "strategy_family": case.strategy_family,
                    "context": case.context.key,
                    "decision_time": case.decision_time.isoformat(),
                    "assignment_time": case.assignment_time.isoformat(),
                    "input_population_hash": case.input_population_hash,
                } for case in self.cases],
            }),
        )

    @classmethod
    def create(cls, *, study_id, registered_at, evaluation_cutoff, economic_unit,
               cases, minimum_cases_per_strategy_context=1):
        if type(cases) is not tuple or not cases:
            raise TypeError("cases must be a non-empty exact tuple")
        if any(type(case) is not RegisteredStrategyCase for case in cases):
            raise TypeError("cases must contain exact RegisteredStrategyCase values")
        return cls(
            study_id=study_id,
            registered_at=registered_at,
            evaluation_cutoff=evaluation_cutoff,
            economic_unit=economic_unit,
            cases=tuple(sorted(cases, key=lambda x: x.case_id)),
            minimum_cases_per_strategy_context=minimum_cases_per_strategy_context,
        )

    @property
    def strategy_families(self):
        return tuple(sorted({case.strategy_family for case in self.cases}))

    @property
    def contexts(self):
        return tuple(sorted({case.context for case in self.cases}))

    @property
    def digest(self):
        return self.study_hash


@dataclass(frozen=True, slots=True)
class StrategyCaseOutcome:
    case_id: str
    strategy_family: str
    context: StrategyContext
    decision_time: datetime
    outcome_available_at: datetime
    execution_reconciled_at: datetime
    input_population_hash: str
    execution_evidence_hash: str
    gross_value: Decimal
    execution_cost: Decimal
    net_value: Decimal
    economic_unit: str
    evidence_hash: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "case_id", _text(self.case_id, "case_id"))
        object.__setattr__(
            self, "strategy_family", _text(self.strategy_family, "strategy_family")
        )
        if type(self.context) is not StrategyContext:
            raise TypeError("context must be an exact StrategyContext")
        decision = _utc(self.decision_time, "decision_time")
        available = _utc(self.outcome_available_at, "outcome_available_at")
        reconciled = _utc(self.execution_reconciled_at, "execution_reconciled_at")
        if available <= decision:
            raise ValueError("outcome_available_at must be after decision_time")
        if reconciled < decision:
            raise ValueError("execution_reconciled_at cannot precede decision_time")
        gross = _decimal(self.gross_value, "gross_value")
        cost = _decimal(self.execution_cost, "execution_cost")
        net = _decimal(self.net_value, "net_value")
        if cost < 0:
            raise ValueError("execution_cost must be non-negative")
        if Fraction(gross) - Fraction(cost) != Fraction(net):
            raise ValueError("net_value must equal gross_value - execution_cost exactly")
        object.__setattr__(self, "decision_time", decision)
        object.__setattr__(self, "outcome_available_at", available)
        object.__setattr__(self, "execution_reconciled_at", reconciled)
        object.__setattr__(
            self,
            "input_population_hash",
            _sha(self.input_population_hash, "input_population_hash"),
        )
        object.__setattr__(
            self,
            "execution_evidence_hash",
            _sha(self.execution_evidence_hash, "execution_evidence_hash"),
        )
        unit = _text(self.economic_unit, "economic_unit")
        object.__setattr__(self, "gross_value", gross)
        object.__setattr__(self, "execution_cost", cost)
        object.__setattr__(self, "net_value", net)
        object.__setattr__(self, "economic_unit", unit)
        object.__setattr__(
            self,
            "evidence_hash",
            _digest({
                "case_id": self.case_id,
                "strategy_family": self.strategy_family,
                "context": self.context.key,
                "decision_time": decision.isoformat(),
                "outcome_available_at": available.isoformat(),
                "execution_reconciled_at": reconciled.isoformat(),
                "input_population_hash": self.input_population_hash,
                "execution_evidence_hash": self.execution_evidence_hash,
                "gross_value": gross,
                "execution_cost": cost,
                "net_value": net,
                "economic_unit": unit,
            }),
        )

    @property
    def digest(self):
        return self.evidence_hash


@dataclass(frozen=True, slots=True)
class StrategyContextSummary:
    context: StrategyContext
    strategy_family: str
    observations: int
    net_sum_numerator: int
    net_sum_denominator: int
    mean_net_numerator: int
    mean_net_denominator: int


@dataclass(frozen=True, slots=True)
class StrategyFamilyAssessment:
    status: str
    reasons: tuple[str, ...]
    summaries: tuple[StrategyContextSummary, ...]
    descriptive_best_by_context: tuple[tuple[str, tuple[str, ...]], ...]
    evidence_hash: str
    economic_edge_status: str = "UNPROVEN"
    registration_authority: str = "NOT_ESTABLISHED"
    regime_routing_authority: str = "NOT_ESTABLISHED"
    promotion_authority: str = "NOT_ESTABLISHED"
    grants_trading_authority: bool = False

    def __post_init__(self):
        if self.status not in {"COVERAGE_OK", "INCONCLUSIVE"}:
            raise ValueError("unsupported strategy-family assessment status")
        if type(self.reasons) is not tuple or type(self.summaries) is not tuple:
            raise TypeError("assessment collections must be exact tuples")
        if type(self.descriptive_best_by_context) is not tuple:
            raise TypeError("descriptive_best_by_context must be an exact tuple")
        _sha(self.evidence_hash, "evidence_hash")
        fixed = {
            "economic_edge_status": (self.economic_edge_status, "UNPROVEN"),
            "registration_authority": (self.registration_authority, "NOT_ESTABLISHED"),
            "regime_routing_authority": (
                self.regime_routing_authority, "NOT_ESTABLISHED"
            ),
            "promotion_authority": (self.promotion_authority, "NOT_ESTABLISHED"),
        }
        for name, (value, expected) in fixed.items():
            if type(value) is not str or value != expected:
                raise ValueError(f"{name} must remain {expected}")
        if type(self.grants_trading_authority) is not bool:
            raise TypeError("grants_trading_authority must be boolean")
        if self.grants_trading_authority:
            raise ValueError("strategy-family evidence cannot grant trading authority")


def assess_strategy_families(study, outcomes):
    """Compare exact net outcomes by context without creating a global winner."""

    if type(study) is not StrategyFamilyStudy:
        raise TypeError("study must be an exact StrategyFamilyStudy")
    if type(outcomes) is not tuple:
        raise TypeError("outcomes must be an exact tuple")
    if any(type(item) is not StrategyCaseOutcome for item in outcomes):
        raise TypeError("outcomes must contain exact StrategyCaseOutcome values")
    original_study_hash = _sha(study.study_hash, "study_hash")
    study = StrategyFamilyStudy(
        study_id=study.study_id,
        registered_at=study.registered_at,
        evaluation_cutoff=study.evaluation_cutoff,
        economic_unit=study.economic_unit,
        cases=study.cases,
        minimum_cases_per_strategy_context=study.minimum_cases_per_strategy_context,
    )
    if study.study_hash != original_study_hash:
        raise ValueError("study_hash changed after study construction")

    clean_outcomes = []
    for item in outcomes:
        original_evidence_hash = _sha(item.evidence_hash, "outcome evidence_hash")
        clean = StrategyCaseOutcome(
            case_id=item.case_id,
            strategy_family=item.strategy_family,
            context=item.context,
            decision_time=item.decision_time,
            outcome_available_at=item.outcome_available_at,
            execution_reconciled_at=item.execution_reconciled_at,
            input_population_hash=item.input_population_hash,
            execution_evidence_hash=item.execution_evidence_hash,
            gross_value=item.gross_value,
            execution_cost=item.execution_cost,
            net_value=item.net_value,
            economic_unit=item.economic_unit,
        )
        if clean.evidence_hash != original_evidence_hash:
            raise ValueError("outcome evidence_hash changed after construction")
        clean_outcomes.append(clean)
    outcomes = tuple(clean_outcomes)

    registered = {case.case_id: case for case in study.cases}
    if len(outcomes) != len(study.cases):
        raise ValueError("outcomes must cover every registered strategy case exactly once")
    supplied = {}
    for item in outcomes:
        if item.case_id in supplied:
            raise ValueError("duplicate strategy outcome for registered case")
        supplied[item.case_id] = item
    if set(supplied) != set(registered):
        raise ValueError("strategy outcome identities do not match registered population")

    grouped = {}
    for case_id in sorted(registered):
        case = registered[case_id]
        item = supplied[case_id]
        if (
            item.strategy_family != case.strategy_family
            or item.context != case.context
            or item.decision_time != case.decision_time
            or item.input_population_hash != case.input_population_hash
        ):
            raise ValueError(f"registered strategy assignment changed for case {case_id}")
        if item.economic_unit != study.economic_unit:
            raise ValueError("strategy outcome economic_unit does not match study")
        if item.outcome_available_at > study.evaluation_cutoff:
            raise ValueError("strategy outcome was not available by evaluation_cutoff")
        if item.execution_reconciled_at > study.evaluation_cutoff:
            raise ValueError("execution was not reconciled by evaluation_cutoff")
        grouped.setdefault((case.context, case.strategy_family), []).append(
            Fraction(item.net_value)
        )

    summaries, reasons = [], []
    for context in study.contexts:
        for family in study.strategy_families:
            values = grouped.get((context, family), [])
            if len(values) < study.minimum_cases_per_strategy_context:
                reasons.append(
                    "LEARNING.STRATEGY_CONTEXT_SAMPLE_INSUFFICIENT:"
                    f"{context.key}:{family}:{len(values)}/"
                    f"{study.minimum_cases_per_strategy_context}"
                )
                continue
            total = sum(values, Fraction())
            mean = total / len(values)
            summaries.append(StrategyContextSummary(
                context, family, len(values),
                total.numerator, total.denominator, mean.numerator, mean.denominator
            ))

    best = []
    for context in study.contexts:
        rows = [row for row in summaries if row.context == context]
        if len(rows) != len(study.strategy_families):
            continue
        means = {
            row.strategy_family: Fraction(row.mean_net_numerator, row.mean_net_denominator)
            for row in rows
        }
        top = max(means.values())
        best.append((context.key, tuple(sorted(k for k, v in means.items() if v == top))))

    reasons = tuple(sorted(set(reasons)))
    evidence_hash = _digest({
        "study": study.digest,
        "outcomes": sorted(item.digest for item in outcomes),
        "summaries": [
            (row.context.key, row.strategy_family, row.observations,
             f"{row.mean_net_numerator}/{row.mean_net_denominator}")
            for row in summaries
        ],
        "descriptive_best_by_context": best,
        "reasons": reasons,
        "authority": {
            "economic_edge": "UNPROVEN",
            "registration": "NOT_ESTABLISHED",
            "regime_routing": "NOT_ESTABLISHED",
            "promotion": "NOT_ESTABLISHED",
            "trading": False,
        },
    })
    return StrategyFamilyAssessment(
        status="COVERAGE_OK" if not reasons else "INCONCLUSIVE",
        reasons=reasons,
        summaries=tuple(summaries),
        descriptive_best_by_context=tuple(best),
        evidence_hash=evidence_hash,
    )
