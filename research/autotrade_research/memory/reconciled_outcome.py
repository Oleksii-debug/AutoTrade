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


def _text(
    value: object,
    *,
    name: str,
    _error_type=MemoryIntegrityError,
) -> str:
    if type(value) is not str:
        raise _error_type(f"{name} must be exact built-in text")
    if not value or value != value.strip():
        raise _error_type(f"{name} must be canonical non-empty text")
    return value


def _digest(
    value: object,
    *,
    name: str,
    _text_fn=_text,
    _error_type=MemoryIntegrityError,
) -> str:
    normalized = _text_fn(value, name=name)
    prefix = "sha256:"
    if not normalized.startswith(prefix):
        raise _error_type(f"{name} must be a sha256 digest")
    body = normalized[len(prefix) :]
    if len(body) != 64 or any(
        character not in "0123456789abcdef" for character in body
    ):
        raise _error_type(f"{name} must be a canonical sha256 digest")
    return normalized


def _cutoff(
    value: object,
    _text_fn=_text,
    _datetime_type=datetime,
    _timezone_utc=timezone.utc,
    _error_type=MemoryIntegrityError,
) -> datetime:
    normalized = _text_fn(value, name="causal_cutoff")
    try:
        parsed = _datetime_type.fromisoformat(normalized)
    except ValueError as error:
        raise _error_type("causal_cutoff is not a valid timestamp") from error
    if type(parsed) is not _datetime_type or parsed.tzinfo is not _timezone_utc:
        raise _error_type("causal_cutoff must use exact UTC timezone")
    if parsed.isoformat() != normalized:
        raise _error_type("causal_cutoff must be canonical UTC time")
    return parsed


def _canonical_value(
    value: Any,
    _mapping_proxy_type=MappingProxyType,
    _text_fn=_text,
    _error_type=MemoryIntegrityError,
) -> Any:
    def convert(item: Any) -> Any:
        item_type = type(item)
        if item_type in {dict, _mapping_proxy_type}:
            return {
                _text_fn(key, name="canonical JSON object key"): convert(nested)
                for key, nested in item.items()
            }
        if item_type in {list, tuple}:
            return [convert(nested) for nested in item]
        if item is None or item_type in {str, int, bool}:
            return item
        if item_type is float:
            raise _error_type(
                "binary float is not permitted in reconciled outcome evidence"
            )
        raise _error_type(
            "reconciled outcome evidence contains a non-canonical JSON value"
        )

    return convert(value)


def _freeze(
    value: Any,
    _mapping_proxy_type=MappingProxyType,
    _text_fn=_text,
    _error_type=MemoryIntegrityError,
) -> Any:
    def freeze(item: Any) -> Any:
        item_type = type(item)
        if item_type in {dict, _mapping_proxy_type}:
            return _mapping_proxy_type(
                {
                    _text_fn(key, name="canonical JSON object key"): freeze(nested)
                    for key, nested in item.items()
                }
            )
        if item_type in {list, tuple}:
            return tuple(freeze(nested) for nested in item)
        if item is None or item_type in {str, int, bool}:
            return item
        if item_type is float:
            raise _error_type(
                "binary float is not permitted in reconciled outcome evidence"
            )
        raise _error_type(
            "reconciled outcome evidence contains a non-canonical JSON value"
        )

    return freeze(value)


