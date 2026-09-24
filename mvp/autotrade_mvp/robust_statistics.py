"""Deterministic statistical diagnostics for economic-edge evaluation.

These utilities do not turn a backtest into proof. They make dependence-aware paired
comparison and multiple-testing correction explicit inputs to the scientific gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import random
from typing import Mapping, Sequence


def _dec(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal/string/integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _quantile(sorted_values: Sequence[Decimal], probability: Decimal) -> Decimal:
    if not sorted_values:
        raise ValueError("quantile requires data")
    if probability < 0 or probability > 1:
        raise ValueError("probability must be within [0,1]")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * Decimal(len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - Decimal(low)
    return sorted_values[low] * (Decimal("1") - fraction) + sorted_values[high] * fraction


@dataclass(frozen=True)
class PairedBootstrapResult:
    observations: int
    block_length: int
    replications: int
    mean_delta: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    confidence: Decimal
    seed: int

    @property
    def strictly_positive(self) -> bool:
        return self.lower_bound > 0


def paired_block_bootstrap(
    candidate_returns: Sequence,
    baseline_returns: Sequence,
    *,
    block_length: int,
    replications: int,
    confidence: Decimal | str = Decimal("0.95"),
    seed: int,
) -> PairedBootstrapResult:
    if len(candidate_returns) != len(baseline_returns) or not candidate_returns:
        raise ValueError("candidate and baseline require equal non-empty paired samples")
    if block_length <= 0 or block_length > len(candidate_returns):
        raise ValueError("invalid block_length")
    if replications < 100:
        raise ValueError("at least 100 bootstrap replications are required")
    conf = _dec(confidence, name="confidence")
    if conf <= 0 or conf >= 1:
        raise ValueError("confidence must be inside (0,1)")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")

    candidate = [_dec(v, name="candidate_return") for v in candidate_returns]
    baseline = [_dec(v, name="baseline_return") for v in baseline_returns]
    deltas = [a - b for a, b in zip(candidate, baseline)]
    n = len(deltas)
    mean_delta = sum(deltas, Decimal("0")) / Decimal(n)
    starts = list(range(0, n - block_length + 1))
    rng = random.Random(seed)
    boot_means: list[Decimal] = []
    for _ in range(replications):
        sample: list[Decimal] = []
        while len(sample) < n:
            start = rng.choice(starts)
            sample.extend(deltas[start : start + block_length])
        sample = sample[:n]
        boot_means.append(sum(sample, Decimal("0")) / Decimal(n))
    boot_means.sort()
    tail = (Decimal("1") - conf) / Decimal("2")
    return PairedBootstrapResult(
        observations=n,
        block_length=block_length,
        replications=replications,
        mean_delta=mean_delta,
        lower_bound=_quantile(boot_means, tail),
        upper_bound=_quantile(boot_means, Decimal("1") - tail),
        confidence=conf,
        seed=seed,
    )


@dataclass(frozen=True)
class MultipleTestingResult:
    alpha: Decimal
    ordered_hypotheses: tuple[str, ...]
    rejected: tuple[str, ...]
    adjusted_thresholds: Mapping[str, Decimal]


def holm_bonferroni(p_values: Mapping[str, Decimal | str], *, alpha: Decimal | str = Decimal("0.05")) -> MultipleTestingResult:
    if not p_values:
        raise ValueError("p_values are required")
    a = _dec(alpha, name="alpha")
    if a <= 0 or a >= 1:
        raise ValueError("alpha must be inside (0,1)")
    normalized: list[tuple[str, Decimal]] = []
    for name, raw in p_values.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("hypothesis names are required")
        p = _dec(raw, name=f"p_value[{name}]")
        if p < 0 or p > 1:
            raise ValueError("p-values must be within [0,1]")
        normalized.append((name, p))
    normalized.sort(key=lambda item: (item[1], item[0]))
    m = len(normalized)
    rejected: list[str] = []
    thresholds: dict[str, Decimal] = {}
    still_rejecting = True
    for index, (name, p) in enumerate(normalized):
        threshold = a / Decimal(m - index)
        thresholds[name] = threshold
        if still_rejecting and p <= threshold:
            rejected.append(name)
        else:
            still_rejecting = False
    return MultipleTestingResult(
        alpha=a,
        ordered_hypotheses=tuple(name for name, _ in normalized),
        rejected=tuple(rejected),
        adjusted_thresholds=thresholds,
    )


@dataclass(frozen=True)
class EdgeEvidence:
    bootstrap: PairedBootstrapResult
    hypothesis_id: str
    multiple_testing: MultipleTestingResult
    forward_evidence_present: bool
    costs_included: bool
    leakage_check_passed: bool


@dataclass(frozen=True)
class EdgeQualification:
    qualified: bool
    blockers: tuple[str, ...]


def qualify_incremental_edge(evidence: EdgeEvidence) -> EdgeQualification:
    blockers: list[str] = []
    if not evidence.bootstrap.strictly_positive:
        blockers.append("EDGE.CONFIDENCE_INTERVAL_NOT_POSITIVE")
    if evidence.hypothesis_id not in evidence.multiple_testing.rejected:
        blockers.append("EDGE.MULTIPLE_TESTING_NOT_PASSED")
    if not evidence.forward_evidence_present:
        blockers.append("EDGE.FORWARD_EVIDENCE_MISSING")
    if not evidence.costs_included:
        blockers.append("EDGE.COSTS_NOT_INCLUDED")
    if not evidence.leakage_check_passed:
        blockers.append("EDGE.LEAKAGE_CHECK_FAILED")
    return EdgeQualification(not blockers, tuple(blockers))
