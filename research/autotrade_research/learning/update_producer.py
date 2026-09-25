"""Evidence-first deterministic producer for bounded online updates (WP-38).

The producer is intentionally not a promotion authority. It reads canonical
ExperienceMemory causal snapshots, immutable checkpoint/calibration/test
artifacts, derives one simple bounded-linear update plus calibrated drift
evidence, and returns deterministic bytes. Publication reuses ResearchJobStore
and ArtifactStore. Existing online/retention/science/risk/champion gates remain
authoritative downstream.

No LLM, network call, credential, order sender, risk mutation, or trading
authority exists in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from ..artifacts.store import ArtifactStore
from ..jobs import ResearchJobStore
from ..memory.episodes import CoveragePopulationSnapshot, ExperienceMemory
from ..science.registry import ProtocolRegistration, ScientificRegistry
from .online import (
    OnlineUpdateDecision,
    OnlineUpdateEnvelope,
    OnlineUpdateInput,
    evaluate_online_update,
)
from .population_coverage import (
    PopulationCoverageManifest,
    build_population_coverage,
    resolve_reconciliation_evidence,
)


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_ARTIFACT_REF_RE = re.compile(
    r"^artifact:([0-9a-f-]{36})@sha256:([0-9a-f]{64})$"
)
_ALGORITHM_ID = "bounded-linear-gradient-v1"
_SCHEMA_VERSION = "1.0.0"


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    if value != value.strip():
        raise ValueError(f"{name} must use canonical text")
    return value


def _sha256_identity(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return text


def _git_sha(value: object) -> str:
    text = _text(value, name="source_sha")
    if _GIT_SHA_RE.fullmatch(text) is None:
        raise ValueError("source_sha must be a canonical 40- or 64-hex Git object id")
    return text


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite exact decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite exact decimal")
    return result


def _non_negative(value: object, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _time(value: object, *, name: str) -> datetime:
    if isinstance(value, datetime):
        point = value
    elif isinstance(value, str):
        try:
            point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{name} must be an ISO timestamp") from error
    else:
        raise TypeError(f"{name} must be datetime or ISO timestamp")
    if point.tzinfo is None or point.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return point.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _time(value, name="time").isoformat().replace("+00:00", "Z")


def _reject_float(value: Any, *, path: str = "$") -> None:
    if isinstance(value, float):
        raise TypeError(f"binary float is not permitted in update evidence: {path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_float(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_float(item, path=f"{path}[{index}]")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    _reject_float(value)
    return json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _artifact_ref(
    artifact_store: ArtifactStore,
    reference: object,
    *,
    name: str,
) -> tuple[str, dict[str, Any], bytes]:
    if not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact_store must be ArtifactStore")
    ref = _text(reference, name=name)
    match = _ARTIFACT_REF_RE.fullmatch(ref)
    if match is None:
        raise ValueError(
            f"{name} must bind artifact:<uuid>@sha256:<64 lowercase hex>"
        )
    artifact_id, digest = match.groups()
    if str(UUID(artifact_id)) != artifact_id:
        raise ValueError(f"{name} artifact id must use canonical UUID text")
    manifest = artifact_store.load_manifest(artifact_id)
    payload = artifact_store.read_bytes(artifact_id)
    expected = "sha256:" + digest
    if manifest.get("sha256") != expected or _digest_bytes(payload) != expected:
        raise ValueError(f"{name} does not match immutable artifact bytes")
    return ref, manifest, payload


@dataclass(frozen=True, slots=True)
class UpdateProducerConfig:
    source_sha: str
    algorithm_version: str
    feature_schema_hash: str
    label_version: str
    learning_rate: Decimal
    min_update_episodes: int
    min_calibration_episodes: int
    target_false_alarm_rate: Decimal
    max_compute_units: Decimal
    update_task: str
    calibration_task: str
    test_evidence_refs: tuple[str, ...]
    instrument_family: str | None = None
    protocol_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_sha", _git_sha(self.source_sha))
        object.__setattr__(
            self,
            "algorithm_version",
            _text(self.algorithm_version, name="algorithm_version"),
        )
        object.__setattr__(
            self,
            "feature_schema_hash",
            _sha256_identity(
                self.feature_schema_hash,
                name="feature_schema_hash",
            ),
        )
        object.__setattr__(
            self,
            "label_version",
            _text(self.label_version, name="label_version"),
        )
        rate = _non_negative(self.learning_rate, name="learning_rate")
        if rate == 0:
            raise ValueError("learning_rate must be positive")
        object.__setattr__(self, "learning_rate", rate)
        for field_name in ("min_update_episodes", "min_calibration_episodes"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                raise ValueError(f"{field_name} must be a positive integer")
        alpha = _non_negative(
            self.target_false_alarm_rate,
            name="target_false_alarm_rate",
        )
        if alpha <= 0 or alpha > Decimal("0.5"):
            raise ValueError(
                "target_false_alarm_rate must be within (0, 0.5]"
            )
        object.__setattr__(self, "target_false_alarm_rate", alpha)
        object.__setattr__(
            self,
            "max_compute_units",
            _non_negative(
                self.max_compute_units,
                name="max_compute_units",
            ),
        )
        object.__setattr__(
            self,
            "update_task",
            _text(self.update_task, name="update_task"),
        )
        object.__setattr__(
            self,
            "calibration_task",
            _text(self.calibration_task, name="calibration_task"),
        )
        if self.update_task == self.calibration_task:
            raise ValueError(
                "update_task and calibration_task must be distinct frozen populations"
            )
        refs = tuple(
            _text(value, name="test_evidence_ref")
            for value in self.test_evidence_refs
        )
        if not refs or len(refs) != len(set(refs)):
            raise ValueError(
                "test_evidence_refs must be a non-empty unique tuple"
            )
        object.__setattr__(self, "test_evidence_refs", refs)
        if self.instrument_family is not None:
            object.__setattr__(
                self,
                "instrument_family",
                _text(
                    self.instrument_family,
                    name="instrument_family",
                ),
            )
        if self.protocol_id is not None:
            object.__setattr__(
                self,
                "protocol_id",
                _text(self.protocol_id, name="protocol_id"),
            )


@dataclass(frozen=True, slots=True)
class OnlineUpdateRuntimeState:
    last_update_at: datetime | None
    updates_in_window: int

    def __post_init__(self) -> None:
        if self.last_update_at is not None:
            object.__setattr__(
                self,
                "last_update_at",
                _time(self.last_update_at, name="last_update_at"),
            )
        if (
            not isinstance(self.updates_in_window, int)
            or isinstance(self.updates_in_window, bool)
            or self.updates_in_window < 0
        ):
            raise ValueError(
                "updates_in_window must be a non-negative integer"
            )


@dataclass(frozen=True, slots=True)
class ProducedOnlineUpdate:
    status: str
    artifact_bytes: bytes
    artifact_digest: str
    proposed_parameters: Mapping[str, Decimal] | None
    drift_score: Decimal | None
    calibrated_threshold: Decimal | None
    online_decision: OnlineUpdateDecision | None
    update_input: OnlineUpdateInput | None

    def __post_init__(self) -> None:
        if self.status not in {
            "UPDATE_PROPOSED",
            "NO_UPDATE",
            "CANDIDATE_REQUIRED",
            "STOP",
        }:
            raise ValueError("unsupported produced update status")
        if not isinstance(self.artifact_bytes, bytes):
            raise TypeError("artifact_bytes must be bytes")
        if _digest_bytes(self.artifact_bytes) != self.artifact_digest:
            raise ValueError("artifact_digest does not match artifact_bytes")
        if self.proposed_parameters is not None:
            object.__setattr__(
                self,
                "proposed_parameters",
                MappingProxyType(dict(self.proposed_parameters)),
            )


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    parameters: Mapping[str, Decimal]
    reference_feature_means: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class _LearningRow:
    episode_id: str
    episode_hash: str
    observation_id: str
    physical_observation_id: str
    features: Mapping[str, Decimal]
    target: Decimal
    label_version: str
    label_available_at: datetime
    outcome_horizon_at: datetime
    execution_reconciled_at: datetime
    reconciliation_evidence: Mapping[str, Any] | None
    correction_hashes: tuple[str, ...]


def _load_checkpoint(
    artifact_store: ArtifactStore,
    reference: str,
    *,
    config: UpdateProducerConfig,
    envelope: OnlineUpdateEnvelope,
) -> tuple[str, _Checkpoint, datetime]:
    ref, manifest, payload = _artifact_ref(
        artifact_store,
        reference,
        name="checkpoint_ref",
    )
    metadata = manifest.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("artifact_kind") != "BOUNDED_LINEAR_CHECKPOINT"
    ):
        raise ValueError(
            "checkpoint_ref must be a registered BOUNDED_LINEAR_CHECKPOINT"
        )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("checkpoint artifact is not valid JSON") from error
    if _canonical_bytes(decoded) != payload:
        raise ValueError("checkpoint artifact must use canonical JSON bytes")
    if not isinstance(decoded, dict):
        raise ValueError("checkpoint artifact must be an object")
    expected_scalar = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_kind": "BOUNDED_LINEAR_CHECKPOINT",
        "algorithm_id": _ALGORITHM_ID,
        "algorithm_version": config.algorithm_version,
        "feature_schema_hash": config.feature_schema_hash,
        "label_version": config.label_version,
    }
    if any(decoded.get(key) != value for key, value in expected_scalar.items()):
        raise ValueError(
            "checkpoint algorithm/schema/version binding does not match producer"
        )
    digest = "sha256:" + ref.rsplit("@sha256:", 1)[1]
    if envelope.champion_artifact_hash != digest:
        raise ValueError(
            "online envelope champion hash does not match checkpoint artifact"
        )

    names = tuple(
        sorted(rule.name for rule in envelope.parameter_rules)
    )
    raw_parameters = decoded.get("parameters")
    raw_means = decoded.get("reference_feature_means")
    if not isinstance(raw_parameters, dict) or not isinstance(raw_means, dict):
        raise ValueError(
            "checkpoint requires parameters and reference_feature_means"
        )
    if tuple(sorted(raw_parameters)) != names or tuple(sorted(raw_means)) != names:
        raise ValueError(
            "checkpoint parameter/feature set must match online envelope"
        )
    parameters = {
        name: _decimal(raw_parameters[name], name=f"parameter[{name}]")
        for name in names
    }
    means = {
        name: _decimal(raw_means[name], name=f"reference_feature_mean[{name}]")
        for name in names
    }
    checkpoint_created_at = _time(
        manifest.get("created_at"),
        name="checkpoint created_at",
    )
    return ref, _Checkpoint(
        parameters=MappingProxyType(parameters),
        reference_feature_means=MappingProxyType(means),
    ), checkpoint_created_at


def _verify_registered_evidence(
    artifact_store: ArtifactStore,
    *,
    config: UpdateProducerConfig,
    calibration_ref: str,
    calibration_population: CoveragePopulationSnapshot,
) -> tuple[str, tuple[str, ...]]:
    calibration, manifest, _payload = _artifact_ref(
        artifact_store,
        calibration_ref,
        name="calibration_evidence_ref",
    )
    metadata = manifest.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("artifact_kind")
        != "DRIFT_CALIBRATION_POPULATION"
        or metadata.get("population_root_hash")
        != calibration_population.root_hash
    ):
        raise ValueError(
            "calibration evidence does not bind the canonical calibration population"
        )

    verified_tests: list[str] = []
    for reference in config.test_evidence_refs:
        ref, test_manifest, _bytes = _artifact_ref(
            artifact_store,
            reference,
            name="test_evidence_ref",
        )
        test_meta = test_manifest.get("metadata")
        if (
            not isinstance(test_meta, dict)
            or test_meta.get("artifact_kind")
            != "QUALIFICATION_TEST_EVIDENCE"
            or test_meta.get("source_sha") != config.source_sha
        ):
            raise ValueError(
                "test evidence must be registered for the exact producer source SHA"
            )
        verified_tests.append(ref)
    return calibration, tuple(sorted(verified_tests))


def _physical_observation_identity(
    artifact_store: ArtifactStore,
    raw: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[str, datetime]:
    """Derive physical identity and authoritative local availability.

    Caller-owned decision/information/learning timestamps are deliberately
    excluded from physical identity. Evidence availability is taken only from
    an integrity-bound ArtifactStore manifest. A caller therefore cannot
    backdate immutable evidence by writing an earlier label_available_at.
    """

    raw_refs = payload.get("evidence_refs")
    if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
        raise ValueError("physical observation requires evidence references")

    resolved_evidence: list[tuple[str, str]] = []
    availability_points: list[datetime] = []
    seen_refs: set[str] = set()
    for raw_ref in raw_refs:
        reference, manifest, evidence_bytes = _artifact_ref(
            artifact_store,
            raw_ref,
            name="physical_evidence_ref",
        )
        if reference in seen_refs:
            raise ValueError(
                "physical observation evidence references must be unique"
            )
        seen_refs.add(reference)
        _sha256_identity(
            manifest.get("manifest_hash"),
            name="physical evidence manifest_hash",
        )
        digest = _sha256_identity(
            manifest.get("sha256"),
            name="physical evidence digest",
        )
        if _digest_bytes(evidence_bytes) != digest:
            raise ValueError(
                "physical observation evidence bytes changed after resolution"
            )
        available_at = _time(
            manifest.get("created_at"),
            name="physical evidence created_at",
        )
        availability_points.append(available_at)
        resolved_evidence.append((digest, _iso(available_at)))

    material = {
        "schema_version": "3.0.0",
        "instrument_family": _text(
            raw.get("instrument_family"),
            name="instrument_family",
        ),
        "evidence": tuple(sorted(resolved_evidence)),
    }
    return (
        _digest_bytes(_canonical_bytes(material)),
        max(availability_points),
    )


def _extract_learning_rows(
    population: CoveragePopulationSnapshot,
    *,
    artifact_store: ArtifactStore,
    cutoff: datetime,
    config: UpdateProducerConfig,
    feature_names: tuple[str, ...],
    reconciliation_evidence_resolver: Callable[[str], Mapping[str, Any]] | None,
) -> tuple[
    tuple[_LearningRow, ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[str, ...],
]:
    population.verify_integrity()
    selected: list[_LearningRow] = []
    exclusions: list[tuple[str, str]] = []

    for raw in population.rows:
        episode_id = _text(raw.get("episode_id"), name="episode_id")
        episode_hash = _sha256_identity(
            raw.get("episode_hash"),
            name="episode_hash",
        )
        tombstones = raw.get("tombstone_lineage")
        if isinstance(tombstones, tuple) and tombstones:
            exclusions.append((episode_id, "TOMBSTONED"))
            continue
        payload = raw.get("effective_payload")
        if not isinstance(payload, Mapping):
            exclusions.append((episode_id, "LEARNING_PAYLOAD_MISSING"))
            continue
        learning = payload.get("learning")
        if not isinstance(learning, Mapping):
            exclusions.append((episode_id, "LEARNING_PAYLOAD_MISSING"))
            continue
        try:
            observation_id = _text(
                learning.get("observation_id"),
                name="observation_id",
            )
            (
                base_physical_observation_id,
                physical_evidence_available_at,
            ) = _physical_observation_identity(
                artifact_store,
                raw,
                payload,
            )
            label_version = _text(
                learning.get("label_version"),
                name="label_version",
            )
            label_available = _time(
                learning.get("label_available_at"),
                name="label_available_at",
            )
            horizon = _time(
                learning.get("outcome_horizon_at"),
                name="outcome_horizon_at",
            )
            features_raw = learning.get("features")
            if not isinstance(features_raw, Mapping):
                raise ValueError("features must be a mapping")
            if tuple(sorted(features_raw)) != feature_names:
                raise ValueError("feature set does not match frozen schema")
            features = {
                name: _decimal(
                    features_raw[name],
                    name=f"features[{name}]",
                )
                for name in feature_names
            }
            target = _decimal(learning.get("target"), name="target")
        except (TypeError, ValueError):
            exclusions.append((episode_id, "LEARNING_SCHEMA_INVALID"))
            continue

        if label_version != config.label_version:
            exclusions.append((episode_id, "LABEL_VERSION_INELIGIBLE"))
            continue
        if (
            label_available > cutoff
            or horizon > cutoff
            or label_available < horizon
        ):
            exclusions.append((episode_id, "LABEL_NOT_CAUSALLY_MATURE"))
            continue
        if physical_evidence_available_at > label_available:
            exclusions.append(
                (episode_id, "PHYSICAL_EVIDENCE_NOT_CAUSALLY_AVAILABLE")
            )
            continue

        intended = payload.get("intended_action")
        if not isinstance(intended, Mapping):
            exclusions.append((episode_id, "LEARNING_SCHEMA_INVALID"))
            continue
        try:
            action_side = _text(
                intended.get("side"),
                name="intended_action.side",
            ).upper()
        except (TypeError, ValueError):
            exclusions.append((episode_id, "LEARNING_SCHEMA_INVALID"))
            continue

        reconciliation_evidence: Mapping[str, Any] | None = None
        physical_observation_id = base_physical_observation_id
        if action_side == "NO_TRADE":
            reconciled = horizon
        else:
            try:
                resolved = resolve_reconciliation_evidence(
                    episode_id,
                    causal_cutoff=cutoff,
                    resolver=reconciliation_evidence_resolver,
                )
            except (TypeError, ValueError):
                exclusions.append(
                    (episode_id, "RECONCILIATION_EVIDENCE_INVALID")
                )
                continue
            if resolved.get("status") != "VERIFIED":
                exclusions.append(
                    (episode_id, "RECONCILIATION_EVIDENCE_UNVERIFIED")
                )
                continue
            reconciliation_evidence = MappingProxyType(dict(resolved))
            reconciled = _time(
                resolved.get("observed_at"),
                name="authority execution_reconciled_at",
            )
            if reconciled > cutoff or label_available < reconciled:
                exclusions.append(
                    (episode_id, "LABEL_NOT_CAUSALLY_MATURE")
                )
                continue
            physical_observation_id = _digest_bytes(
                _canonical_bytes(
                    {
                        "physical_observation_id": base_physical_observation_id,
                        "reconciliation_evidence": dict(resolved),
                    }
                )
            )

        corrections = raw.get("correction_lineage")
        correction_hashes: list[str] = []
        if isinstance(corrections, tuple):
            for correction in corrections:
                if not isinstance(correction, Mapping):
                    raise ValueError(
                        "canonical correction lineage is malformed"
                    )
                correction_hashes.append(
                    _sha256_identity(
                        correction.get("correction_hash"),
                        name="correction_hash",
                    )
                )
        selected.append(
            _LearningRow(
                episode_id=episode_id,
                episode_hash=episode_hash,
                observation_id=observation_id,
                physical_observation_id=physical_observation_id,
                features=MappingProxyType(features),
                target=target,
                label_version=label_version,
                label_available_at=label_available,
                outcome_horizon_at=horizon,
                execution_reconciled_at=reconciled,
                reconciliation_evidence=reconciliation_evidence,
                correction_hashes=tuple(sorted(correction_hashes)),
            )
        )

    groups: dict[str, list[_LearningRow]] = {}
    caller_aliases: dict[str, set[str]] = {}
    for row in selected:
        groups.setdefault(row.physical_observation_id, []).append(row)
        caller_aliases.setdefault(row.observation_id, set()).add(
            row.physical_observation_id
        )

    deduped: list[_LearningRow] = []
    aliases: list[tuple[str, tuple[str, ...]]] = []
    conflicts: list[str] = []
    for physical_id in sorted(groups):
        group = sorted(groups[physical_id], key=lambda item: item.episode_id)
        semantic = {
            (
                tuple(sorted((key, str(value)) for key, value in row.features.items())),
                str(row.target),
                row.label_version,
                _iso(row.label_available_at),
                _iso(row.outcome_horizon_at),
                _iso(row.execution_reconciled_at),
            )
            for row in group
        }
        episode_ids = tuple(row.episode_id for row in group)
        aliases.append((physical_id, episode_ids))
        if len(semantic) != 1:
            conflicts.append(physical_id)
            for row in group:
                exclusions.append(
                    (row.episode_id, "PHYSICAL_SEMANTIC_CONFLICT")
                )
            continue
        deduped.append(group[0])
        for duplicate in group[1:]:
            exclusions.append((duplicate.episode_id, "PHYSICAL_DUPLICATE"))

    # Caller aliases remain useful diagnostic labels but cannot define physical
    # identity. Reusing one alias for multiple evidence-backed observations is
    # ambiguous and therefore fails closed rather than merging distinct facts.
    for observation_id, physical_ids in sorted(caller_aliases.items()):
        if len(physical_ids) > 1:
            conflicts.append("caller-alias:" + observation_id)

    deduped.sort(key=lambda row: (row.label_available_at, row.episode_id))
    exclusions.sort()
    aliases.sort()
    return (
        tuple(deduped),
        tuple(exclusions),
        tuple(aliases),
        tuple(sorted(set(conflicts))),
    )


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("cannot compute mean of empty evidence")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _drift_score(
    row: _LearningRow,
    reference: Mapping[str, Decimal],
) -> Decimal:
    return _mean(
        [
            abs(row.features[name] - reference[name])
            for name in sorted(reference)
        ]
    )


def _calibrate_threshold(
    rows: Sequence[_LearningRow],
    *,
    reference: Mapping[str, Decimal],
    target_false_alarm_rate: Decimal,
) -> tuple[Decimal, int, int]:
    scores = sorted(_drift_score(row, reference) for row in rows)
    count = len(scores)
    rank = (
        (Decimal("1") - target_false_alarm_rate) * Decimal(count)
    ).to_integral_value(rounding=ROUND_CEILING)
    index = max(0, min(count - 1, int(rank) - 1))
    threshold = scores[index]
    false_alarms = sum(1 for score in scores if score > threshold)
    return threshold, false_alarms, count


def _proposal(
    rows: Sequence[_LearningRow],
    *,
    checkpoint: _Checkpoint,
    learning_rate: Decimal,
) -> dict[str, Decimal]:
    names = tuple(sorted(checkpoint.parameters))
    gradients = {name: Decimal("0") for name in names}
    for row in rows:
        prediction = sum(
            (
                checkpoint.parameters[name] * row.features[name]
                for name in names
            ),
            Decimal("0"),
        )
        error = row.target - prediction
        for name in names:
            gradients[name] += error * row.features[name]
    denominator = Decimal(len(rows))
    return {
        name: checkpoint.parameters[name]
        + learning_rate * (gradients[name] / denominator)
        for name in names
    }


def _row_evidence(row: _LearningRow) -> dict[str, Any]:
    return {
        "episode_id": row.episode_id,
        "episode_hash": row.episode_hash,
        "observation_id": row.observation_id,
        "physical_observation_id": row.physical_observation_id,
        "features": {
            name: str(value)
            for name, value in sorted(row.features.items())
        },
        "target": str(row.target),
        "label_version": row.label_version,
        "label_available_at": _iso(row.label_available_at),
        "outcome_horizon_at": _iso(row.outcome_horizon_at),
        "execution_reconciled_at": _iso(row.execution_reconciled_at),
        "reconciliation_evidence": (
            None
            if row.reconciliation_evidence is None
            else dict(row.reconciliation_evidence)
        ),
        "correction_hashes": list(row.correction_hashes),
    }


def _producer_config_evidence(config: UpdateProducerConfig) -> dict[str, Any]:
    value = {
        "source_sha": config.source_sha,
        "algorithm_id": _ALGORITHM_ID,
        "algorithm_version": config.algorithm_version,
        "feature_schema_hash": config.feature_schema_hash,
        "label_version": config.label_version,
        "learning_rate": str(config.learning_rate),
        "min_update_episodes": config.min_update_episodes,
        "min_calibration_episodes": config.min_calibration_episodes,
        "target_false_alarm_rate": str(config.target_false_alarm_rate),
        "max_compute_units": str(config.max_compute_units),
        "update_task": config.update_task,
        "calibration_task": config.calibration_task,
        "test_evidence_refs": sorted(config.test_evidence_refs),
        "instrument_family": config.instrument_family,
        "protocol_id": config.protocol_id,
    }
    return value


def _online_envelope_evidence(
    envelope: OnlineUpdateEnvelope,
) -> dict[str, Any]:
    return {
        "envelope_id": envelope.envelope_id,
        "champion_artifact_hash": envelope.champion_artifact_hash,
        "parameter_rules": [
            {
                "name": rule.name,
                "minimum": str(rule.minimum),
                "maximum": str(rule.maximum),
                "max_absolute_step": str(rule.max_absolute_step),
            }
            for rule in sorted(
                envelope.parameter_rules,
                key=lambda item: item.name,
            )
        ],
        "eligible_label_versions": sorted(
            envelope.eligible_label_versions
        ),
        "min_seconds_between_updates": envelope.min_seconds_between_updates,
        "max_updates_per_window": envelope.max_updates_per_window,
        "max_compute_units_per_update": str(
            envelope.max_compute_units_per_update
        ),
        "max_drift_score": str(envelope.max_drift_score),
    }



def _coverage_selection(
    rows: tuple[_LearningRow, ...],
    exclusions: tuple[tuple[str, str], ...],
    aliases: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Map deduplicated learner rows back to the complete episode population.

    Multiple immutable episodes may be aliases of one physical observation.
    They remain first-class population members even though the gradient consumes
    the physical fact once. Only episodes that are genuinely unusable are
    exclusions in the canonical population manifest.
    """

    accepted_physical = {row.physical_observation_id for row in rows}
    included: set[str] = set()
    for physical_id, episode_ids in aliases:
        if physical_id in accepted_physical:
            included.update(episode_ids)

    exclusion_map: dict[str, str] = {}
    for episode_id, reason in exclusions:
        if episode_id in included and reason == "PHYSICAL_DUPLICATE":
            continue
        if episode_id in included:
            # An accepted physical fact cannot simultaneously be excluded from
            # population coverage for another reason.
            raise ValueError(
                "accepted learning episode has incompatible population exclusion"
            )
        prior = exclusion_map.get(episode_id)
        if prior is not None and prior != reason:
            raise ValueError(
                "learning episode has multiple incompatible exclusion reasons"
            )
        exclusion_map[episode_id] = reason
    return tuple(sorted(included)), exclusion_map


