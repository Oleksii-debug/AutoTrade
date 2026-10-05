"""Frozen, reverifiable reconciled outcome facts from canonical ExperienceMemory.

This module intentionally stops before assigning numeric task utility. It owns the
historical *fact* presented to a future preregistered scorer: the effective outcome
and its correction lineage at one exact causal cut. A terminal scientific scorer
must still be independently defined by the registered estimand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from autotrade_research.memory.episodes import (
    CoveragePopulationSnapshot,
    ExperienceMemory,
    MemoryIntegrityError,
)


_COVERAGE_READ = ExperienceMemory.coverage_population_snapshot
_COVERAGE_READ_RAW = ExperienceMemory.__dict__["coverage_population_snapshot"]
_MEMORY_CONNECT_RAW = ExperienceMemory.__dict__["_connect"]
_COVERAGE_VERIFY = CoveragePopulationSnapshot.verify_integrity
_COVERAGE_VERIFY_RAW = CoveragePopulationSnapshot.__dict__["verify_integrity"]


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise MemoryIntegrityError(f"{name} must be exact built-in text")
    if not value or value != value.strip():
        raise MemoryIntegrityError(f"{name} must be canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    normalized = _text(value, name=name)
    prefix = "sha256:"
    if not normalized.startswith(prefix):
        raise MemoryIntegrityError(f"{name} must be a sha256 digest")
    body = normalized[len(prefix) :]
    if len(body) != 64 or any(
        character not in "0123456789abcdef" for character in body
    ):
        raise MemoryIntegrityError(f"{name} must be a canonical sha256 digest")
    return normalized


def _cutoff(value: object) -> datetime:
    normalized = _text(value, name="causal_cutoff")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise MemoryIntegrityError("causal_cutoff is not a valid timestamp") from error
    if type(parsed) is not datetime or parsed.tzinfo is not timezone.utc:
        raise MemoryIntegrityError("causal_cutoff must use exact UTC timezone")
    if parsed.isoformat() != normalized:
        raise MemoryIntegrityError("causal_cutoff must be canonical UTC time")
    return parsed


def _canonical_value(value: Any) -> Any:
    value_type = type(value)
    if value_type in {dict, MappingProxyType}:
        return {
            _text(key, name="canonical JSON object key"): _canonical_value(item)
            for key, item in value.items()
        }
    if value_type in {list, tuple}:
        return [_canonical_value(item) for item in value]
    if value is None or value_type in {str, int, bool}:
        return value
    if value_type is float:
        raise MemoryIntegrityError(
            "binary float is not permitted in reconciled outcome evidence"
        )
    raise MemoryIntegrityError(
        "reconciled outcome evidence contains a non-canonical JSON value"
    )


def _freeze(value: Any) -> Any:
    value_type = type(value)
    if value_type in {dict, MappingProxyType}:
        return MappingProxyType(
            {
                _text(key, name="canonical JSON object key"): _freeze(item)
                for key, item in value.items()
            }
        )
    if value_type in {list, tuple}:
        return tuple(_freeze(item) for item in value)
    if value is None or value_type in {str, int, bool}:
        return value
    if value_type is float:
        raise MemoryIntegrityError(
            "binary float is not permitted in reconciled outcome evidence"
        )
    raise MemoryIntegrityError(
        "reconciled outcome evidence contains a non-canonical JSON value"
    )


def _hash(value: Any) -> str:
    try:
        canonical = json.dumps(
            _canonical_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise MemoryIntegrityError(
            "reconciled outcome evidence is not canonical JSON"
        ) from error
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def _assert_memory_authority(memory: ExperienceMemory) -> None:
    if type(memory) is not ExperienceMemory:
        raise TypeError("memory must be exact ExperienceMemory")
    if (
        ExperienceMemory.__dict__.get("coverage_population_snapshot")
        is not _COVERAGE_READ_RAW
    ):
        raise MemoryIntegrityError(
            "ExperienceMemory coverage executable changed after composition"
        )
    if ExperienceMemory.__dict__.get("_connect") is not _MEMORY_CONNECT_RAW:
        raise MemoryIntegrityError(
            "ExperienceMemory persistence executable changed after composition"
        )
    if (
        CoveragePopulationSnapshot.__dict__.get("verify_integrity")
        is not _COVERAGE_VERIFY_RAW
    ):
        raise MemoryIntegrityError(
            "coverage snapshot verifier changed after composition"
        )
    state = object.__getattribute__(memory, "__dict__")
    if type(state) is not dict:
        raise MemoryIntegrityError("ExperienceMemory state is not canonical")
    shadowed = tuple(
        sorted(
            name
            for name in state
            if name in ExperienceMemory.__dict__
            and callable(getattr(ExperienceMemory, name, None))
        )
    )
    if shadowed:
        raise MemoryIntegrityError(
            "ExperienceMemory shadows canonical executables: "
            + ", ".join(shadowed)
        )


def _validate_query(
    *,
    memory: ExperienceMemory,
    episode_id: object,
    causal_cutoff: datetime,
    granted_permissions: set[str],
    task: str | None,
    instrument_family: str | None,
) -> tuple[str, tuple[str, ...]]:
    _assert_memory_authority(memory)
    canonical_episode_id = _text(episode_id, name="episode_id")
    try:
        parsed_episode_id = str(UUID(canonical_episode_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("episode_id must be a UUID") from error
    if parsed_episode_id != canonical_episode_id:
        raise ValueError("episode_id must be a canonical UUID")
    if type(causal_cutoff) is not datetime:
        raise TypeError("causal_cutoff must be exact datetime")
    if causal_cutoff.tzinfo is not timezone.utc:
        raise ValueError("causal_cutoff must use exact UTC timezone")
    if type(granted_permissions) is not set or not granted_permissions:
        raise TypeError("granted_permissions must be a non-empty exact set")
    permissions = tuple(
        sorted(_text(value, name="permission_class") for value in granted_permissions)
    )
    if len(permissions) != len(granted_permissions):
        raise MemoryIntegrityError("granted_permissions must be unique")
    if task is not None:
        _text(task, name="task")
    if instrument_family is not None:
        _text(instrument_family, name="instrument_family")
    return canonical_episode_id, permissions


def _fact_identity_payload(evidence: "ReconciledOutcomeFactEvidence") -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "causal_cutoff": evidence.causal_cutoff,
        "permission_classes": evidence.permission_classes,
        "task": evidence.task,
        "instrument_family": evidence.instrument_family,
        "population_root_hash": evidence.population_root_hash,
        "episode_id": evidence.episode_id,
        "episode_hash": evidence.episode_hash,
        "effective_outcome": evidence.effective_outcome,
        "correction_lineage": evidence.correction_lineage,
    }


def _verify_fact_integrity(evidence: "ReconciledOutcomeFactEvidence") -> None:
    if type(evidence) is not ReconciledOutcomeFactEvidence:
        raise TypeError("evidence must be exact ReconciledOutcomeFactEvidence")
    expected = _hash(_fact_identity_payload(evidence))
    if evidence.evidence_digest != expected:
        raise MemoryIntegrityError(
            "reconciled outcome evidence digest does not match its canonical fact"
        )


@dataclass(frozen=True)
class ReconciledOutcomeFactEvidence:
    """One exact effective outcome derived from a frozen memory population.

    Construction alone never establishes authority. Trust boundaries must call
    :func:`reverify_reconciled_outcome_fact`, which re-resolves the same query
    against ExperienceMemory and requires byte-equivalent evidence identity.
    """

    causal_cutoff: str
    permission_classes: tuple[str, ...]
    task: str | None
    instrument_family: str | None
    population_root_hash: str
    episode_id: str
    episode_hash: str
    effective_outcome: Any
    correction_lineage: tuple[Mapping[str, Any], ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        _cutoff(self.causal_cutoff)
        if type(self.permission_classes) is not tuple or not self.permission_classes:
            raise MemoryIntegrityError(
                "reconciled outcome permission_classes must be a non-empty exact tuple"
            )
        normalized_permissions = tuple(
            _text(value, name="permission_class") for value in self.permission_classes
        )
        if normalized_permissions != tuple(sorted(set(normalized_permissions))):
            raise MemoryIntegrityError(
                "reconciled outcome permission_classes must be sorted and unique"
            )
        if self.task is not None:
            _text(self.task, name="task")
        if self.instrument_family is not None:
            _text(self.instrument_family, name="instrument_family")
        _digest(self.population_root_hash, name="population_root_hash")
        episode_id = _text(self.episode_id, name="episode_id")
        try:
            canonical_episode_id = str(UUID(episode_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise MemoryIntegrityError("episode_id must be a canonical UUID") from error
        if canonical_episode_id != episode_id:
            raise MemoryIntegrityError("episode_id must be a canonical UUID")
        _digest(self.episode_hash, name="episode_hash")
        if type(self.correction_lineage) is not tuple:
            raise MemoryIntegrityError(
                "correction_lineage must be an exact immutable tuple"
            )
        object.__setattr__(self, "effective_outcome", _freeze(self.effective_outcome))
        object.__setattr__(
            self,
            "correction_lineage",
            tuple(_freeze(item) for item in self.correction_lineage),
        )
        _digest(self.evidence_digest, name="evidence_digest")
        _verify_fact_integrity(self)

    def _identity_payload(self) -> Mapping[str, Any]:
        return _fact_identity_payload(self)

    def verify_integrity(self) -> None:
        """Reject mutation or a digest that does not bind the complete fact."""
        _verify_fact_integrity(self)


def resolve_reconciled_outcome_fact(
    memory: ExperienceMemory,
    *,
    episode_id: str,
    causal_cutoff: datetime,
    granted_permissions: set[str],
    task: str | None = None,
    instrument_family: str | None = None,
) -> ReconciledOutcomeFactEvidence:
    """Resolve one effective outcome from the exact canonical population cut."""

    normalized_episode_id, permissions = _validate_query(
        memory=memory,
        episode_id=episode_id,
        causal_cutoff=causal_cutoff,
        granted_permissions=granted_permissions,
        task=task,
        instrument_family=instrument_family,
    )
    snapshot = _COVERAGE_READ(
        memory,
        causal_cutoff=causal_cutoff,
        granted_permissions=set(permissions),
        task=task,
        instrument_family=instrument_family,
    )
    if type(snapshot) is not CoveragePopulationSnapshot:
        raise MemoryIntegrityError(
            "canonical memory returned a non-canonical coverage snapshot"
        )
    _COVERAGE_VERIFY(snapshot)
    matches = tuple(
        row for row in snapshot.rows if row.get("episode_id") == normalized_episode_id
    )
    if not matches:
        raise KeyError(normalized_episode_id)
    if len(matches) != 1:
        raise MemoryIntegrityError(
            "canonical population contains duplicate episode identity"
        )
    row = matches[0]
    tombstones = row.get("tombstone_lineage")
    if type(tombstones) is not tuple:
        raise MemoryIntegrityError("canonical outcome tombstone lineage is malformed")
    if tombstones:
        raise MemoryIntegrityError(
            "tombstoned episode cannot issue reconciled outcome evidence"
        )
    payload = row.get("effective_payload")
    if type(payload) not in {dict, MappingProxyType} or "outcome" not in payload:
        raise MemoryIntegrityError("canonical effective payload lacks outcome")
    correction_lineage = row.get("correction_lineage")
    if type(correction_lineage) is not tuple:
        raise MemoryIntegrityError("canonical correction lineage is malformed")

    identity = {
        "schema_version": 1,
        "causal_cutoff": snapshot.causal_cutoff,
        "permission_classes": snapshot.permission_classes,
        "task": snapshot.task,
        "instrument_family": snapshot.instrument_family,
        "population_root_hash": snapshot.root_hash,
        "episode_id": normalized_episode_id,
        "episode_hash": row.get("episode_hash"),
        "effective_outcome": payload["outcome"],
        "correction_lineage": correction_lineage,
    }
    result = ReconciledOutcomeFactEvidence(
        causal_cutoff=snapshot.causal_cutoff,
        permission_classes=snapshot.permission_classes,
        task=snapshot.task,
        instrument_family=snapshot.instrument_family,
        population_root_hash=snapshot.root_hash,
        episode_id=normalized_episode_id,
        episode_hash=row.get("episode_hash"),
        effective_outcome=payload["outcome"],
        correction_lineage=correction_lineage,
        evidence_digest=_hash(identity),
    )
    _assert_memory_authority(memory)
    return result


def reverify_reconciled_outcome_fact(
    memory: ExperienceMemory,
    evidence: ReconciledOutcomeFactEvidence,
) -> ReconciledOutcomeFactEvidence:
    """Re-resolve a fact from durable memory and require exact historical identity."""

    _assert_memory_authority(memory)
    _verify_fact_integrity(evidence)
    resolved = resolve_reconciled_outcome_fact(
        memory,
        episode_id=evidence.episode_id,
        causal_cutoff=_cutoff(evidence.causal_cutoff),
        granted_permissions=set(evidence.permission_classes),
        task=evidence.task,
        instrument_family=evidence.instrument_family,
    )
    if resolved != evidence:
        raise MemoryIntegrityError(
            "reconciled outcome evidence does not match canonical ExperienceMemory"
        )
    return resolved
