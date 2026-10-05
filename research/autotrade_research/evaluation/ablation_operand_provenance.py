"""Canonical non-numeric utility-fact provenance for trusted ablation outcomes.

This module composes existing owners rather than creating a scorer. It resolves the
registered population and immutable outcome artifacts through
``AblationQualificationAuthority``, then proves each artifact's
``utility_evidence_digest`` is the exact frozen reconciled outcome fact issued by
``ExperienceMemory`` at the same causal cut.

No numeric utility, cost, net value, PASS/FAIL verdict, or trading authority is
returned here. A preregistered task-specific scorer and the complete canonical cost
owner are still required before terminal WP-63 qualification can proceed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import autotrade_research.evaluation.ablation as _ablation
from autotrade_research.evaluation.ablation import (
    AblationOutcomeArtifactRef,
    AblationPair,
    AblationQualificationAuthority,
    RegisteredAblationPopulation,
)
from autotrade_research.evaluation.ablation_outcome_binding import (
    BoundReconciledAblationOutcome,
    bind_ablation_outcome_to_reconciled_fact,
)
from autotrade_research.memory.episodes import MemoryIntegrityError


# Capture the canonical authority operations once. Later module-global rebinding is
# diagnostic state only and cannot retarget this composition path.
_RESOLVE_AUTHORITY = AblationQualificationAuthority.resolve
_RESOLVE_POLICY_CONTEXT = _ablation._registered_policy_context


@dataclass(frozen=True)
class ResolvedAblationUtilityFactProvenance:
    """Exact population + artifact-to-memory fact bindings, with no numeric score."""

    protocol_digest: str
    population_digest: str
    coverage_digest: str
    source_revision: str
    causal_cutoff: datetime
    bound_outcomes: tuple[BoundReconciledAblationOutcome, ...]

    def __post_init__(self) -> None:
        if type(self.bound_outcomes) is not tuple or not self.bound_outcomes:
            raise MemoryIntegrityError(
                "bound_outcomes must be a non-empty exact immutable tuple"
            )
        if any(
            type(item) is not BoundReconciledAblationOutcome
            for item in self.bound_outcomes
        ):
            raise MemoryIntegrityError(
                "bound_outcomes must contain exact reconciled ablation bindings"
            )
        identities = tuple(
            (item.case_id, item.variant) for item in self.bound_outcomes
        )
        if len(identities) != len(set(identities)):
            raise MemoryIntegrityError(
                "bound_outcomes contain duplicate ablation outcome identity"
            )
        if any(
            item.reconciled_fact.population_root_hash != self.population_digest
            for item in self.bound_outcomes
        ):
            raise MemoryIntegrityError(
                "bound outcome fact does not belong to the resolved population cut"
            )
        if any(
            item.reconciled_fact.causal_cutoff != self.causal_cutoff.isoformat()
            for item in self.bound_outcomes
        ):
            raise MemoryIntegrityError(
                "bound outcome fact does not belong to the resolved causal cut"
            )


def resolve_ablation_utility_fact_provenance(
    authority: AblationQualificationAuthority,
    pairs: list[AblationPair] | tuple[AblationPair, ...],
    *,
    outcome_refs: list[AblationOutcomeArtifactRef]
    | tuple[AblationOutcomeArtifactRef, ...],
) -> ResolvedAblationUtilityFactProvenance:
    """Resolve exact utility-fact provenance without trusting artifact numerics."""

    if type(authority) is not AblationQualificationAuthority:
        raise TypeError("authority must be exact AblationQualificationAuthority")
    if type(pairs) not in {list, tuple}:
        raise TypeError("pairs must be an exact list or tuple")
    if type(outcome_refs) not in {list, tuple}:
        raise TypeError("outcome_refs must be an exact list or tuple")
    if not pairs:
        raise ValueError("utility fact provenance requires a non-empty population")
    if not outcome_refs:
        raise ValueError("utility fact provenance requires immutable outcome refs")
    if any(type(item) is not AblationPair for item in pairs):
        raise TypeError("pairs must contain exact AblationPair values")
    if any(type(item) is not AblationOutcomeArtifactRef for item in outcome_refs):
        raise TypeError(
            "outcome_refs must contain exact AblationOutcomeArtifactRef values"
        )

    (
        _registry,
        memory,
        _artifacts,
        _protocol_id,
        protocol_hash,
        source_revision,
        causal_cutoff,
        permission_classes,
        task,
        instrument_family,
    ) = _RESOLVE_POLICY_CONTEXT(authority)

    population, outcomes = _RESOLVE_AUTHORITY(
        authority,
        pairs,
        outcome_refs=outcome_refs,
    )
    if type(population) is not RegisteredAblationPopulation:
        raise MemoryIntegrityError(
            "ablation authority returned non-canonical population evidence"
        )
    if population.protocol_digest != protocol_hash:
        raise MemoryIntegrityError(
            "ablation population protocol does not match authority binding"
        )
    if population.source_revision != source_revision:
        raise MemoryIntegrityError(
            "ablation population source revision does not match authority binding"
        )
    if population.evaluation_cutoff_utc != causal_cutoff:
        raise MemoryIntegrityError(
            "ablation population cutoff does not match authority binding"
        )
    if not population.complete or population.coverage_digest is None:
        raise MemoryIntegrityError(
            "ablation population is incomplete for utility provenance"
        )

    bound = tuple(
        bind_ablation_outcome_to_reconciled_fact(
            memory,
            outcome,
            causal_cutoff=causal_cutoff,
            granted_permissions=set(permission_classes),
            task=task,
            instrument_family=instrument_family,
        )
        for outcome in outcomes
    )
    expected = {
        (pair.full.case_id, "FULL")
        for pair in pairs
    } | {
        (pair.ablated.case_id, "ABLATED")
        for pair in pairs
    }
    observed = {(item.case_id, item.variant) for item in bound}
    if observed != expected:
        raise MemoryIntegrityError(
            "bound utility fact provenance does not exactly cover matched outcomes"
        )
    if any(
        item.reconciled_fact.population_root_hash != population.population_digest
        for item in bound
    ):
        raise MemoryIntegrityError(
            "bound utility fact population digest does not match registered population"
        )

    # Revalidate the hidden construction binding after persistent reads so a
    # concurrent visible-authority mutation cannot be accepted as one coherent cut.
    post_context = _RESOLVE_POLICY_CONTEXT(authority)
    if (
        post_context[1] is not memory
        or post_context[4] != protocol_hash
        or post_context[5] != source_revision
        or post_context[6] != causal_cutoff
        or post_context[7] != permission_classes
        or post_context[8] != task
        or post_context[9] != instrument_family
    ):
        raise MemoryIntegrityError(
            "ablation authority binding changed during utility provenance resolution"
        )

    return ResolvedAblationUtilityFactProvenance(
        protocol_digest=protocol_hash,
        population_digest=population.population_digest,
        coverage_digest=population.coverage_digest,
        source_revision=source_revision,
        causal_cutoff=causal_cutoff,
        bound_outcomes=bound,
    )