def _population_authority_reason(
    manifest: PopulationCoverageManifest | None,
    *,
    population: CoveragePopulationSnapshot,
    rows: tuple[_LearningRow, ...],
    exclusions: tuple[tuple[str, str], ...],
    aliases: tuple[tuple[str, tuple[str, ...]], ...],
    candidate_hash: str,
    cutoff: datetime,
    permission_classes: tuple[str, ...],
    task: str,
    instrument_family: str | None,
    population_name: str,
    reconciliation_evidence_resolver: Callable[[str], Mapping[str, Any]] | None,
) -> tuple[str | None, PopulationCoverageManifest | None]:
    """Verify one caller-supplied manifest against the canonical memory cut."""

    if manifest is None:
        return f"LEARNING.{population_name}_POPULATION_COVERAGE_REQUIRED", None
    if not isinstance(manifest, PopulationCoverageManifest):
        raise TypeError(
            f"{population_name.lower()}_population_manifest must be PopulationCoverageManifest"
        )
    if manifest.candidate_hash != candidate_hash:
        return f"LEARNING.{population_name}_POPULATION_CANDIDATE_MISMATCH", manifest

    included_episode_ids, coverage_exclusions = _coverage_selection(
        rows,
        exclusions,
        aliases,
    )
    try:
        expected = build_population_coverage(
            population,
            candidate_hash=candidate_hash,
            frozen_protocol_hash=manifest.frozen_protocol_hash,
            input_snapshot_hash=population.root_hash,
            causal_cutoff=cutoff,
            permission_classes=permission_classes,
            included_episode_ids=included_episode_ids,
            exclusions=coverage_exclusions,
            reconciliation_evidence_resolver=reconciliation_evidence_resolver,
            task=task,
            instrument_family=instrument_family,
        )
    except (TypeError, ValueError):
        return f"LEARNING.{population_name}_POPULATION_CANONICAL_INVALID", manifest

    if manifest != expected:
        return f"LEARNING.{population_name}_POPULATION_COVERAGE_MISMATCH", manifest
    if not manifest.complete:
        return f"LEARNING.{population_name}_POPULATION_COVERAGE_INCOMPLETE", manifest

    outcome_counts = {
        outcome_class: count
        for outcome_class, count, _digest in manifest.included_outcomes
    }
    labels_complete = all(
        complete
        for _regime, complete in manifest.included_labels_complete_by_regime
    )
    if (
        not labels_complete
        or outcome_counts.get("PENDING", 0) != 0
        or outcome_counts.get("UNKNOWN", 0) != 0
    ):
        return f"LEARNING.{population_name}_OUTCOME_EVIDENCE_INCOMPLETE", manifest
    return None, manifest



