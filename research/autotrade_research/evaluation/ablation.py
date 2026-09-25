"""Evidence-bound matched-input causal ablation primitives.

This module measures marginal contribution on matched causal shadow cases. It
never routes models, grants trading authority, or treats the result as proof of
economic edge by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
import re
from typing import Iterable

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.io.strict_json import strict_json_loads
from autotrade_research.memory.episodes import ExperienceMemory
from autotrade_research.science.registry import ScientificRegistry


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _decimal(value: Decimal | int | str, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must use Decimal, string or integer input")
    try:
        number = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be canonical sha256:<64 lowercase hex>")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


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
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
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
            if (
                not isinstance(self.syndication_group, str)
                or not self.syndication_group.strip()
            ):
                raise ValueError(
                    "syndication_group must be None or a non-empty string"
                )
            object.__setattr__(
                self,
                "syndication_group",
                self.syndication_group.strip(),
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
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        object.__setattr__(self, "case_id", self.case_id.strip())
        population_unit = self.population_unit_id
        if population_unit is None:
            population_unit = self.case_id
        if not isinstance(population_unit, str) or not population_unit.strip():
            raise ValueError("population_unit_id must be a non-empty string")
        object.__setattr__(self, "population_unit_id", population_unit.strip())
        object.__setattr__(
            self,
            "input_fingerprint",
            _digest(self.input_fingerprint, "input_fingerprint"),
        )
        if self.variant not in {"FULL", "ABLATED"}:
            raise ValueError("variant must be FULL or ABLATED")
        if (
            not isinstance(self.elapsed_ms, int)
            or isinstance(self.elapsed_ms, bool)
            or not isinstance(self.deadline_ms, int)
            or isinstance(self.deadline_ms, bool)
        ):
            raise TypeError("elapsed_ms and deadline_ms must be integers")
        if self.elapsed_ms < 0 or self.deadline_ms <= 0:
            raise ValueError("elapsed_ms must be non-negative and deadline_ms positive")
        if not isinstance(self.components, tuple):
            raise TypeError("components must be an immutable tuple")
        if any(not isinstance(item, str) or not item.strip() for item in self.components):
            raise ValueError("component identities must be non-empty strings")
        normalized_components = tuple(item.strip() for item in self.components)
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

        if not isinstance(self.input_evidence, tuple):
            raise TypeError("input_evidence must be an immutable tuple")
        if any(
            not isinstance(item, CausalInputEvidence)
            for item in self.input_evidence
        ):
            raise TypeError(
                "input_evidence entries must be CausalInputEvidence"
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
        if not isinstance(self.target_component, str) or not self.target_component.strip():
            raise ValueError("target_component is required")
        object.__setattr__(self, "target_component", self.target_component.strip())
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
        return self.full.utility - self.ablated.utility

    @property
    def cost_delta(self) -> Decimal:
        return self.full.cost - self.ablated.cost

    @property
    def net_value_delta(self) -> Decimal | None:
        if not self.utility_comparable:
            return None
        return (self.full.utility - self.full.cost) - (
            self.ablated.utility - self.ablated.cost
        )

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
    mean_cost_delta: Decimal
    mean_latency_delta_ms: Decimal
    status: str


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


def _validate_pairs(target_component: str, pairs: Iterable[AblationPair]) -> list[AblationPair]:
    if not isinstance(target_component, str) or not target_component.strip():
        raise ValueError("target_component is required")
    target_component = target_component.strip()
    selected = list(pairs)
    if any(pair.target_component != target_component for pair in selected):
        raise ValueError("all pairs must target the requested component")

    seen_case_ids: set[str] = set()
    seen_input_fingerprints: set[str] = set()
    seen_population_units: set[str] = set()
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


def summarize_ablation(target_component: str, pairs: Iterable[AblationPair]) -> AblationSummary:
    target_component = target_component.strip() if isinstance(target_component, str) else target_component
    selected = _validate_pairs(target_component, pairs)
    comparable = [pair for pair in selected if pair.utility_comparable]

    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN

        utility_deltas = [pair.utility_delta for pair in comparable]
        concrete_utility = [value for value in utility_deltas if value is not None]

        def mean(values: list[Decimal]) -> Decimal | None:
            if not values:
                return None
            return sum(values, Decimal("0")) / Decimal(len(values))

        cost_values = [pair.cost_delta for pair in selected]
        latency_values = [Decimal(pair.latency_delta_ms) for pair in selected]
        mean_cost = mean(cost_values) or Decimal("0")
        mean_latency = mean(latency_values) or Decimal("0")
        mean_utility = mean(concrete_utility)

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
        mean_utility_delta=mean_utility,
        mean_cost_delta=mean_cost,
        mean_latency_delta_ms=mean_latency,
        status="DESCRIPTIVE_ONLY" if concrete_utility else "INCONCLUSIVE",
    )


def evaluate_incremental_value(
    target_component: str,
    pairs: Iterable[AblationPair],
    *,
    minimum_pairs: int,
    required_lower_bound: Decimal,
    uncertainty_multiplier: Decimal = Decimal("2"),
) -> AblationEvaluation:
    """Measure conservative net marginal value without granting promotion authority."""

    if not isinstance(minimum_pairs, int) or isinstance(minimum_pairs, bool) or minimum_pairs < 2:
        raise ValueError("minimum_pairs must be an integer >= 2")
    required = _decimal(required_lower_bound, "required_lower_bound")
    multiplier = _decimal(uncertainty_multiplier, "uncertainty_multiplier")
    if multiplier < 0:
        raise ValueError("uncertainty_multiplier must be non-negative")

    target = target_component.strip() if isinstance(target_component, str) else target_component
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

    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        values = [
            pair.net_value_delta
            for pair in selected
            if pair.utility_comparable
        ]
        concrete = [value for value in values if value is not None]
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

    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        count = Decimal(len(concrete))
        mean = sum(concrete, Decimal("0")) / count
        squared = sum(
            ((value - mean) * (value - mean) for value in concrete),
            Decimal("0"),
        )
        variance = squared / Decimal(len(concrete) - 1)
        stddev = variance.sqrt()
        standard_error = stddev / count.sqrt()
        lower = mean - multiplier * standard_error
    return AblationEvaluation(
        target_component=target,
        pair_count=len(concrete),
        mean_net_incremental_value=mean,
        sample_stddev=stddev,
        lower_bound=lower,
        required_lower_bound=required,
        uncertainty_multiplier=multiplier,
        status="PASS" if lower >= required else "FAIL",
        reason="matched_causal_ablation_net_of_cost",
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
        if not isinstance(self.target_component, str) or not self.target_component.strip():
            raise ValueError("target_component is required")
        object.__setattr__(self, "target_component", self.target_component.strip())
        if not isinstance(self.source_revision, str) or _GIT_SHA.fullmatch(self.source_revision) is None:
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
        if (
            not isinstance(self.minimum_pairs, int)
            or isinstance(self.minimum_pairs, bool)
            or self.minimum_pairs < 2
        ):
            raise ValueError("minimum_pairs must be an integer >= 2")
        required = _decimal(self.required_lower_bound, "required_lower_bound")
        multiplier = _decimal(self.uncertainty_multiplier, "uncertainty_multiplier")
        if multiplier < 0:
            raise ValueError("uncertainty_multiplier must be non-negative")
        object.__setattr__(self, "required_lower_bound", required)
        object.__setattr__(self, "uncertainty_multiplier", multiplier)
        if (
            not isinstance(self.pair_count, int)
            or isinstance(self.pair_count, bool)
            or self.pair_count < 0
        ):
            raise ValueError("pair_count must be a non-negative integer")
        if not isinstance(self.evaluation, AblationEvaluation):
            raise TypeError("evaluation must be AblationEvaluation")
        if self.evaluation.target_component != self.target_component:
            raise ValueError("evaluation target_component must match the bundle")
        if self.evaluation.required_lower_bound != required:
            raise ValueError("evaluation required_lower_bound must match the bundle")
        if self.evaluation.uncertainty_multiplier != multiplier:
            raise ValueError("evaluation uncertainty_multiplier must match the bundle")
        if self.evaluation.pair_count > self.pair_count:
            raise ValueError("evaluation pair_count cannot exceed locked pair_count")
        if not isinstance(self.payload, str) or not self.payload:
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
            decoded.get("schema_version") != "1.0.0"
            or decoded.get("target_component") != self.target_component
            or decoded.get("source_revision") != self.source_revision
            or decoded.get("protocol_digest") != self.protocol_digest
            or decoded.get("dataset_digest") != self.dataset_digest
            or decoded.get("pair_count") != self.pair_count
            or policy.get("minimum_pairs") != self.minimum_pairs
            or policy.get("required_lower_bound") != _canonical_decimal_text(required)
            or policy.get("uncertainty_multiplier") != _canonical_decimal_text(multiplier)
        ):
            raise ValueError("payload metadata does not match bundle metadata")


def _canonical_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


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
        "lower_bound": _canonical_decimal_text(item.lower_bound),
        "mean_net_incremental_value": _canonical_decimal_text(
            item.mean_net_incremental_value
        ),
        "pair_count": item.pair_count,
        "reason": item.reason,
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

        try:
            canonical_id = str(UUID(self.artifact_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("artifact_id must be a canonical UUID") from error
        if canonical_id != self.artifact_id:
            raise ValueError("artifact_id must use canonical UUID text")
        object.__setattr__(self, "sha256", _digest(self.sha256, "sha256"))


_ABLATION_OUTCOME_MEDIA_TYPE = "application/vnd.autotrade.ablation-outcome+json"


def _parse_utc_text(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
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
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
            canonical = value.strip()
            if canonical != value:
                raise ValueError(f"{name} must use canonical text")
            object.__setattr__(self, name, canonical)
        if self.variant not in {"FULL", "ABLATED"}:
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
        if not isinstance(self.source_revision, str) or _GIT_SHA.fullmatch(self.source_revision) is None:
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

    def __post_init__(self) -> None:
        for name in (
            "protocol_digest",
            "population_digest",
            "stopping_rule_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if not isinstance(self.source_revision, str) or _GIT_SHA.fullmatch(self.source_revision) is None:
            raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
        registered = _utc(self.registered_at_utc, "registered_at_utc")
        cutoff = _utc(self.evaluation_cutoff_utc, "evaluation_cutoff_utc")
        if cutoff < registered:
            raise ValueError("evaluation cutoff cannot precede population registration")
        object.__setattr__(self, "registered_at_utc", registered)
        object.__setattr__(self, "evaluation_cutoff_utc", cutoff)
        if not isinstance(self.population_unit_ids, tuple) or not self.population_unit_ids:
            raise ValueError("population_unit_ids must be a non-empty immutable tuple")
        normalized = tuple(
            value.strip()
            for value in self.population_unit_ids
            if isinstance(value, str) and value.strip()
        )
        if len(normalized) != len(self.population_unit_ids):
            raise ValueError("population_unit_ids must contain canonical non-empty strings")
        if tuple(sorted(normalized)) != normalized:
            raise ValueError("population_unit_ids must be sorted canonically")
        if len(set(normalized)) != len(normalized):
            raise ValueError("population_unit_ids must be unique")
        object.__setattr__(self, "population_unit_ids", normalized)
        if type(self.complete) is not bool:
            raise TypeError("complete must be a boolean")


class AblationQualificationAuthority:
    """Resolve qualification evidence only through canonical persistent authorities.

    The authority never publishes evidence. It consumes an append-only scientific
    protocol, recomputes the complete ExperienceMemory population at the frozen
    causal cutoff, and reads pre-existing immutable outcome artifacts from the
    canonical ArtifactStore.
    """

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
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        if not isinstance(experience_memory, ExperienceMemory):
            raise TypeError("experience_memory must be ExperienceMemory")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore")
        if not isinstance(protocol_id, str) or not protocol_id.strip():
            raise ValueError("protocol_id is required")
        if not isinstance(protocol_hash, str):
            raise TypeError("protocol_hash must be text")
        if not isinstance(source_revision, str) or _GIT_SHA.fullmatch(source_revision) is None:
            raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
        if not isinstance(granted_permissions, set) or not granted_permissions:
            raise ValueError("granted_permissions must be a non-empty set")
        self.scientific_registry = scientific_registry
        self.experience_memory = experience_memory
        self.artifact_store = artifact_store
        self.protocol_id = protocol_id.strip()
        self.protocol_hash = _digest(protocol_hash, "protocol_hash")
        self.source_revision = source_revision
        self.causal_cutoff = _utc(causal_cutoff, "causal_cutoff")
        self.granted_permissions = set(granted_permissions)
        self.task = task
        self.instrument_family = instrument_family

    def _load_outcome(
        self,
        reference: AblationOutcomeArtifactRef,
        *,
        population_root: str,
    ) -> CanonicalAblationOutcomeEvidence:
        if not isinstance(reference, AblationOutcomeArtifactRef):
            raise TypeError("outcome_refs must contain AblationOutcomeArtifactRef")
        manifest = self.artifact_store.load_manifest(reference.artifact_id)
        if manifest.get("sha256") != reference.sha256:
            raise ValueError("ablation outcome artifact digest mismatch")
        if manifest.get("media_type") != _ABLATION_OUTCOME_MEDIA_TYPE:
            raise ValueError("ablation outcome artifact media type is not qualified")
        data = self.artifact_store.read_bytes(reference.artifact_id)
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
            payload.get("protocol_id") != self.protocol_id
            or payload.get("protocol_hash") != self.protocol_hash
            or payload.get("population_root") != population_root
            or payload.get("source_revision") != self.source_revision
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
        selected = tuple(pairs)
        registration = self.scientific_registry.protocol_registration(self.protocol_id)
        if registration.protocol_hash != self.protocol_hash:
            raise ValueError("registered protocol hash does not match qualification binding")
        registered_at = _parse_utc_text(
            registration.created_at,
            "protocol registered_at",
        )
        snapshot = self.experience_memory.coverage_population_snapshot(
            causal_cutoff=self.causal_cutoff,
            granted_permissions=set(self.granted_permissions),
            task=self.task,
            instrument_family=self.instrument_family,
        )
        snapshot.verify_integrity()
        completeness = self.scientific_registry.completeness(self.protocol_id)
        population = RegisteredAblationPopulation(
            protocol_digest=self.protocol_hash,
            population_digest=snapshot.root_hash,
            stopping_rule_digest=completeness["stopping_rules_hash"],
            source_revision=self.source_revision,
            registered_at_utc=registered_at,
            evaluation_cutoff_utc=self.causal_cutoff,
            population_unit_ids=tuple(
                sorted(row["episode_id"] for row in snapshot.rows)
            ),
            complete=True,
        )
        outcomes = tuple(
            self._load_outcome(reference, population_root=snapshot.root_hash)
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
                    complete=True,
                )
        return population, outcomes


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


def evaluate_qualified_incremental_value(
    target_component: str,
    pairs: Iterable[AblationPair],
    *,
    population: RegisteredAblationPopulation,
    canonical_outcomes: Iterable[CanonicalAblationOutcomeEvidence],
    minimum_pairs: int,
    required_lower_bound: Decimal,
    uncertainty_multiplier: Decimal = Decimal("2"),
) -> AblationEvaluation:
    """Evaluate only evidence-bound, pre-registered, complete matched populations.

    The existing evaluate_incremental_value() remains a descriptive/statistical
    primitive. This qualification path cannot PASS from caller-authored utility
    or cost alone: every scored outcome must match immutable canonical evidence,
    the population must be complete and registered before causal inputs, and
    stale pre-cutoff outcome revisions fail closed.
    """

    if not isinstance(population, RegisteredAblationPopulation):
        raise TypeError("population must be RegisteredAblationPopulation")
    required = _decimal(required_lower_bound, "required_lower_bound")
    multiplier = _decimal(uncertainty_multiplier, "uncertainty_multiplier")
    if multiplier < 0:
        raise ValueError("uncertainty_multiplier must be non-negative")
    target = target_component.strip() if isinstance(target_component, str) else target_component
    selected = _validate_pairs(target, pairs)

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
        if not isinstance(evidence, CanonicalAblationOutcomeEvidence):
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
            if evidence.outcome_available_utc > population.evaluation_cutoff_utc:
                return inconclusive("canonical_outcome_not_mature_at_cutoff")
            if (
                evidence.superseded_at_utc is not None
                and evidence.superseded_at_utc <= population.evaluation_cutoff_utc
            ):
                return inconclusive("stale_canonical_outcome_revision")

    base = evaluate_incremental_value(
        target,
        selected,
        minimum_pairs=minimum_pairs,
        required_lower_bound=required,
        uncertainty_multiplier=multiplier,
    )
    if base.status == "INCONCLUSIVE":
        return base
    return AblationEvaluation(
        target_component=base.target_component,
        pair_count=base.pair_count,
        mean_net_incremental_value=base.mean_net_incremental_value,
        sample_stddev=base.sample_stddev,
        lower_bound=base.lower_bound,
        required_lower_bound=base.required_lower_bound,
        uncertainty_multiplier=base.uncertainty_multiplier,
        status=base.status,
        reason="qualified_registered_canonical_ablation_net_of_cost",
    )


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

    if not isinstance(source_revision, str) or _GIT_SHA.fullmatch(source_revision) is None:
        raise ValueError("source_revision must be an exact 40-character lowercase git SHA")
    protocol = _digest(protocol_digest, "protocol_digest")
    dataset = _digest(dataset_digest, "dataset_digest")
    target = target_component.strip() if isinstance(target_component, str) else target_component
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
            "minimum_pairs": minimum_pairs,
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
        "schema_version": "1.0.0",
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

    if not isinstance(bundle, AblationEvidenceBundle):
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
