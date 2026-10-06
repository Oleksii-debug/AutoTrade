"""Owned, non-numeric WP-63 operand preflight.

This composes the already independent utility-fact owner, preregistered utility
projection rule and preregistered complete-cost rule into one re-verifiable
research-side bundle.  It intentionally stops before numeric terminal evaluation:
a task-specific canonical numeric utility projection, preregistered cost-component
attribution and projection, the complete canonical cost-composite owner and, when
configured, FX valuation evidence must still be composed at the frozen cut.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from autotrade_research.evaluation.ablation import (
    AblationOutcomeArtifactRef,
    AblationPair,
    AblationQualificationAuthority,
)
from autotrade_research.evaluation.ablation_cost_rule_provenance import (
    ResolvedAblationCostRuleProvenance,
    resolve_ablation_cost_rule_provenance,
)
from autotrade_research.evaluation.ablation_operand_provenance import (
    ResolvedAblationUtilityFactProvenance,
    resolve_ablation_utility_fact_provenance,
)
from autotrade_research.evaluation.ablation_utility_rule_provenance import (
    ResolvedAblationUtilityRuleProvenance,
    resolve_ablation_utility_rule_provenance,
)
from autotrade_research.evaluation.utility_projection import (
    UtilityProjectionRuleError,
    build_utility_projection_rule,
)
from autotrade_research.memory.episodes import MemoryIntegrityError


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_BLOCKING_REASON = "canonical_terminal_numeric_operand_evidence_unavailable"
_BASE_MISSING_TERMINAL_EVIDENCE = (
    "utility_numeric_projection",
    "registered_cost_component_attribution",
    "registered_cost_component_projection",
    "complete_cost_composite",
)


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise MemoryIntegrityError(
            f"{name} must be an exact canonical sha256 digest"
        )
    return value


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MemoryIntegrityError(f"{name} must be exact canonical non-empty text")
    return value


def _exact_utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise MemoryIntegrityError(f"{name} must use exact UTC datetime")
    return value


def _missing_terminal_evidence(fx_valuation_ref: str | None) -> tuple[str, ...]:
    missing = list(_BASE_MISSING_TERMINAL_EVIDENCE)
    if fx_valuation_ref is not None:
        missing.append("fx_valuation")
    return tuple(missing)


def _material(
    *,
    protocol_digest: str,
    population_digest: str,
    coverage_digest: str,
    source_revision: str,
    causal_cutoff: datetime,
    value_unit: str,
    fx_valuation_ref: str | None,
    utility_fact_provenance_digest: str,
    utility_rule_binding_digest: str,
    cost_rule_binding_digest: str,
    cost_components: tuple[str, ...],
    terminal_numeric_operands: bool,
    missing_terminal_evidence: tuple[str, ...],
    blocking_reason: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "protocol_digest": protocol_digest,
        "population_digest": population_digest,
        "coverage_digest": coverage_digest,
        "source_revision": source_revision,
        "causal_cutoff": causal_cutoff.isoformat(),
        "value_unit": value_unit,
        "fx_valuation_ref": fx_valuation_ref,
        "utility_fact_provenance_digest": utility_fact_provenance_digest,
        "utility_rule_binding_digest": utility_rule_binding_digest,
        "cost_rule_binding_digest": cost_rule_binding_digest,
        "cost_components": list(cost_components),
        "terminal_numeric_operands": terminal_numeric_operands,
        "missing_terminal_evidence": list(missing_terminal_evidence),
        "blocking_reason": blocking_reason,
    }


def _bundle_digest(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolvedAblationOwnedOperandPreflight:
    """Exact owned preflight state; explicitly not terminal numeric evidence."""

    protocol_digest: str
    population_digest: str
    coverage_digest: str
    source_revision: str
    causal_cutoff: datetime
    value_unit: str
    fx_valuation_ref: str | None
    utility_fact_provenance_digest: str
    utility_rule_binding_digest: str
    cost_rule_binding_digest: str
    cost_components: tuple[str, ...]
    terminal_numeric_operands: bool
    missing_terminal_evidence: tuple[str, ...]
    blocking_reason: str
    bundle_digest: str

    def __post_init__(self) -> None:
        for name in (
            "protocol_digest",
            "population_digest",
            "coverage_digest",
            "utility_fact_provenance_digest",
            "utility_rule_binding_digest",
            "cost_rule_binding_digest",
            "bundle_digest",
        ):
            _digest(getattr(self, name), name=name)
        if (
            type(self.source_revision) is not str
            or len(self.source_revision) != 40
            or any(character not in "0123456789abcdef" for character in self.source_revision)
        ):
            raise MemoryIntegrityError(
                "source_revision must be an exact lowercase 40-character git SHA"
            )
        _exact_utc(self.causal_cutoff, name="causal_cutoff")
        _text(self.value_unit, name="value_unit")
        if self.fx_valuation_ref is not None:
            _text(self.fx_valuation_ref, name="fx_valuation_ref")
        if type(self.cost_components) is not tuple or not self.cost_components:
            raise MemoryIntegrityError(
                "cost_components must be a non-empty exact tuple"
            )
        if any(
            type(item) is not str or not item or item != item.strip()
            for item in self.cost_components
        ):
            raise MemoryIntegrityError("cost_components are not canonical")
        if type(self.terminal_numeric_operands) is not bool:
            raise MemoryIntegrityError("terminal_numeric_operands must be exact bool")
        if self.terminal_numeric_operands:
            raise MemoryIntegrityError(
                "owned operand preflight cannot claim terminal numeric operands"
            )
        if type(self.missing_terminal_evidence) is not tuple:
            raise MemoryIntegrityError(
                "missing_terminal_evidence must be an exact tuple"
            )
        expected_missing = _missing_terminal_evidence(self.fx_valuation_ref)
        if self.missing_terminal_evidence != expected_missing:
            raise MemoryIntegrityError(
                "owned operand preflight missing-evidence set is not canonical"
            )
        if self.blocking_reason != _BLOCKING_REASON:
            raise MemoryIntegrityError("owned operand preflight blocker is not canonical")
        ResolvedAblationOwnedOperandPreflight.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _material(
            protocol_digest=self.protocol_digest,
            population_digest=self.population_digest,
            coverage_digest=self.coverage_digest,
            source_revision=self.source_revision,
            causal_cutoff=self.causal_cutoff,
            value_unit=self.value_unit,
            fx_valuation_ref=self.fx_valuation_ref,
            utility_fact_provenance_digest=self.utility_fact_provenance_digest,
            utility_rule_binding_digest=self.utility_rule_binding_digest,
            cost_rule_binding_digest=self.cost_rule_binding_digest,
            cost_components=self.cost_components,
            terminal_numeric_operands=self.terminal_numeric_operands,
            missing_terminal_evidence=self.missing_terminal_evidence,
            blocking_reason=self.blocking_reason,
        )
        if self.bundle_digest != _bundle_digest(material):
            raise MemoryIntegrityError(
                "owned operand preflight digest does not match canonical material"
            )


def _compose(
    facts: ResolvedAblationUtilityFactProvenance,
    utility_rule: ResolvedAblationUtilityRuleProvenance,
    cost_rule: ResolvedAblationCostRuleProvenance,
) -> ResolvedAblationOwnedOperandPreflight:
    if type(facts) is not ResolvedAblationUtilityFactProvenance:
        raise TypeError("facts must be exact ResolvedAblationUtilityFactProvenance")
    if type(utility_rule) is not ResolvedAblationUtilityRuleProvenance:
        raise TypeError("utility_rule must be exact ResolvedAblationUtilityRuleProvenance")
    if type(cost_rule) is not ResolvedAblationCostRuleProvenance:
        raise TypeError("cost_rule must be exact ResolvedAblationCostRuleProvenance")
    ResolvedAblationUtilityFactProvenance.verify_integrity(facts)
    ResolvedAblationUtilityRuleProvenance.verify_integrity(utility_rule)
    ResolvedAblationCostRuleProvenance.verify_integrity(cost_rule)
    if not (
        facts.protocol_digest
        == utility_rule.protocol_digest
        == cost_rule.protocol_digest
    ):
        raise MemoryIntegrityError(
            "owned operand provenance does not share one protocol digest"
        )
    if utility_rule.utility_fact_provenance_digest != facts.provenance_digest:
        raise MemoryIntegrityError(
            "utility rule does not bind the resolved utility facts"
        )
    try:
        installed_utility_rule = build_utility_projection_rule(utility_rule.value_unit)
    except UtilityProjectionRuleError as error:
        raise MemoryIntegrityError(
            "registered utility projection rule is not source-owned"
        ) from error
    if (
        utility_rule.projection_artifact_id != installed_utility_rule.artifact_id
        or utility_rule.projection_artifact_digest != installed_utility_rule.sha256
    ):
        raise MemoryIntegrityError(
            "registered utility projection rule identity is not source-owned"
        )
    if utility_rule.value_unit != cost_rule.value_unit:
        raise MemoryIntegrityError(
            "utility and cost rules do not share one registered value unit"
        )
    if utility_rule.fx_valuation_ref != cost_rule.fx_valuation_ref:
        raise MemoryIntegrityError(
            "utility and cost rules do not share one FX dependency"
        )
    missing = _missing_terminal_evidence(utility_rule.fx_valuation_ref)
    material = _material(
        protocol_digest=facts.protocol_digest,
        population_digest=facts.population_digest,
        coverage_digest=facts.coverage_digest,
        source_revision=facts.source_revision,
        causal_cutoff=facts.causal_cutoff,
        value_unit=utility_rule.value_unit,
        fx_valuation_ref=utility_rule.fx_valuation_ref,
        utility_fact_provenance_digest=facts.provenance_digest,
        utility_rule_binding_digest=utility_rule.binding_digest,
        cost_rule_binding_digest=cost_rule.binding_digest,
        cost_components=cost_rule.cost_components,
        terminal_numeric_operands=False,
        missing_terminal_evidence=missing,
        blocking_reason=_BLOCKING_REASON,
    )
    return ResolvedAblationOwnedOperandPreflight(
        protocol_digest=facts.protocol_digest,
        population_digest=facts.population_digest,
        coverage_digest=facts.coverage_digest,
        source_revision=facts.source_revision,
        causal_cutoff=facts.causal_cutoff,
        value_unit=utility_rule.value_unit,
        fx_valuation_ref=utility_rule.fx_valuation_ref,
        utility_fact_provenance_digest=facts.provenance_digest,
        utility_rule_binding_digest=utility_rule.binding_digest,
        cost_rule_binding_digest=cost_rule.binding_digest,
        cost_components=cost_rule.cost_components,
        terminal_numeric_operands=False,
        missing_terminal_evidence=missing,
        blocking_reason=_BLOCKING_REASON,
        bundle_digest=_bundle_digest(material),
    )


def resolve_ablation_owned_operand_preflight(
    authority: AblationQualificationAuthority,
    pairs: list[AblationPair] | tuple[AblationPair, ...],
    *,
    outcome_refs: list[AblationOutcomeArtifactRef]
    | tuple[AblationOutcomeArtifactRef, ...],
) -> ResolvedAblationOwnedOperandPreflight:
    """Resolve every currently owned research-side operand precursor."""

    facts = resolve_ablation_utility_fact_provenance(
        authority,
        pairs,
        outcome_refs=outcome_refs,
    )
    utility_rule = resolve_ablation_utility_rule_provenance(
        authority,
        pairs,
        outcome_refs=outcome_refs,
        utility_facts=facts,
    )
    cost_rule = resolve_ablation_cost_rule_provenance(authority)
    return _compose(facts, utility_rule, cost_rule)


def reverify_ablation_owned_operand_preflight(
    authority: AblationQualificationAuthority,
    pairs: list[AblationPair] | tuple[AblationPair, ...],
    *,
    outcome_refs: list[AblationOutcomeArtifactRef]
    | tuple[AblationOutcomeArtifactRef, ...],
    evidence: ResolvedAblationOwnedOperandPreflight,
) -> ResolvedAblationOwnedOperandPreflight:
    if type(evidence) is not ResolvedAblationOwnedOperandPreflight:
        raise TypeError(
            "evidence must be exact ResolvedAblationOwnedOperandPreflight"
        )
    ResolvedAblationOwnedOperandPreflight.verify_integrity(evidence)
    resolved = resolve_ablation_owned_operand_preflight(
        authority,
        pairs,
        outcome_refs=outcome_refs,
    )
    if resolved != evidence:
        raise MemoryIntegrityError(
            "owned operand preflight does not match canonical authority evidence"
        )
    return resolved