def _scientific_preregistration_reason(
    scientific_registry: ScientificRegistry | None,
    *,
    config: UpdateProducerConfig,
    calibration_population: CoveragePopulationSnapshot,
    calibration_cutoff: datetime,
    permission_classes: tuple[str, ...],
    checkpoint_created_at: datetime,
    update_manifest: PopulationCoverageManifest | None,
    calibration_manifest: PopulationCoverageManifest | None,
) -> tuple[str | None, ProtocolRegistration | None]:
    """Verify independent, time-ordered preregistration for this update cut."""

    if config.protocol_id is None or scientific_registry is None:
        return "LEARNING.SCIENTIFIC_PREREGISTRATION_REQUIRED", None
    if not isinstance(scientific_registry, ScientificRegistry):
        raise TypeError("scientific_registry must be ScientificRegistry or None")
    if update_manifest is None or calibration_manifest is None:
        return None, None
    if (
        update_manifest.frozen_protocol_hash
        != calibration_manifest.frozen_protocol_hash
    ):
        return None, None
    try:
        registration, protocol = scientific_registry.protocol_document(
            config.protocol_id
        )
    except KeyError:
        return "LEARNING.SCIENTIFIC_PREREGISTRATION_REQUIRED", None

    scope = protocol.get("online_update_registration")
    expected_scope = {
        "schema_version": "1.0.0",
        "calibration_population_root_hash": calibration_population.root_hash,
        "calibration_cutoff": _iso(calibration_cutoff),
        "update_task": config.update_task,
        "calibration_task": config.calibration_task,
        "instrument_family": config.instrument_family,
        "permission_classes": list(permission_classes),
        "feature_schema_hash": config.feature_schema_hash,
        "label_version": config.label_version,
        "source_sha": config.source_sha,
    }
    if not isinstance(scope, dict) or scope != expected_scope:
        return "LEARNING.SCIENTIFIC_PREREGISTRATION_SCOPE_MISMATCH", None
    if registration.protocol_hash != update_manifest.frozen_protocol_hash:
        return "LEARNING.SCIENTIFIC_PREREGISTRATION_HASH_MISMATCH", None
    registered_at = _time(
        registration.created_at,
        name="protocol registration created_at",
    )
    if registered_at >= checkpoint_created_at:
        return "LEARNING.SCIENTIFIC_PREREGISTRATION_LATE", None
    return None, registration