def _hash(
    value: Any,
    _canonical_value_fn=_canonical_value,
    _json_dumps=json.dumps,
    _sha256_fn=sha256,
    _error_type=MemoryIntegrityError,
) -> str:
    try:
        canonical = _json_dumps(
            _canonical_value_fn(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise _error_type(
            "reconciled outcome evidence is not canonical JSON"
        ) from error
    return "sha256:" + _sha256_fn(canonical.encode("utf-8")).hexdigest()


def _assert_memory_authority(
    memory: ExperienceMemory,
    _memory_type=ExperienceMemory,
    _snapshot_type=CoveragePopulationSnapshot,
    _coverage_read_raw=_COVERAGE_READ_RAW,
    _memory_connect_raw=_MEMORY_CONNECT_RAW,
    _coverage_verify_raw=_COVERAGE_VERIFY_RAW,
    _object_getattribute=object.__getattribute__,
    _error_type=MemoryIntegrityError,
) -> None:
    if type(memory) is not _memory_type:
        raise TypeError("memory must be exact ExperienceMemory")
    if (
        _memory_type.__dict__.get("coverage_population_snapshot")
        is not _coverage_read_raw
    ):
        raise _error_type(
            "ExperienceMemory coverage executable changed after composition"
        )
    if _memory_type.__dict__.get("_connect") is not _memory_connect_raw:
        raise _error_type(
            "ExperienceMemory persistence executable changed after composition"
        )
    if (
        _snapshot_type.__dict__.get("verify_integrity")
        is not _coverage_verify_raw
    ):
        raise _error_type(
            "coverage snapshot verifier changed after composition"
        )
    state = _object_getattribute(memory, "__dict__")
    if type(state) is not dict:
        raise _error_type("ExperienceMemory state is not canonical")
    shadowed = tuple(
        sorted(
            name
            for name in state
            if name in _memory_type.__dict__
            and callable(getattr(_memory_type, name, None))
        )
    )
    if shadowed:
        raise _error_type(
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
    _assert_memory_authority_fn=_assert_memory_authority,
    _text_fn=_text,
    _uuid_type=UUID,
    _datetime_type=datetime,
    _timezone_utc=timezone.utc,
    _error_type=MemoryIntegrityError,
) -> tuple[str, tuple[str, ...]]:
    _assert_memory_authority_fn(memory)
    canonical_episode_id = _text_fn(episode_id, name="episode_id")
    try:
        parsed_episode_id = str(_uuid_type(canonical_episode_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("episode_id must be a UUID") from error
    if parsed_episode_id != canonical_episode_id:
        raise ValueError("episode_id must be a canonical UUID")
    if type(causal_cutoff) is not _datetime_type:
        raise TypeError("causal_cutoff must be exact datetime")
    if causal_cutoff.tzinfo is not _timezone_utc:
        raise ValueError("causal_cutoff must use exact UTC timezone")
    if type(granted_permissions) is not set or not granted_permissions:
        raise TypeError("granted_permissions must be a non-empty exact set")
    permissions = tuple(
        sorted(_text_fn(value, name="permission_class") for value in granted_permissions)
    )
    if len(permissions) != len(granted_permissions):
        raise _error_type("granted_permissions must be unique")
    if task is not None:
        _text_fn(task, name="task")
    if instrument_family is not None:
        _text_fn(instrument_family, name="instrument_family")
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


def _verify_fact_integrity(
    evidence: "ReconciledOutcomeFactEvidence",
    _hash_fn=_hash,
    _identity_payload_fn=_fact_identity_payload,
    _error_type=MemoryIntegrityError,
) -> None:
    if type(evidence) is not ReconciledOutcomeFactEvidence:
        raise TypeError("evidence must be exact ReconciledOutcomeFactEvidence")
    expected = _hash_fn(_identity_payload_fn(evidence))
    if evidence.evidence_digest != expected:
        raise _error_type(
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

    def __post_init__(
        self,
        _cutoff_fn=_cutoff,
        _text_fn=_text,
        _digest_fn=_digest,
        _freeze_fn=_freeze,
        _verify_fact_integrity_fn=_verify_fact_integrity,
        _uuid_type=UUID,
        _error_type=MemoryIntegrityError,
    ) -> None:
        _cutoff_fn(self.causal_cutoff)
        if type(self.permission_classes) is not tuple or not self.permission_classes:
            raise _error_type(
                "reconciled outcome permission_classes must be a non-empty exact tuple"
            )
        normalized_permissions = tuple(
            _text_fn(value, name="permission_class") for value in self.permission_classes
        )
        if normalized_permissions != tuple(sorted(set(normalized_permissions))):
            raise _error_type(
                "reconciled outcome permission_classes must be sorted and unique"
            )
        if self.task is not None:
            _text_fn(self.task, name="task")
        if self.instrument_family is not None:
            _text_fn(self.instrument_family, name="instrument_family")
        _digest_fn(self.population_root_hash, name="population_root_hash")
        episode_id = _text_fn(self.episode_id, name="episode_id")
        try:
            canonical_episode_id = str(_uuid_type(episode_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise _error_type("episode_id must be a canonical UUID") from error
        if canonical_episode_id != episode_id:
            raise _error_type("episode_id must be a canonical UUID")
        _digest_fn(self.episode_hash, name="episode_hash")
        if type(self.correction_lineage) is not tuple:
            raise _error_type(
                "correction_lineage must be an exact immutable tuple"
            )
        object.__setattr__(self, "effective_outcome", _freeze_fn(self.effective_outcome))
        object.__setattr__(
            self,
            "correction_lineage",
            tuple(_freeze_fn(item) for item in self.correction_lineage),
        )
        _digest_fn(self.evidence_digest, name="evidence_digest")
        _verify_fact_integrity_fn(self)

    def _identity_payload(
        self,
        _identity_payload_fn=_fact_identity_payload,
    ) -> Mapping[str, Any]:
        return _identity_payload_fn(self)

    def verify_integrity(
        self,
        _verify_fact_integrity_fn=_verify_fact_integrity,
    ) -> None:
        """Reject mutation or a digest that does not bind the complete fact."""
        _verify_fact_integrity_fn(self)


def _resolve_reconciled_outcome_fact_impl(
    memory: ExperienceMemory,
    *,
    episode_id: str,
    causal_cutoff: datetime,
    granted_permissions: set[str],
    task: str | None = None,
    instrument_family: str | None = None,
    _validate_query_fn,
    _coverage_read_fn,
    _snapshot_type,
    _coverage_verify_fn,
    _error_type,
    _mapping_proxy_type,
    _fact_type,
    _hash_fn,
    _assert_memory_authority_fn,
) -> ReconciledOutcomeFactEvidence:
    """Resolve one effective outcome from the exact canonical population cut."""

    normalized_episode_id, permissions = _validate_query_fn(
        memory=memory,
        episode_id=episode_id,
        causal_cutoff=causal_cutoff,
        granted_permissions=granted_permissions,
        task=task,
        instrument_family=instrument_family,
    )
    snapshot = _coverage_read_fn(
        memory,
        causal_cutoff=causal_cutoff,
        granted_permissions=set(permissions),
        task=task,
        instrument_family=instrument_family,
    )
    if type(snapshot) is not _snapshot_type:
        raise _error_type(
            "canonical memory returned a non-canonical coverage snapshot"
        )
    _coverage_verify_fn(snapshot)
    matches = tuple(
        row for row in snapshot.rows if row.get("episode_id") == normalized_episode_id
    )
    if not matches:
        raise KeyError(normalized_episode_id)
    if len(matches) != 1:
        raise _error_type(
            "canonical population contains duplicate episode identity"
        )
    row = matches[0]
    tombstones = row.get("tombstone_lineage")
    if type(tombstones) is not tuple:
        raise _error_type("canonical outcome tombstone lineage is malformed")
    if tombstones:
        raise _error_type(
            "tombstoned episode cannot issue reconciled outcome evidence"
        )
    payload = row.get("effective_payload")
    if type(payload) not in {dict, _mapping_proxy_type} or "outcome" not in payload:
        raise _error_type("canonical effective payload lacks outcome")
    correction_lineage = row.get("correction_lineage")
    if type(correction_lineage) is not tuple:
        raise _error_type("canonical correction lineage is malformed")

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
    result = _fact_type(
        causal_cutoff=snapshot.causal_cutoff,
        permission_classes=snapshot.permission_classes,
        task=snapshot.task,
        instrument_family=snapshot.instrument_family,
        population_root_hash=snapshot.root_hash,
        episode_id=normalized_episode_id,
        episode_hash=row.get("episode_hash"),
        effective_outcome=payload["outcome"],
        correction_lineage=correction_lineage,
        evidence_digest=_hash_fn(identity),
    )
    _assert_memory_authority_fn(memory)
    return result


def _reverify_reconciled_outcome_fact_impl(
    memory: ExperienceMemory,
    evidence: ReconciledOutcomeFactEvidence,
    *,
    _assert_memory_authority_fn,
    _verify_fact_integrity_fn,
    _resolve_fn,
    _cutoff_fn,
    _error_type,
) -> ReconciledOutcomeFactEvidence:
    """Re-resolve a fact from durable memory and require exact historical identity."""

    _assert_memory_authority_fn(memory)
    _verify_fact_integrity_fn(evidence)
    resolved = _resolve_fn(
        memory,
        episode_id=evidence.episode_id,
        causal_cutoff=_cutoff_fn(evidence.causal_cutoff),
        granted_permissions=set(evidence.permission_classes),
        task=evidence.task,
        instrument_family=evidence.instrument_family,
    )
    if resolved != evidence:
        raise _error_type(
            "reconciled outcome evidence does not match canonical ExperienceMemory"
        )
    return resolved


def _bind_reconciled_outcome_authority(
    resolve_impl,
    reverify_impl,
    validate_query_fn,
    coverage_read_fn,
    snapshot_type,
    coverage_verify_fn,
    error_type,
    mapping_proxy_type,
    fact_type,
    hash_fn,
    assert_memory_authority_fn,
    verify_fact_integrity_fn,
    cutoff_fn,
):
    def resolve_reconciled_outcome_fact(
        memory: ExperienceMemory,
        *,
        episode_id: str,
        causal_cutoff: datetime,
        granted_permissions: set[str],
        task: str | None = None,
        instrument_family: str | None = None,
    ) -> ReconciledOutcomeFactEvidence:
        return resolve_impl(
            memory,
            episode_id=episode_id,
            causal_cutoff=causal_cutoff,
            granted_permissions=granted_permissions,
            task=task,
            instrument_family=instrument_family,
            _validate_query_fn=validate_query_fn,
            _coverage_read_fn=coverage_read_fn,
            _snapshot_type=snapshot_type,
            _coverage_verify_fn=coverage_verify_fn,
            _error_type=error_type,
            _mapping_proxy_type=mapping_proxy_type,
            _fact_type=fact_type,
            _hash_fn=hash_fn,
            _assert_memory_authority_fn=assert_memory_authority_fn,
        )

    def reverify_reconciled_outcome_fact(
        memory: ExperienceMemory,
        evidence: ReconciledOutcomeFactEvidence,
    ) -> ReconciledOutcomeFactEvidence:
        return reverify_impl(
            memory,
            evidence,
            _assert_memory_authority_fn=assert_memory_authority_fn,
            _verify_fact_integrity_fn=verify_fact_integrity_fn,
            _resolve_fn=resolve_reconciled_outcome_fact,
            _cutoff_fn=cutoff_fn,
            _error_type=error_type,
        )

    return resolve_reconciled_outcome_fact, reverify_reconciled_outcome_fact


(
    resolve_reconciled_outcome_fact,
    reverify_reconciled_outcome_fact,
) = _bind_reconciled_outcome_authority(
    _resolve_reconciled_outcome_fact_impl,
    _reverify_reconciled_outcome_fact_impl,
    _validate_query,
    _COVERAGE_READ,
    CoveragePopulationSnapshot,
    _COVERAGE_VERIFY,
    MemoryIntegrityError,
    MappingProxyType,
    ReconciledOutcomeFactEvidence,
    _hash,
    _assert_memory_authority,
    _verify_fact_integrity,
    _cutoff,
)
del _bind_reconciled_outcome_authority
del _resolve_reconciled_outcome_fact_impl
del _reverify_reconciled_outcome_fact_impl

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
