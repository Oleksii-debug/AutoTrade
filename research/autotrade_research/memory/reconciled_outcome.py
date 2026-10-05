"""Frozen, reverifiable reconciled outcome facts from canonical ExperienceMemory.

This module intentionally stops before assigning numeric task utility.  It owns the
historical *fact* presented to a future preregistered scorer: the effective outcome
and its correction lineage at one exact causal cut.  A terminal scientific scorer
must still be independently defined by the registered estimand.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any
from uuid import UUID

from research.autotrade_research.memory.episodes import (
    ExperienceMemory,
    MemoryIntegrityError,
)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise MemoryIntegrityError(f"{name} must be text")
    normalized = value.strip()
    if not normalized or normalized != value:
        raise MemoryIntegrityError(f"{name} must be canonical non-empty text")
    return normalized


def _digest(value: str, *, name: str) -> str:
    normalized = _text(value, name=name)
    prefix = "sha256:"
    if not normalized.startswith(prefix):
        raise MemoryIntegrityError(f"{name} must be a sha256 digest")
    body = normalized[len(prefix) :]
    if len(body) != 64 or any(character not in "0123456789abcdef" for character in body):
        raise MemoryIntegrityError(f"{name} must be a canonical sha256 digest")
    return normalized


def _cutoff(value: str) -> datetime:
    normalized = _text(value, name="causal_cutoff")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise MemoryIntegrityError("causal_cutoff is not a valid timestamp") from error
    if parsed.tzinfo is None:
        raise MemoryIntegrityError("causal_cutoff must be timezone-aware")
    utc = parsed.astimezone(timezone.utc)
    if utc.isoformat() != normalized:
        raise MemoryIntegrityError("causal_cutoff must be canonical UTC time")
    return utc


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _canonical_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        raise MemoryIntegrityError(
            "binary float is not permitted in reconciled outcome evidence"
        )
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


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


@dataclass(frozen=True)
class ReconciledOutcomeFactEvidence:
    """One exact effective outcome derived from a frozen memory population.

    Construction alone never establishes authority.  Trust boundaries must call
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
        if not isinstance(self.permission_classes, tuple) or not self.permission_classes:
            raise MemoryIntegrityError(
                "reconciled outcome permission_classes must be a non-empty tuple"
            )
        normalized_permissions = tuple(
            _text(value, name="permission_class")
            for value in self.permission_classes
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
        try:
            canonical_episode_id = str(UUID(self.episode_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise MemoryIntegrityError("episode_id must be a canonical UUID") from error
        if canonical_episode_id != self.episode_id:
            raise MemoryIntegrityError("episode_id must be a canonical UUID")
        _digest(self.episode_hash, name="episode_hash")
        if not isinstance(self.correction_lineage, tuple):
            raise MemoryIntegrityError("correction_lineage must be an immutable tuple")
        if any(not isinstance(item, Mapping) for item in self.correction_lineage):
            raise MemoryIntegrityError("correction_lineage must contain mappings")
        object.__setattr__(self, "effective_outcome", _freeze(self.effective_outcome))
        object.__setattr__(
            self,
            "correction_lineage",
            tuple(_freeze(item) for item in self.correction_lineage),
        )
        _digest(self.evidence_digest, name="evidence_digest")
        self.verify_integrity()

    def _identity_payload(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "causal_cutoff": self.causal_cutoff,
            "permission_classes": self.permission_classes,
            "task": self.task,
            "instrument_family": self.instrument_family,
            "population_root_hash": self.population_root_hash,
            "episode_id": self.episode_id,
            "episode_hash": self.episode_hash,
            "effective_outcome": self.effective_outcome,
            "correction_lineage": self.correction_lineage,
        }

    def verify_integrity(self) -> None:
        """Reject mutation or a digest that does not bind the complete fact."""

        expected = _hash(self._identity_payload())
        if self.evidence_digest != expected:
            raise MemoryIntegrityError(
                "reconciled outcome evidence digest does not match its canonical fact"
            )


def resolve_reconciled_outcome_fact(
    memory: ExperienceMemory,
    *,
    episode_id: str,
    causal_cutoff: datetime,
    granted_permissions: set[str],
    task: str | None = None,
    instrument_family: str | None = None,
) -> ReconciledOutcomeFactEvidence:
    """Resolve one effective outcome from the exact canonical population cut.

    Tombstoned episodes are retained by the population snapshot for coverage
    accounting, but they cannot issue a usable outcome fact.  Post-cut corrections
    are deliberately invisible because ``coverage_population_snapshot`` applies
    only correction evidence causally available at ``causal_cutoff``.
    """

    try:
        normalized_episode_id = str(UUID(episode_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("episode_id must be a UUID") from error
    if normalized_episode_id != episode_id:
        raise ValueError("episode_id must be a canonical UUID")

    snapshot = memory.coverage_population_snapshot(
        causal_cutoff=causal_cutoff,
        granted_permissions=granted_permissions,
        task=task,
        instrument_family=instrument_family,
    )
    snapshot.verify_integrity()
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
    if not isinstance(tombstones, tuple):
        raise MemoryIntegrityError("canonical outcome tombstone lineage is malformed")
    if tombstones:
        raise MemoryIntegrityError(
            "tombstoned episode cannot issue reconciled outcome evidence"
        )
    payload = row.get("effective_payload")
    if not isinstance(payload, Mapping) or "outcome" not in payload:
        raise MemoryIntegrityError("canonical effective payload lacks outcome")
    correction_lineage = row.get("correction_lineage")
    if not isinstance(correction_lineage, tuple):
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
    return ReconciledOutcomeFactEvidence(
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


def reverify_reconciled_outcome_fact(
    memory: ExperienceMemory,
    evidence: ReconciledOutcomeFactEvidence,
) -> ReconciledOutcomeFactEvidence:
    """Re-resolve a fact from durable memory and require exact historical identity."""

    if not isinstance(evidence, ReconciledOutcomeFactEvidence):
        raise TypeError("evidence must be ReconciledOutcomeFactEvidence")
    evidence.verify_integrity()
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