def produce_bounded_online_update(
    *,
    memory: ExperienceMemory,
    artifact_store: ArtifactStore,
    checkpoint_ref: str,
    calibration_evidence_ref: str,
    envelope: OnlineUpdateEnvelope,
    config: UpdateProducerConfig,
    runtime_state: OnlineUpdateRuntimeState,
    update_cutoff: datetime,
    calibration_cutoff: datetime,
    granted_permissions: set[str],
    update_population_manifest: PopulationCoverageManifest | None = None,
    calibration_population_manifest: PopulationCoverageManifest | None = None,
    scientific_registry: ScientificRegistry | None = None,
    reconciliation_evidence_resolver: Callable[[str], Mapping[str, Any]] | None = None,
) -> ProducedOnlineUpdate:
    if not isinstance(memory, ExperienceMemory):
        raise TypeError("memory must be ExperienceMemory")
    if not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact_store must be ArtifactStore")
    if not isinstance(envelope, OnlineUpdateEnvelope):
        raise TypeError("envelope must be OnlineUpdateEnvelope")
    if not isinstance(config, UpdateProducerConfig):
        raise TypeError("config must be UpdateProducerConfig")
    if not isinstance(runtime_state, OnlineUpdateRuntimeState):
        raise TypeError("runtime_state must be OnlineUpdateRuntimeState")
    if (
        reconciliation_evidence_resolver is not None
        and not callable(reconciliation_evidence_resolver)
    ):
        raise TypeError(
            "reconciliation_evidence_resolver must be callable or None"
        )
    update_time = _time(update_cutoff, name="update_cutoff")
    calibration_time = _time(
        calibration_cutoff,
        name="calibration_cutoff",
    )
    if calibration_time >= update_time:
        raise ValueError(
            "drift calibration cutoff must be strictly before update cutoff"
        )
    if not isinstance(granted_permissions, set) or not granted_permissions:
        raise ValueError("granted_permissions must be a non-empty set")
    canonical_permissions = tuple(
        sorted(
            _text(value, name="granted_permission")
            for value in granted_permissions
        )
    )

    checkpoint_reference, checkpoint, checkpoint_created_at = _load_checkpoint(
        artifact_store,
        checkpoint_ref,
        config=config,
        envelope=envelope,
    )
    update_population = memory.coverage_population_snapshot(
        causal_cutoff=update_time,
        granted_permissions=set(canonical_permissions),
        task=config.update_task,
        instrument_family=config.instrument_family,
    )
    calibration_population = memory.coverage_population_snapshot(
        causal_cutoff=calibration_time,
        granted_permissions=set(canonical_permissions),
        task=config.calibration_task,
        instrument_family=config.instrument_family,
    )
    calibration_reference, test_references = _verify_registered_evidence(
        artifact_store,
        config=config,
        calibration_ref=calibration_evidence_ref,
        calibration_population=calibration_population,
    )

    feature_names = tuple(sorted(checkpoint.parameters))
    (
        update_rows,
        update_exclusions,
        update_aliases,
        update_conflicts,
    ) = _extract_learning_rows(
        update_population,
        artifact_store=artifact_store,
        cutoff=update_time,
        config=config,
        feature_names=feature_names,
        reconciliation_evidence_resolver=reconciliation_evidence_resolver,
    )
    (
        calibration_rows,
        calibration_exclusions,
        calibration_aliases,
        calibration_conflicts,
    ) = _extract_learning_rows(
        calibration_population,
        artifact_store=artifact_store,
        cutoff=calibration_time,
        config=config,
        feature_names=feature_names,
        reconciliation_evidence_resolver=reconciliation_evidence_resolver,
    )

    cross_population_overlap = tuple(
        sorted(
            {row.physical_observation_id for row in update_rows}
            & {row.physical_observation_id for row in calibration_rows}
        )
    )

    checkpoint_candidate_hash = _sha256_identity(
        "sha256:" + checkpoint_reference.rsplit("@sha256:", 1)[1],
        name="checkpoint candidate hash",
    )
    update_population_reason, verified_update_manifest = (
        _population_authority_reason(
            update_population_manifest,
            population=update_population,
            rows=update_rows,
            exclusions=update_exclusions,
            aliases=update_aliases,
            candidate_hash=checkpoint_candidate_hash,
            cutoff=update_time,
            permission_classes=canonical_permissions,
            task=config.update_task,
            instrument_family=config.instrument_family,
            population_name="UPDATE",
            reconciliation_evidence_resolver=reconciliation_evidence_resolver,
        )
    )
    calibration_population_reason, verified_calibration_manifest = (
        _population_authority_reason(
            calibration_population_manifest,
            population=calibration_population,
            rows=calibration_rows,
            exclusions=calibration_exclusions,
            aliases=calibration_aliases,
            candidate_hash=checkpoint_candidate_hash,
            cutoff=calibration_time,
            permission_classes=canonical_permissions,
            task=config.calibration_task,
            instrument_family=config.instrument_family,
            population_name="CALIBRATION",
            reconciliation_evidence_resolver=reconciliation_evidence_resolver,
        )
    )

    preregistration_reason, protocol_registration = (
        _scientific_preregistration_reason(
            scientific_registry,
            config=config,
            calibration_population=calibration_population,
            calibration_cutoff=calibration_time,
            permission_classes=canonical_permissions,
            checkpoint_created_at=checkpoint_created_at,
            update_manifest=verified_update_manifest,
            calibration_manifest=verified_calibration_manifest,
        )
    )

    reasons: list[str] = []
    if preregistration_reason is not None:
        reasons.append(preregistration_reason)
    if update_population_reason is not None:
        reasons.append(update_population_reason)
    if calibration_population_reason is not None:
        reasons.append(calibration_population_reason)
    if (
        verified_update_manifest is not None
        and verified_calibration_manifest is not None
        and verified_update_manifest.frozen_protocol_hash
        != verified_calibration_manifest.frozen_protocol_hash
    ):
        reasons.append("LEARNING.POPULATION_PROTOCOL_MISMATCH")
    if cross_population_overlap:
        reasons.append("LEARNING.CROSS_POPULATION_CONTAMINATION")
    if update_conflicts:
        reasons.append("LEARNING.UPDATE_ALIAS_CONFLICT")
    if calibration_conflicts:
        reasons.append("LEARNING.CALIBRATION_ALIAS_CONFLICT")
    if len(update_rows) < config.min_update_episodes:
        reasons.append("LEARNING.INSUFFICIENT_UPDATE_EVIDENCE")
    if len(calibration_rows) < config.min_calibration_episodes:
        reasons.append("LEARNING.INSUFFICIENT_CALIBRATION_EVIDENCE")

    compute_units = Decimal(
        (len(update_rows) + len(calibration_rows)) * len(feature_names)
    )
    if compute_units > config.max_compute_units:
        reasons.append("LEARNING.PRODUCER_COMPUTE_BUDGET_EXCEEDED")

    threshold: Decimal | None = None
    false_alarms = 0
    false_alarm_denominator = 0
    drift: Decimal | None = None
    proposed: dict[str, Decimal] | None = None
    update_input: OnlineUpdateInput | None = None
    decision: OnlineUpdateDecision | None = None
    status = "NO_UPDATE"

    if not reasons:
        threshold, false_alarms, false_alarm_denominator = _calibrate_threshold(
            calibration_rows,
            reference=checkpoint.reference_feature_means,
            target_false_alarm_rate=config.target_false_alarm_rate,
        )
        drift = _mean(
            [
                _drift_score(
                    row,
                    checkpoint.reference_feature_means,
                )
                for row in update_rows
            ]
        )
        if drift > threshold:
            reasons.append("LEARNING.CALIBRATED_DRIFT_LIMIT_EXCEEDED")
        else:
            proposed = _proposal(
                update_rows,
                checkpoint=checkpoint,
                learning_rate=config.learning_rate,
            )
            latest_label = max(
                row.label_available_at for row in update_rows
            )
            latest_horizon = max(
                row.outcome_horizon_at for row in update_rows
            )
            latest_reconciled = max(
                row.execution_reconciled_at for row in update_rows
            )
            update_input = OnlineUpdateInput.create(
                current_parameters=checkpoint.parameters,
                proposed_parameters=proposed,
                label_version=config.label_version,
                label_available_at=latest_label,
                outcome_horizon_at=latest_horizon,
                execution_reconciled_at=latest_reconciled,
                observed_at=update_time,
                last_update_at=runtime_state.last_update_at,
                updates_in_window=runtime_state.updates_in_window,
                reserved_compute_units=compute_units,
                drift_score=drift,
            )
            decision = evaluate_online_update(envelope, update_input)
            if decision.status == "ALLOW":
                status = "UPDATE_PROPOSED"
            else:
                status = decision.status
                reasons.extend(decision.reasons)

    producer_config = _producer_config_evidence(config)
    online_envelope = _online_envelope_evidence(envelope)
    artifact = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_kind": "ONLINE_UPDATE_PRODUCTION_RESULT",
        "status": status,
        "reasons": sorted(set(reasons)),
        "source": {
            "source_sha": config.source_sha,
            "algorithm_id": _ALGORITHM_ID,
            "algorithm_version": config.algorithm_version,
            "feature_schema_hash": config.feature_schema_hash,
            "rng_seed": None,
            "unresolved_limits": [
                "bounded linear estimator family only",
                "economic edge remains unproven",
                "artifact grants no trading or promotion authority",
            ],
        },
        "producer_config": producer_config,
        "producer_config_sha256": _digest_bytes(
            _canonical_bytes(producer_config)
        ),
        "online_envelope": online_envelope,
        "online_envelope_sha256": _digest_bytes(
            _canonical_bytes(online_envelope)
        ),
        "runtime_state": {
            "last_update_at": (
                None
                if runtime_state.last_update_at is None
                else _iso(runtime_state.last_update_at)
            ),
            "updates_in_window": runtime_state.updates_in_window,
        },
        "granted_permissions": list(canonical_permissions),
        "scientific_registration": (
            None
            if protocol_registration is None
            else {
                "protocol_id": protocol_registration.protocol_id,
                "protocol_hash": protocol_registration.protocol_hash,
                "registered_at": protocol_registration.created_at,
                "checkpoint_created_at": _iso(checkpoint_created_at),
            }
        ),
        "checkpoint": {
            "artifact_ref": checkpoint_reference,
            "parameters": {
                name: str(value)
                for name, value in sorted(checkpoint.parameters.items())
            },
            "reference_feature_means": {
                name: str(value)
                for name, value in sorted(
                    checkpoint.reference_feature_means.items()
                )
            },
        },
        "evidence": {
            "update_population_root": update_population.root_hash,
            "update_cutoff": _iso(update_time),
            "update_task": config.update_task,
            "calibration_population_root": calibration_population.root_hash,
            "calibration_cutoff": _iso(calibration_time),
            "calibration_task": config.calibration_task,
            "calibration_evidence_ref": calibration_reference,
            "test_evidence_refs": list(test_references),
            "label_version": config.label_version,
            "population_authority": {
                "candidate_hash": checkpoint_candidate_hash,
                "update_manifest_digest": (
                    None
                    if verified_update_manifest is None
                    else verified_update_manifest.digest
                ),
                "calibration_manifest_digest": (
                    None
                    if verified_calibration_manifest is None
                    else verified_calibration_manifest.digest
                ),
                "frozen_protocol_hash": (
                    None
                    if verified_update_manifest is None
                    or verified_calibration_manifest is None
                    or verified_update_manifest.frozen_protocol_hash
                    != verified_calibration_manifest.frozen_protocol_hash
                    else verified_update_manifest.frozen_protocol_hash
                ),
            },
        },
        "population": {
            "update_included": [
                _row_evidence(row) for row in update_rows
            ],
            "update_exclusions": [
                list(value) for value in update_exclusions
            ],
            "update_alias_groups": [
                [physical_observation_id, list(episode_ids)]
                for physical_observation_id, episode_ids in update_aliases
            ],
            "calibration_included": [
                _row_evidence(row) for row in calibration_rows
            ],
            "calibration_exclusions": [
                list(value) for value in calibration_exclusions
            ],
            "calibration_alias_groups": [
                [physical_observation_id, list(episode_ids)]
                for physical_observation_id, episode_ids in calibration_aliases
            ],
            "cross_population_physical_overlap": list(
                cross_population_overlap
            ),
        },
        "calibration": {
            "target_false_alarm_rate": str(
                config.target_false_alarm_rate
            ),
            "threshold": None if threshold is None else str(threshold),
            "false_alarm_count": false_alarms,
            "false_alarm_denominator": false_alarm_denominator,
            "measured_false_alarm_rate": (
                None
                if false_alarm_denominator == 0
                else str(
                    Decimal(false_alarms)
                    / Decimal(false_alarm_denominator)
                )
            ),
            "drift_score": None if drift is None else str(drift),
        },
        "compute": {
            "estimated_compute_units": str(compute_units),
            "max_compute_units": str(config.max_compute_units),
        },
        "proposal": (
            None
            if proposed is None
            else {
                name: str(value)
                for name, value in sorted(proposed.items())
            }
        ),
        "online_gate": (
            None
            if decision is None
            else {
                "decision_id": decision.decision_id,
                "status": decision.status,
                "reasons": list(decision.reasons),
                "grants_trading_authority": decision.grants_trading_authority,
            }
        ),
    }
    data = _canonical_bytes(artifact)
    return ProducedOnlineUpdate(
        status=status,
        artifact_bytes=data,
        artifact_digest=_digest_bytes(data),
        proposed_parameters=proposed,
        drift_score=drift,
        calibrated_threshold=threshold,
        online_decision=decision,
        update_input=update_input,
    )


def publish_online_update_result(
    *,
    job_store: ResearchJobStore,
    job_id: str,
    worker_id: str,
    generation: int,
    artifact_store: ArtifactStore,
    produced: ProducedOnlineUpdate,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Publish deterministic producer bytes through the canonical job boundary."""
    if not isinstance(job_store, ResearchJobStore):
        raise TypeError("job_store must be ResearchJobStore")
    if not isinstance(produced, ProducedOnlineUpdate):
        raise TypeError("produced must be ProducedOnlineUpdate")
    if _digest_bytes(produced.artifact_bytes) != produced.artifact_digest:
        raise ValueError("produced artifact bytes changed before publication")
    return job_store.publish_result_bytes(
        job_id,
        worker_id=worker_id,
        generation=generation,
        artifact_store=artifact_store,
        data=produced.artifact_bytes,
        media_type="application/json",
        rights={"storage": True, "export": False},
        slot="wp38-online-update",
        now=now,
    )
