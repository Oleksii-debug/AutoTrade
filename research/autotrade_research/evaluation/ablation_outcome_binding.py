"""Bridge immutable ablation outcomes to canonical reconciled outcome facts.

The bridge deliberately does not expose or validate the artifact's numeric utility.
It proves only that the artifact points at one ExperienceMemory-owned outcome fact
at the selected causal cut. A registered task-specific scorer remains required
before that fact can become numeric utility authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import UUID

from autotrade_research.evaluation.ablation import CanonicalAblationOutcomeEvidence
from autotrade_research.memory.episodes import (
    CoveragePopulationSnapshot,
    ExperienceMemory,
    MemoryIntegrityError,
)
from autotrade_research.memory.reconciled_outcome import (
    ReconciledOutcomeFactEvidence,
    _assert_memory_authority,
    _verify_fact_integrity,
    resolve_reconciled_outcome_fact,
    reverify_reconciled_outcome_fact,
)


_ASSERT_MEMORY_AUTHORITY = _assert_memory_authority
_VERIFY_FACT_INTEGRITY = _verify_fact_integrity
_RESOLVE_RECONCILED_FACT = resolve_reconciled_outcome_fact
_REVERIFY_RECONCILED_FACT = reverify_reconciled_outcome_fact
_COVERAGE_READ = ExperienceMemory.coverage_population_snapshot
_COVERAGE_VERIFY = CoveragePopulationSnapshot.verify_integrity


def _canonical_text(
    value: object,
    *,
    name: str,
    _error_type=MemoryIntegrityError,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _error_type(f"{name} must be exact canonical non-empty text")
    return value


def _sha256(
    value: object,
    *,
    name: str,
    _canonical_text_fn=_canonical_text,
    _error_type=MemoryIntegrityError,
) -> str:
    normalized = _canonical_text_fn(value, name=name)
    prefix = "sha256:"
    if not normalized.startswith(prefix):
        raise _error_type(f"{name} must be a sha256 digest")
    material = normalized[len(prefix) :]
    if len(material) != 64 or any(
        character not in "0123456789abcdef" for character in material
    ):
        raise _error_type(f"{name} must be a canonical sha256 digest")
    return normalized


def _stored_utc(
    value: object,
    *,
    name: str,
    _datetime_type=datetime,
    _timezone_utc=timezone.utc,
    _error_type=MemoryIntegrityError,
) -> datetime:
    if type(value) is not str:
        raise _error_type(f"{name} must be exact canonical UTC text")
    try:
        parsed = _datetime_type.fromisoformat(value)
    except ValueError as error:
        raise _error_type(f"{name} is not a valid timestamp") from error
    if type(parsed) is not _datetime_type or parsed.tzinfo is not _timezone_utc:
        raise _error_type(f"{name} must use exact UTC timezone")
    if parsed.isoformat() != value:
        raise _error_type(f"{name} must be canonical UTC text")
    return parsed


@dataclass(frozen=True)
class BoundReconciledAblationOutcome:
    """Non-numeric binding between one ablation artifact and one memory fact.

    This is a value object, not an issuer capability. Its embedded reconciled fact
    still has to be reverified against ExperienceMemory at every authority boundary.
    """

    case_id: str
    variant: str
    population_unit_id: str
    ablation_artifact_digest: str
    reconciled_fact: ReconciledOutcomeFactEvidence
    effective_outcome_available_utc: datetime

    def __post_init__(
        self,
        _canonical_text_fn=_canonical_text,
        _sha256_fn=_sha256,
        _verify_fact_integrity_fn=_VERIFY_FACT_INTEGRITY,
        _stored_utc_fn=_stored_utc,
        _fact_type=ReconciledOutcomeFactEvidence,
        _datetime_type=datetime,
        _timezone_utc=timezone.utc,
        _uuid_type=UUID,
        _error_type=MemoryIntegrityError,
    ) -> None:
        _canonical_text_fn(self.case_id, name="case_id")
        if type(self.variant) is not str or self.variant not in {"FULL", "ABLATED"}:
            raise _error_type("variant must be exact FULL or ABLATED text")
        episode_id = _canonical_text_fn(
            self.population_unit_id,
            name="population_unit_id",
        )
        try:
            canonical_episode_id = str(_uuid_type(episode_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise _error_type(
                "population_unit_id must be a canonical UUID"
            ) from error
        if canonical_episode_id != episode_id:
            raise _error_type("population_unit_id must be a canonical UUID")
        _sha256_fn(self.ablation_artifact_digest, name="ablation_artifact_digest")
        if type(self.reconciled_fact) is not _fact_type:
            raise TypeError(
                "reconciled_fact must be exact ReconciledOutcomeFactEvidence"
            )
        _verify_fact_integrity_fn(self.reconciled_fact)
        if self.population_unit_id != self.reconciled_fact.episode_id:
            raise _error_type(
                "ablation population unit does not match reconciled outcome episode"
            )
        if type(self.effective_outcome_available_utc) is not _datetime_type:
            raise TypeError("effective_outcome_available_utc must be exact datetime")
        if self.effective_outcome_available_utc.tzinfo is not _timezone_utc:
            raise ValueError(
                "effective_outcome_available_utc must use exact UTC timezone"
            )
        fact_cutoff = _stored_utc_fn(
            self.reconciled_fact.causal_cutoff,
            name="reconciled fact causal_cutoff",
        )
        if self.effective_outcome_available_utc > fact_cutoff:
            raise _error_type(
                "effective outcome availability cannot follow reconciled fact cutoff"
            )


def _bind_ablation_outcome_to_reconciled_fact_impl(
    memory: ExperienceMemory,
    outcome: CanonicalAblationOutcomeEvidence,
    *,
    causal_cutoff: datetime,
    granted_permissions: set[str],
    task: str | None = None,
    instrument_family: str | None = None,
    _assert_memory_authority_fn,
    _outcome_type,
    _datetime_type,
    _timezone_utc,
    _error_type,
    _resolve_reconciled_fact_fn,
    _reverify_reconciled_fact_fn,
    _coverage_read_fn,
    _snapshot_type,
    _coverage_verify_fn,
    _stored_utc_fn,
    _mapping_proxy_type,
    _bound_type,
) -> BoundReconciledAblationOutcome:
    """Bind an ablation artifact to independently re-resolved outcome history.

    The artifact numeric utility is intentionally never read. Its
    utility_evidence_digest must instead equal the digest independently recomputed
    from ExperienceMemory for the same population unit and frozen cut.
    """

    _assert_memory_authority_fn(memory)
    if type(outcome) is not _outcome_type:
        raise TypeError("outcome must be exact CanonicalAblationOutcomeEvidence")
    if type(causal_cutoff) is not _datetime_type:
        raise TypeError("causal_cutoff must be exact datetime")
    if causal_cutoff.tzinfo is not _timezone_utc:
        raise ValueError("causal_cutoff must use exact UTC timezone")
    if type(granted_permissions) is not set or not granted_permissions:
        raise TypeError("granted_permissions must be a non-empty exact set")
    cutoff = causal_cutoff
    if outcome.superseded_at_utc is not None and outcome.superseded_at_utc <= cutoff:
        raise _error_type(
            "superseded ablation outcome cannot bind at the selected causal cut"
        )

    fact = _resolve_reconciled_fact_fn(
        memory,
        episode_id=outcome.population_unit_id,
        causal_cutoff=cutoff,
        granted_permissions=granted_permissions,
        task=task,
        instrument_family=instrument_family,
    )
    _reverify_reconciled_fact_fn(memory, fact)
    if outcome.utility_evidence_digest != fact.evidence_digest:
        raise _error_type(
            "ablation utility evidence digest does not match canonical reconciled outcome fact"
        )

    _assert_memory_authority_fn(memory)
    snapshot = _coverage_read_fn(
        memory,
        causal_cutoff=cutoff,
        granted_permissions=set(granted_permissions),
        task=task,
        instrument_family=instrument_family,
    )
    if type(snapshot) is not _snapshot_type:
        raise _error_type(
            "canonical memory returned a non-canonical coverage snapshot"
        )
    _coverage_verify_fn(snapshot)
    if snapshot.root_hash != fact.population_root_hash:
        raise _error_type(
            "reconciled outcome population changed during ablation binding"
        )
    rows = tuple(
        row for row in snapshot.rows if row.get("episode_id") == fact.episode_id
    )
    if len(rows) != 1:
        raise _error_type(
            "reconciled outcome population unit is not unique at binding cut"
        )
    row = rows[0]
    available = _stored_utc_fn(row.get("created_at"), name="episode created_at")
    lineage = row.get("correction_lineage")
    if type(lineage) is not tuple:
        raise _error_type("canonical correction lineage is malformed")
    for correction in lineage:
        if type(correction) not in {dict, _mapping_proxy_type}:
            raise _error_type("canonical correction lineage is malformed")
        fields = correction.get("supersedes_fields")
        if type(fields) is not tuple:
            raise _error_type(
                "canonical correction supersedes_fields are malformed"
            )
        if "outcome" in fields:
            correction_available = _stored_utc_fn(
                correction.get("available_at"),
                name="outcome correction available_at",
            )
            if correction_available > available:
                available = correction_available

    if outcome.outcome_available_utc < available:
        raise _error_type(
            "ablation outcome claims availability before canonical reconciled outcome fact"
        )
    if outcome.outcome_available_utc > cutoff:
        raise _error_type(
            "ablation outcome was not available by selected causal cut"
        )
    _assert_memory_authority_fn(memory)

    return _bound_type(
        case_id=outcome.case_id,
        variant=outcome.variant,
        population_unit_id=outcome.population_unit_id,
        ablation_artifact_digest=outcome.evidence_digest,
        reconciled_fact=fact,
        effective_outcome_available_utc=available,
    )


def _bind_ablation_outcome_authority(
    bind_impl,
    assert_memory_authority_fn,
    outcome_type,
    datetime_type,
    timezone_utc,
    error_type,
    resolve_reconciled_fact_fn,
    reverify_reconciled_fact_fn,
    coverage_read_fn,
    snapshot_type,
    coverage_verify_fn,
    stored_utc_fn,
    mapping_proxy_type,
    bound_type,
):
    def bind_ablation_outcome_to_reconciled_fact(
        memory: ExperienceMemory,
        outcome: CanonicalAblationOutcomeEvidence,
        *,
        causal_cutoff: datetime,
        granted_permissions: set[str],
        task: str | None = None,
        instrument_family: str | None = None,
    ) -> BoundReconciledAblationOutcome:
        return bind_impl(
            memory,
            outcome,
            causal_cutoff=causal_cutoff,
            granted_permissions=granted_permissions,
            task=task,
            instrument_family=instrument_family,
            _assert_memory_authority_fn=assert_memory_authority_fn,
            _outcome_type=outcome_type,
            _datetime_type=datetime_type,
            _timezone_utc=timezone_utc,
            _error_type=error_type,
            _resolve_reconciled_fact_fn=resolve_reconciled_fact_fn,
            _reverify_reconciled_fact_fn=reverify_reconciled_fact_fn,
            _coverage_read_fn=coverage_read_fn,
            _snapshot_type=snapshot_type,
            _coverage_verify_fn=coverage_verify_fn,
            _stored_utc_fn=stored_utc_fn,
            _mapping_proxy_type=mapping_proxy_type,
            _bound_type=bound_type,
        )

    return bind_ablation_outcome_to_reconciled_fact


bind_ablation_outcome_to_reconciled_fact = _bind_ablation_outcome_authority(
    _bind_ablation_outcome_to_reconciled_fact_impl,
    _ASSERT_MEMORY_AUTHORITY,
    CanonicalAblationOutcomeEvidence,
    datetime,
    timezone.utc,
    MemoryIntegrityError,
    _RESOLVE_RECONCILED_FACT,
    _REVERIFY_RECONCILED_FACT,
    _COVERAGE_READ,
    CoveragePopulationSnapshot,
    _COVERAGE_VERIFY,
    _stored_utc,
    MappingProxyType,
    BoundReconciledAblationOutcome,
)
del _bind_ablation_outcome_authority
del _bind_ablation_outcome_to_reconciled_fact_impl
