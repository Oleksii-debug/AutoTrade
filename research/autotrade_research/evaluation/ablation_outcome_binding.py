"""Bridge immutable ablation outcomes to canonical reconciled outcome facts.

The bridge deliberately does not expose or validate the artifact's numeric utility.
It proves only that the artifact points at one ExperienceMemory-owned outcome fact
at the selected causal cut.  A registered task-specific scorer remains required
before that fact can become numeric utility authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from research.autotrade_research.evaluation.ablation import (
    CanonicalAblationOutcomeEvidence,
)
from research.autotrade_research.memory.episodes import (
    ExperienceMemory,
    MemoryIntegrityError,
)
from research.autotrade_research.memory.reconciled_outcome import (
    ReconciledOutcomeFactEvidence,
    resolve_reconciled_outcome_fact,
    reverify_reconciled_outcome_fact,
)


def _stored_utc(value: object, *, name: str) -> datetime:
    if type(value) is not str:
        raise MemoryIntegrityError(f"{name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MemoryIntegrityError(f"{name} is not a valid timestamp") from error
    if parsed.tzinfo is None:
        raise MemoryIntegrityError(f"{name} must be timezone-aware")
    normalized = parsed.astimezone(timezone.utc)
    if normalized.isoformat() != value:
        raise MemoryIntegrityError(f"{name} must be canonical UTC text")
    return normalized


@dataclass(frozen=True)
class BoundReconciledAblationOutcome:
    """Non-numeric binding between one ablation artifact and one memory fact."""

    case_id: str
    variant: str
    population_unit_id: str
    ablation_artifact_digest: str
    reconciled_fact: ReconciledOutcomeFactEvidence
    effective_outcome_available_utc: datetime

    def __post_init__(self) -> None:
        if type(self.reconciled_fact) is not ReconciledOutcomeFactEvidence:
            raise TypeError("reconciled_fact must be exact ReconciledOutcomeFactEvidence")
        self.reconciled_fact.verify_integrity()
        if self.population_unit_id != self.reconciled_fact.episode_id:
            raise MemoryIntegrityError(
                "ablation population unit does not match reconciled outcome episode"
            )
        if type(self.effective_outcome_available_utc) is not datetime:
            raise TypeError("effective_outcome_available_utc must be exact datetime")
        if self.effective_outcome_available_utc.tzinfo is None:
            raise ValueError("effective_outcome_available_utc must be timezone-aware")
        if self.effective_outcome_available_utc.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("effective_outcome_available_utc must be UTC")


def bind_ablation_outcome_to_reconciled_fact(
    memory: ExperienceMemory,
    outcome: CanonicalAblationOutcomeEvidence,
    *,
    causal_cutoff: datetime,
    granted_permissions: set[str],
    task: str | None = None,
    instrument_family: str | None = None,
) -> BoundReconciledAblationOutcome:
    """Bind an ablation artifact to independently re-resolved outcome history.

    ``outcome.utility`` is intentionally never read.  The artifact's
    ``utility_evidence_digest`` must instead equal the digest independently
    recomputed from ExperienceMemory for the same population unit and frozen cut.
    """

    if type(memory) is not ExperienceMemory:
        raise TypeError("memory must be exact ExperienceMemory")
    if type(outcome) is not CanonicalAblationOutcomeEvidence:
        raise TypeError("outcome must be exact CanonicalAblationOutcomeEvidence")
    if type(causal_cutoff) is not datetime:
        raise TypeError("causal_cutoff must be exact datetime")
    cutoff = causal_cutoff.astimezone(timezone.utc)
    if outcome.superseded_at_utc is not None and outcome.superseded_at_utc <= cutoff:
        raise MemoryIntegrityError(
            "superseded ablation outcome cannot bind at the selected causal cut"
        )

    fact = resolve_reconciled_outcome_fact(
        memory,
        episode_id=outcome.population_unit_id,
        causal_cutoff=cutoff,
        granted_permissions=granted_permissions,
        task=task,
        instrument_family=instrument_family,
    )
    reverify_reconciled_outcome_fact(memory, fact)
    if outcome.utility_evidence_digest != fact.evidence_digest:
        raise MemoryIntegrityError(
            "ablation utility evidence digest does not match canonical reconciled outcome fact"
        )

    snapshot = memory.coverage_population_snapshot(
        causal_cutoff=cutoff,
        granted_permissions=granted_permissions,
        task=task,
        instrument_family=instrument_family,
    )
    snapshot.verify_integrity()
    if snapshot.root_hash != fact.population_root_hash:
        raise MemoryIntegrityError(
            "reconciled outcome population changed during ablation binding"
        )
    rows = tuple(
        row for row in snapshot.rows if row.get("episode_id") == fact.episode_id
    )
    if len(rows) != 1:
        raise MemoryIntegrityError(
            "reconciled outcome population unit is not unique at binding cut"
        )
    row = rows[0]
    available = _stored_utc(row.get("created_at"), name="episode created_at")
    lineage = row.get("correction_lineage")
    if not isinstance(lineage, tuple):
        raise MemoryIntegrityError("canonical correction lineage is malformed")
    for correction in lineage:
        if not isinstance(correction, Mapping):
            raise MemoryIntegrityError("canonical correction lineage is malformed")
        fields = correction.get("supersedes_fields")
        if not isinstance(fields, tuple):
            raise MemoryIntegrityError("canonical correction supersedes_fields are malformed")
        if "outcome" in fields:
            correction_available = _stored_utc(
                correction.get("available_at"),
                name="outcome correction available_at",
            )
            if correction_available > available:
                available = correction_available

    if outcome.outcome_available_utc < available:
        raise MemoryIntegrityError(
            "ablation outcome claims availability before canonical reconciled outcome fact"
        )
    if outcome.outcome_available_utc > cutoff:
        raise MemoryIntegrityError(
            "ablation outcome was not available by selected causal cut"
        )

    return BoundReconciledAblationOutcome(
        case_id=outcome.case_id,
        variant=outcome.variant,
        population_unit_id=outcome.population_unit_id,
        ablation_artifact_digest=outcome.evidence_digest,
        reconciled_fact=fact,
        effective_outcome_available_utc=available,
    )
