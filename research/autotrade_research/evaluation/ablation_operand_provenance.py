"""Canonical non-numeric utility-fact provenance for trusted ablation outcomes.

This module composes existing owners rather than creating a scorer. It resolves the
registered population from the hidden issued authority context, authenticates the
immutable outcome artifacts through the canonical ArtifactStore, and proves every
``utility_evidence_digest`` is the exact frozen reconciled outcome fact issued by
``ExperienceMemory`` at the same causal cut.

No numeric utility, cost, net value, PASS/FAIL verdict, or trading authority is
returned here. A preregistered task-specific scorer and the complete canonical cost
owner are still required before terminal WP-63 qualification can proceed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

import autotrade_research.evaluation.ablation as _ablation
from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.evaluation.ablation import (
    AblationOutcomeArtifactRef,
    AblationPair,
    AblationQualificationAuthority,
    CanonicalAblationOutcomeEvidence,
    RegisteredAblationPopulation,
)
from autotrade_research.evaluation.ablation_outcome_binding import (
    BoundReconciledAblationOutcome,
    bind_ablation_outcome_to_reconciled_fact,
)
from autotrade_research.io.strict_json import strict_json_loads
from autotrade_research.learning.population_coverage import build_population_coverage
from autotrade_research.memory.episodes import (
    CoveragePopulationSnapshot,
    ExperienceMemory,
    MemoryIntegrityError,
)
from autotrade_research.science.registry import ScientificRegistry


# Capture the exact implementation objects that define this composition. Later
# instance or module-global rebinding cannot retarget the provenance resolver.
_RESOLVE_POLICY_CONTEXT = _ablation._registered_policy_context
_VALIDATE_PAIRS = _ablation._validate_pairs
_POPULATION_CANDIDATE_HASH = _ablation._ablation_population_candidate_hash
_PARSE_UTC_TEXT = _ablation._parse_utc_text
_CANONICAL_OUTCOME = CanonicalAblationOutcomeEvidence
_REGISTERED_POPULATION = RegisteredAblationPopulation
_COVERAGE_READ = ExperienceMemory.coverage_population_snapshot
_COVERAGE_VERIFY = CoveragePopulationSnapshot.verify_integrity
_PROTOCOL_REGISTRATION = ScientificRegistry.protocol_registration
_PROTOCOL_COMPLETENESS = ScientificRegistry.completeness
_ARTIFACT_READ = ArtifactStore.read_authenticated_snapshot
_BUILD_POPULATION_COVERAGE = build_population_coverage
_STRICT_JSON_LOADS = strict_json_loads
_OUTCOME_MEDIA_TYPE = _ablation._ABLATION_OUTCOME_MEDIA_TYPE
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_OUTCOME_FIELDS = {
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


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise MemoryIntegrityError(
            f"{name} must be an exact canonical sha256 digest"
        )
    return value


def _source_revision(value: object) -> str:
    if type(value) is not str or _GIT_SHA.fullmatch(value) is None:
        raise MemoryIntegrityError(
            "source_revision must be an exact lowercase 40-character git SHA"
        )
    return value


def _exact_utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime:
        raise MemoryIntegrityError(f"{name} must be exact datetime")
    if value.tzinfo is not timezone.utc:
        raise MemoryIntegrityError(f"{name} must use exact UTC timezone")
    return value


def _snapshot(
    memory: ExperienceMemory,
    *,
    causal_cutoff: datetime,
    permission_classes: tuple[str, ...],
    task: str | None,
    instrument_family: str | None,
) -> CoveragePopulationSnapshot:
    snapshot = _COVERAGE_READ(
        memory,
        causal_cutoff=causal_cutoff,
        granted_permissions=set(permission_classes),
        task=task,
        instrument_family=instrument_family,
    )
    if type(snapshot) is not CoveragePopulationSnapshot:
        raise MemoryIntegrityError(
            "canonical memory returned a non-canonical population snapshot"
        )
    _COVERAGE_VERIFY(snapshot)
    return snapshot


def _resolve_population(
    *,
    scientific_registry: ScientificRegistry,
    memory: ExperienceMemory,
    protocol_id: str,
    protocol_hash: str,
    source_revision: str,
    causal_cutoff: datetime,
    permission_classes: tuple[str, ...],
    task: str | None,
    instrument_family: str | None,
    pairs: tuple[AblationPair, ...],
) -> RegisteredAblationPopulation:
    if not pairs:
        raise ValueError("qualified ablation requires a non-empty matched population")
    target = pairs[0].target_component
    _VALIDATE_PAIRS(target, pairs)
    if any(pair.full.outcome_available_utc > causal_cutoff for pair in pairs):
        raise ValueError(
            "selected ablation outcome was not available by causal cutoff"
        )

    registration = _PROTOCOL_REGISTRATION(scientific_registry, protocol_id)
    if registration.protocol_hash != protocol_hash:
        raise MemoryIntegrityError(
            "registered protocol hash does not match qualification binding"
        )
    if type(registration.created_at) is not str:
        raise MemoryIntegrityError("protocol registered_at is not canonical text")
    try:
        registered_at = datetime.fromisoformat(registration.created_at)
    except ValueError as error:
        raise MemoryIntegrityError("protocol registered_at is invalid") from error
    _exact_utc(registered_at, name="protocol registered_at")
    if registered_at.isoformat() != registration.created_at:
        raise MemoryIntegrityError("protocol registered_at is not canonical")

    snapshot = _snapshot(
        memory,
        causal_cutoff=causal_cutoff,
        permission_classes=permission_classes,
        task=task,
        instrument_family=instrument_family,
    )
    selected_units = tuple(sorted(pair.full.population_unit_id for pair in pairs))
    eligible_units = tuple(sorted(row["episode_id"] for row in snapshot.rows))
    selected_set = set(selected_units)
    exclusions = {
        episode_id: "not_selected_by_registered_ablation_population"
        for episode_id in eligible_units
        if episode_id not in selected_set
    }
    coverage = _BUILD_POPULATION_COVERAGE(
        snapshot,
        candidate_hash=_POPULATION_CANDIDATE_HASH(
            target,
            pairs,
            source_revision=source_revision,
        ),
        frozen_protocol_hash=protocol_hash,
        input_snapshot_hash=snapshot.root_hash,
        causal_cutoff=causal_cutoff,
        permission_classes=permission_classes,
        included_episode_ids=selected_units,
        exclusions=exclusions,
        task=task,
        instrument_family=instrument_family,
    )
    labels_complete = all(
        complete
        for _regime, complete in coverage.included_labels_complete_by_regime
    )
    completeness = _PROTOCOL_COMPLETENESS(scientific_registry, protocol_id)
    stopping_rule_digest = completeness.get("stopping_rules_hash")
    _digest(stopping_rule_digest, name="stopping_rule_digest")
    return _REGISTERED_POPULATION(
        protocol_digest=protocol_hash,
        population_digest=snapshot.root_hash,
        stopping_rule_digest=stopping_rule_digest,
        source_revision=source_revision,
        registered_at_utc=registered_at,
        evaluation_cutoff_utc=causal_cutoff,
        population_unit_ids=coverage.included_episode_ids,
        complete=coverage.complete and labels_complete,
        coverage_digest=coverage.digest,
    )


def _load_outcome(
    artifact_store: ArtifactStore,
    reference: AblationOutcomeArtifactRef,
    *,
    protocol_id: str,
    protocol_hash: str,
    population_root: str,
    source_revision: str,
) -> CanonicalAblationOutcomeEvidence:
    if type(reference) is not AblationOutcomeArtifactRef:
        raise TypeError(
            "outcome_refs must contain exact AblationOutcomeArtifactRef values"
        )
    manifest, data = _ARTIFACT_READ(artifact_store, reference.artifact_id)
    if type(manifest) is not dict or type(data) is not bytes:
        raise MemoryIntegrityError(
            "canonical artifact read returned non-canonical snapshot values"
        )
    if manifest.get("sha256") != reference.sha256:
        raise MemoryIntegrityError("ablation outcome artifact digest mismatch")
    if manifest.get("media_type") != _OUTCOME_MEDIA_TYPE:
        raise MemoryIntegrityError(
            "ablation outcome artifact media type is not qualified"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MemoryIntegrityError(
            "ablation outcome artifact must be UTF-8 JSON"
        ) from error
    try:
        payload = _STRICT_JSON_LOADS(text)
    except ValueError as error:
        raise MemoryIntegrityError(
            "ablation outcome artifact JSON is invalid"
        ) from error
    if type(payload) is not dict or set(payload) != _OUTCOME_FIELDS:
        raise MemoryIntegrityError(
            "ablation outcome artifact schema is not canonical"
        )
    if payload.get("schema_version") != 1:
        raise MemoryIntegrityError(
            "ablation outcome artifact schema version is unsupported"
        )
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != text:
        raise MemoryIntegrityError(
            "ablation outcome artifact JSON must be canonical"
        )
    if (
        payload.get("protocol_id") != protocol_id
        or payload.get("protocol_hash") != protocol_hash
        or payload.get("population_root") != population_root
        or payload.get("source_revision") != source_revision
    ):
        raise MemoryIntegrityError(
            "ablation outcome artifact authority binding mismatch"
        )
    superseded_raw = payload.get("superseded_at_utc")
    superseded = (
        None
        if superseded_raw is None
        else _PARSE_UTC_TEXT(superseded_raw, "superseded_at_utc")
    )
    return _CANONICAL_OUTCOME(
        case_id=payload.get("case_id"),
        variant=payload.get("variant"),
        population_unit_id=payload.get("population_unit_id"),
        utility=payload.get("utility"),
        cost=payload.get("cost"),
        outcome_available_utc=_PARSE_UTC_TEXT(
            payload.get("outcome_available_utc"),
            "outcome_available_utc",
        ),
        source_revision=payload.get("source_revision"),
        utility_evidence_digest=payload.get("utility_evidence_digest"),
        cost_evidence_digest=payload.get("cost_evidence_digest"),
        evidence_digest=reference.sha256,
        superseded_at_utc=superseded,
    )


def _resolve_population_and_outcomes(
    authority: AblationQualificationAuthority,
    pairs: tuple[AblationPair, ...],
    refs: tuple[AblationOutcomeArtifactRef, ...],
):
    (
        scientific_registry,
        memory,
        artifact_store,
        protocol_id,
        protocol_hash,
        source_revision,
        causal_cutoff,
        permission_classes,
        task,
        instrument_family,
    ) = _RESOLVE_POLICY_CONTEXT(authority)
    population = _resolve_population(
        scientific_registry=scientific_registry,
        memory=memory,
        protocol_id=protocol_id,
        protocol_hash=protocol_hash,
        source_revision=source_revision,
        causal_cutoff=causal_cutoff,
        permission_classes=permission_classes,
        task=task,
        instrument_family=instrument_family,
        pairs=pairs,
    )
    snapshot = _snapshot(
        memory,
        causal_cutoff=causal_cutoff,
        permission_classes=permission_classes,
        task=task,
        instrument_family=instrument_family,
    )
    if snapshot.root_hash != population.population_digest:
        raise MemoryIntegrityError(
            "ablation population changed during utility provenance resolution"
        )
    outcomes = tuple(
        _load_outcome(
            artifact_store,
            reference,
            protocol_id=protocol_id,
            protocol_hash=protocol_hash,
            population_root=snapshot.root_hash,
            source_revision=source_revision,
        )
        for reference in refs
    )
    if any(item.outcome_available_utc > causal_cutoff for item in outcomes):
        raise MemoryIntegrityError(
            "ablation outcome artifact became available after causal cutoff"
        )
    expected = {
        (item.case_id, item.variant)
        for pair in pairs
        for item in (pair.full, pair.ablated)
    }
    observed = [(item.case_id, item.variant) for item in outcomes]
    if len(observed) != len(set(observed)):
        raise MemoryIntegrityError(
            "duplicate canonical ablation outcome artifact identity"
        )
    if set(observed) != expected:
        raise MemoryIntegrityError(
            "canonical ablation outcomes do not exactly match selected pairs"
        )
    return (
        population,
        outcomes,
        memory,
        protocol_hash,
        source_revision,
        causal_cutoff,
        permission_classes,
        task,
        instrument_family,
    )


def _provenance_material(
    *,
    protocol_digest: str,
    population_digest: str,
    coverage_digest: str,
    source_revision: str,
    causal_cutoff: datetime,
    bound_outcomes: tuple[BoundReconciledAblationOutcome, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "protocol_digest": protocol_digest,
        "population_digest": population_digest,
        "coverage_digest": coverage_digest,
        "source_revision": source_revision,
        "causal_cutoff": causal_cutoff.isoformat(),
        "bound_outcomes": [
            {
                "case_id": item.case_id,
                "variant": item.variant,
                "population_unit_id": item.population_unit_id,
                "ablation_artifact_digest": item.ablation_artifact_digest,
                "reconciled_fact_digest": item.reconciled_fact.evidence_digest,
                "effective_outcome_available_utc": (
                    item.effective_outcome_available_utc.isoformat()
                ),
            }
            for item in bound_outcomes
        ],
    }


def _provenance_digest(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolvedAblationUtilityFactProvenance:
    """Exact population + artifact-to-memory fact bindings, with no numeric score.

    Construction is only a value-integrity property. Authority consumers must call
    :func:`reverify_ablation_utility_fact_provenance` against the canonical
    qualification authority and immutable outcome references before use.
    """

    protocol_digest: str
    population_digest: str
    coverage_digest: str
    source_revision: str
    causal_cutoff: datetime
    bound_outcomes: tuple[BoundReconciledAblationOutcome, ...]
    provenance_digest: str

    def __post_init__(self) -> None:
        _digest(self.protocol_digest, name="protocol_digest")
        _digest(self.population_digest, name="population_digest")
        _digest(self.coverage_digest, name="coverage_digest")
        _source_revision(self.source_revision)
        _exact_utc(self.causal_cutoff, name="causal_cutoff")
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
        if identities != tuple(sorted(identities)):
            raise MemoryIntegrityError(
                "bound_outcomes must use canonical case/variant ordering"
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
        _digest(self.provenance_digest, name="provenance_digest")
        ResolvedAblationUtilityFactProvenance.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _provenance_material(
            protocol_digest=self.protocol_digest,
            population_digest=self.population_digest,
            coverage_digest=self.coverage_digest,
            source_revision=self.source_revision,
            causal_cutoff=self.causal_cutoff,
            bound_outcomes=self.bound_outcomes,
        )
        expected = _provenance_digest(material)
        if self.provenance_digest != expected:
            raise MemoryIntegrityError(
                "utility fact provenance digest does not match canonical material"
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
    selected = tuple(pairs)
    refs = tuple(outcome_refs)

    (
        population,
        outcomes,
        memory,
        protocol_hash,
        source_revision,
        causal_cutoff,
        permission_classes,
        task,
        instrument_family,
    ) = _resolve_population_and_outcomes(authority, selected, refs)
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
        sorted(
            (
                bind_ablation_outcome_to_reconciled_fact(
                    memory,
                    outcome,
                    causal_cutoff=causal_cutoff,
                    granted_permissions=set(permission_classes),
                    task=task,
                    instrument_family=instrument_family,
                )
                for outcome in outcomes
            ),
            key=lambda item: (item.case_id, item.variant),
        )
    )
    expected = {
        (pair.full.case_id, "FULL") for pair in selected
    } | {
        (pair.ablated.case_id, "ABLATED") for pair in selected
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

    material = _provenance_material(
        protocol_digest=protocol_hash,
        population_digest=population.population_digest,
        coverage_digest=population.coverage_digest,
        source_revision=source_revision,
        causal_cutoff=causal_cutoff,
        bound_outcomes=bound,
    )
    return ResolvedAblationUtilityFactProvenance(
        protocol_digest=protocol_hash,
        population_digest=population.population_digest,
        coverage_digest=population.coverage_digest,
        source_revision=source_revision,
        causal_cutoff=causal_cutoff,
        bound_outcomes=bound,
        provenance_digest=_provenance_digest(material),
    )


def reverify_ablation_utility_fact_provenance(
    authority: AblationQualificationAuthority,
    pairs: list[AblationPair] | tuple[AblationPair, ...],
    *,
    outcome_refs: list[AblationOutcomeArtifactRef]
    | tuple[AblationOutcomeArtifactRef, ...],
    evidence: ResolvedAblationUtilityFactProvenance,
) -> ResolvedAblationUtilityFactProvenance:
    """Re-resolve the exact canonical provenance and require identical evidence."""

    if type(evidence) is not ResolvedAblationUtilityFactProvenance:
        raise TypeError(
            "evidence must be exact ResolvedAblationUtilityFactProvenance"
        )
    ResolvedAblationUtilityFactProvenance.verify_integrity(evidence)
    resolved = resolve_ablation_utility_fact_provenance(
        authority,
        pairs,
        outcome_refs=outcome_refs,
    )
    if resolved != evidence:
        raise MemoryIntegrityError(
            "utility fact provenance does not match canonical authority evidence"
        )
    return resolved
