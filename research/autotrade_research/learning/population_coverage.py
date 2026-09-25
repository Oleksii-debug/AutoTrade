"""Population-completeness evidence for scientific retention qualification.

This module does not store experience and does not score or promote models. It
turns a verified causal projection from the canonical ExperienceMemory into one
immutable manifest so favorable metrics cannot silently omit eligible negative,
null/no-trade, unknown, or pending observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping, Sequence

from ..memory.episodes import CoveragePopulationSnapshot


_OUTCOME_CLASSES = ("POSITIVE", "NEGATIVE", "NULL", "UNKNOWN", "PENDING")


def _text(value, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _sha_identity(value, *, name: str) -> str:
    value = _text(value, name=name)
    if not value.startswith("sha256:"):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    digest = value[7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return value


def _time(value, *, name: str) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{name} must be an ISO timestamp") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{name} must be datetime or ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _canonical(value) -> str:
    if isinstance(value, float):
        raise TypeError("binary float is not permitted in population evidence")
    if isinstance(value, Mapping):
        normalized = {
            _text(key, name="population evidence key"): json.loads(_canonical(item))
            for key, item in value.items()
        }
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        normalized = [json.loads(_canonical(item)) for item in value]
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    raise TypeError(
        f"unsupported population evidence value type: {type(value).__name__}"
    )


def _digest(value) -> str:
    return "sha256:" + sha256(_canonical(value).encode("utf-8")).hexdigest()


def _summary(
    rows: Sequence[tuple[str, str, bool]],
) -> tuple[tuple[str, int, str], ...]:
    result = []
    for outcome_class in _OUTCOME_CLASSES:
        identities = sorted(
            digest
            for current_class, digest, _no_trade in rows
            if current_class == outcome_class
        )
        result.append((outcome_class, len(identities), _digest(identities)))
    return tuple(result)


@dataclass(frozen=True)
class PopulationCoverageManifest:
    candidate_hash: str
    frozen_protocol_hash: str
    input_snapshot_hash: str
    causal_cutoff: str
    permission_classes: tuple[str, ...]
    task: str | None
    instrument_family: str | None
    eligible_episode_ids: tuple[str, ...]
    included_episode_ids: tuple[str, ...]
    exclusions: tuple[tuple[str, str], ...]
    episode_digests: tuple[tuple[str, str], ...]
    eligible_outcomes: tuple[tuple[str, int, str], ...]
    included_outcomes: tuple[tuple[str, int, str], ...]
    eligible_no_trade_count: int
    included_no_trade_count: int
    included_regime_counts: tuple[tuple[str, int], ...]
    included_labels_complete_by_regime: tuple[tuple[str, bool], ...]
    digest: str

    def __post_init__(self) -> None:
        _sha_identity(self.candidate_hash, name="candidate_hash")
        _sha_identity(self.frozen_protocol_hash, name="frozen_protocol_hash")
        _sha_identity(self.input_snapshot_hash, name="input_snapshot_hash")
        _sha_identity(self.digest, name="population coverage digest")

        normalized_cutoff = _time(self.causal_cutoff, name="causal_cutoff")
        if normalized_cutoff != self.causal_cutoff:
            raise ValueError("causal_cutoff must use canonical UTC ISO form")
        if self.task is not None and _text(self.task, name="task") != self.task:
            raise ValueError("task must use canonical text")
        if (
            self.instrument_family is not None
            and _text(self.instrument_family, name="instrument_family")
            != self.instrument_family
        ):
            raise ValueError("instrument_family must use canonical text")

        if not isinstance(self.permission_classes, tuple) or not self.permission_classes:
            raise ValueError("permission_classes must be a non-empty tuple")
        normalized_permissions = tuple(
            _text(value, name="permission_class")
            for value in self.permission_classes
        )
        if normalized_permissions != self.permission_classes:
            raise ValueError("permission_classes must use canonical text")
        if tuple(sorted(set(normalized_permissions))) != normalized_permissions:
            raise ValueError("permission_classes must be sorted and unique")

        tuple_fields = (
            "eligible_episode_ids",
            "included_episode_ids",
            "exclusions",
            "episode_digests",
            "eligible_outcomes",
            "included_outcomes",
            "included_regime_counts",
            "included_labels_complete_by_regime",
        )
        if any(not isinstance(getattr(self, name), tuple) for name in tuple_fields):
            raise TypeError("population manifest collections must be immutable tuples")

        eligible_ids = tuple(
            _text(value, name="eligible episode_id")
            for value in self.eligible_episode_ids
        )
        included_ids = tuple(
            _text(value, name="included episode_id")
            for value in self.included_episode_ids
        )
        if eligible_ids != self.eligible_episode_ids or included_ids != self.included_episode_ids:
            raise ValueError("episode identities must use canonical text")
        if tuple(sorted(set(eligible_ids))) != eligible_ids:
            raise ValueError("eligible episode identities must be sorted and unique")
        if tuple(sorted(set(included_ids))) != included_ids:
            raise ValueError("included episode identities must be sorted and unique")

        normalized_exclusions = []
        for item in self.exclusions:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("exclusions must contain (episode_id, reason) tuples")
            episode_id = _text(item[0], name="excluded episode_id")
            reason = _text(item[1], name="exclusion reason")
            if (episode_id, reason) != item:
                raise ValueError("exclusions must use canonical text")
            normalized_exclusions.append(item)
        if tuple(sorted(normalized_exclusions)) != self.exclusions:
            raise ValueError("exclusions must be sorted")
        excluded_ids = tuple(episode_id for episode_id, _reason in self.exclusions)
        if len(excluded_ids) != len(set(excluded_ids)):
            raise ValueError("excluded episode identities must be unique")

        included = set(included_ids)
        excluded = set(excluded_ids)
        eligible = set(eligible_ids)
        if included & excluded:
            raise ValueError("episode cannot be both included and excluded")
        if included | excluded != eligible:
            raise ValueError("population manifest must account for every eligible episode")

        digest_ids = []
        for item in self.episode_digests:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("episode_digests must contain (episode_id, digest) tuples")
            episode_id = _text(item[0], name="episode digest episode_id")
            evidence_digest = _sha_identity(item[1], name="episode fact digest")
            if (episode_id, evidence_digest) != item:
                raise ValueError("episode_digests must use canonical values")
            digest_ids.append(episode_id)
        if tuple(sorted(self.episode_digests)) != self.episode_digests:
            raise ValueError("episode_digests must be sorted")
        if len(digest_ids) != len(set(digest_ids)) or set(digest_ids) != eligible:
            raise ValueError("episode_digests must cover every eligible episode exactly once")

        def validate_outcomes(value, *, name):
            if len(value) != len(_OUTCOME_CLASSES):
                raise ValueError(f"{name} must contain every canonical outcome class")
            result = {}
            for index, item in enumerate(value):
                if not isinstance(item, tuple) or len(item) != 3:
                    raise TypeError(f"{name} rows must be (class, count, digest) tuples")
                outcome_class, count, outcome_digest = item
                if outcome_class != _OUTCOME_CLASSES[index]:
                    raise ValueError(f"{name} must use canonical outcome ordering")
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError(f"{name} counts must be non-negative integers")
                _sha_identity(outcome_digest, name=f"{name} digest")
                result[outcome_class] = count
            return result

        eligible_counts = validate_outcomes(
            self.eligible_outcomes,
            name="eligible_outcomes",
        )
        included_counts = validate_outcomes(
            self.included_outcomes,
            name="included_outcomes",
        )
        if sum(eligible_counts.values()) != len(eligible_ids):
            raise ValueError("eligible outcome counts do not match eligible population")
        if sum(included_counts.values()) != len(included_ids):
            raise ValueError("included outcome counts do not match included population")
        for outcome_class in _OUTCOME_CLASSES:
            if included_counts[outcome_class] > eligible_counts[outcome_class]:
                raise ValueError("included outcome count exceeds eligible outcome count")

        for name, value in (
            ("eligible_no_trade_count", self.eligible_no_trade_count),
            ("included_no_trade_count", self.included_no_trade_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.eligible_no_trade_count > eligible_counts["NULL"]:
            raise ValueError("eligible NO_TRADE count cannot exceed NULL outcomes")
        if self.included_no_trade_count > included_counts["NULL"]:
            raise ValueError("included NO_TRADE count cannot exceed NULL outcomes")
        if self.included_no_trade_count > self.eligible_no_trade_count:
            raise ValueError("included NO_TRADE count cannot exceed eligible count")

        regime_ids = []
        total_regime_observations = 0
        for item in self.included_regime_counts:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("included_regime_counts must contain (regime, count) tuples")
            regime = _text(item[0], name="regime")
            count = item[1]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("regime observation count must be a non-negative integer")
            if (regime, count) != item:
                raise ValueError("regime identities must use canonical text")
            regime_ids.append(regime)
            total_regime_observations += count
        if tuple(sorted(self.included_regime_counts)) != self.included_regime_counts:
            raise ValueError("included_regime_counts must be sorted")
        if len(regime_ids) != len(set(regime_ids)):
            raise ValueError("included regime identities must be unique")
        if total_regime_observations != len(included_ids):
            raise ValueError("regime observation counts do not match included population")

        label_regimes = []
        for item in self.included_labels_complete_by_regime:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError(
                    "included_labels_complete_by_regime must contain (regime, bool) tuples"
                )
            regime = _text(item[0], name="label regime")
            complete = item[1]
            if not isinstance(complete, bool):
                raise TypeError("regime label completeness must be boolean")
            if (regime, complete) != item:
                raise ValueError("label regime identities must use canonical text")
            label_regimes.append(regime)
        if (
            tuple(sorted(self.included_labels_complete_by_regime))
            != self.included_labels_complete_by_regime
        ):
            raise ValueError("included label completeness rows must be sorted")
        if len(label_regimes) != len(set(label_regimes)):
            raise ValueError("label regime identities must be unique")
        if set(label_regimes) != set(regime_ids):
            raise ValueError("label completeness must cover every included regime exactly once")

        expected = _digest(
            {
                "candidate_hash": self.candidate_hash,
                "frozen_protocol_hash": self.frozen_protocol_hash,
                "input_snapshot_hash": self.input_snapshot_hash,
                "causal_cutoff": self.causal_cutoff,
                "permission_classes": self.permission_classes,
                "task": self.task,
                "instrument_family": self.instrument_family,
                "eligible_episode_ids": self.eligible_episode_ids,
                "included_episode_ids": self.included_episode_ids,
                "exclusions": self.exclusions,
                "episode_digests": self.episode_digests,
                "eligible_outcomes": self.eligible_outcomes,
                "included_outcomes": self.included_outcomes,
                "eligible_no_trade_count": self.eligible_no_trade_count,
                "included_no_trade_count": self.included_no_trade_count,
                "included_regime_counts": self.included_regime_counts,
                "included_labels_complete_by_regime": self.included_labels_complete_by_regime,
            }
        )
        if expected != self.digest:
            raise ValueError("population coverage digest does not match canonical content")

    @property
    def complete(self) -> bool:
        """True only when every eligible episode is actually scored.

        Explicit exclusions remain immutable audit evidence, but this module
        cannot independently prove that an arbitrary exclusion reason was
        authorized by the frozen protocol.  Therefore exclusions fail closed
        for terminal retention qualification until that authorization is
        verified by a separate protocol-evidence boundary.
        """

        accounted = set(self.included_episode_ids) | {
            episode_id for episode_id, _reason in self.exclusions
        }
        return (
            accounted == set(self.eligible_episode_ids)
            and not self.exclusions
            and set(self.included_episode_ids) == set(self.eligible_episode_ids)
        )


def build_population_coverage(
    population: CoveragePopulationSnapshot,
    *,
    candidate_hash: str,
    frozen_protocol_hash: str,
    input_snapshot_hash: str | None = None,
    causal_cutoff,
    permission_classes: Sequence[str],
    included_episode_ids: Sequence[str],
    exclusions: Mapping[str, str],
    task: str | None = None,
    instrument_family: str | None = None,
) -> PopulationCoverageManifest:
    """Build one deterministic manifest over the protocol-eligible population.

    Population must come from ExperienceMemory.coverage_population or an
    independently equivalent verified projection. Every eligible identity must
    be either included or explicitly excluded with a non-empty protocol reason.
    """

    if not isinstance(population, CoveragePopulationSnapshot):
        raise TypeError(
            "population must be a canonical CoveragePopulationSnapshot from ExperienceMemory"
        )
    # Recompute the population root from a detached canonical copy before any
    # qualification decision consumes the snapshot.  Frozen dataclass identity
    # alone is insufficient because nested JSON mappings can otherwise mutate.
    population_rows = population.verified_rows()
    candidate = _sha_identity(candidate_hash, name="candidate_hash")
    protocol = _sha_identity(frozen_protocol_hash, name="frozen_protocol_hash")
    snapshot = _sha_identity(population.root_hash, name="population root_hash")
    if input_snapshot_hash is not None:
        supplied_snapshot = _sha_identity(
            input_snapshot_hash,
            name="input_snapshot_hash",
        )
        if supplied_snapshot != snapshot:
            raise ValueError(
                "input_snapshot_hash must equal canonical ExperienceMemory population root"
            )
    cutoff = _time(causal_cutoff, name="causal_cutoff")

    if isinstance(permission_classes, (str, bytes)):
        raise TypeError("permission_classes must be a collection of text")
    permissions = tuple(
        sorted({_text(value, name="permission_class") for value in permission_classes})
    )
    if not permissions:
        raise ValueError("permission_classes must not be empty")
    normalized_task = None if task is None else _text(task, name="task")
    normalized_family = (
        None
        if instrument_family is None
        else _text(instrument_family, name="instrument_family")
    )
    if cutoff != population.causal_cutoff:
        raise ValueError("population snapshot causal_cutoff does not match frozen scope")
    if permissions != population.permission_classes:
        raise ValueError("population snapshot permissions do not match frozen scope")
    if normalized_task != population.task:
        raise ValueError("population snapshot task does not match frozen scope")
    if normalized_family != population.instrument_family:
        raise ValueError(
            "population snapshot instrument_family does not match frozen scope"
        )

    if population.eligible_count != len(population_rows):
        raise ValueError("population snapshot eligible_count does not match rows")

    if isinstance(included_episode_ids, (str, bytes)):
        raise TypeError("included_episode_ids must be a collection")
    included = tuple(
        sorted(_text(value, name="included episode_id") for value in included_episode_ids)
    )
    if len(included) != len(set(included)):
        raise ValueError("included_episode_ids must be unique")
    if not isinstance(exclusions, Mapping):
        raise TypeError("exclusions must be a mapping")
    normalized_exclusions = {
        _text(episode_id, name="excluded episode_id"): _text(
            reason,
            name="exclusion reason",
        )
        for episode_id, reason in exclusions.items()
    }

    records = {}
    record_rows: list[tuple[str, str, bool]] = []
    included_rows: list[tuple[str, str, bool]] = []
    regime_counts: dict[str, int] = {}
    regime_labels_complete: dict[str, bool] = {}
    episode_digests: list[tuple[str, str]] = []

    for raw in population_rows:
        if not isinstance(raw, Mapping):
            raise TypeError("population entries must be mappings")
        episode_id = _text(raw.get("episode_id"), name="episode_id")
        if episode_id in records:
            raise ValueError("population episode identities must be unique")
        episode_hash = _sha_identity(raw.get("episode_hash"), name="episode_hash")
        decision_time = _time(raw.get("decision_time"), name="decision_time")
        information_cutoff = _time(
            raw.get("information_cutoff"),
            name="information_cutoff",
        )
        created_at = _time(raw.get("created_at"), name="created_at")
        if any(value > cutoff for value in (decision_time, information_cutoff, created_at)):
            raise ValueError("population contains evidence unavailable at causal_cutoff")
        regime = _text(raw.get("regime"), name="regime")
        permission = _text(raw.get("permission_class"), name="permission_class")
        if permission not in permissions:
            raise ValueError("population contains permission class outside frozen scope")
        if normalized_task is not None and raw.get("task") != normalized_task:
            raise ValueError("population task does not match frozen scope")
        if normalized_family is not None and raw.get("instrument_family") != normalized_family:
            raise ValueError("population instrument family does not match frozen scope")

        payload = raw.get("effective_payload")
        if not isinstance(payload, Mapping):
            raise ValueError("effective_payload must be a mapping")
        outcome = payload.get("outcome")
        if not isinstance(outcome, Mapping):
            raise ValueError("effective outcome must be a mapping")
        outcome_class = _text(outcome.get("class"), name="outcome.class").upper()
        if outcome_class not in _OUTCOME_CLASSES:
            raise ValueError("outcome.class is not a canonical population class")
        label_mature = outcome.get("label_mature")
        if not isinstance(label_mature, bool):
            raise TypeError("outcome.label_mature must be boolean")
        reconciliation_state = _text(
            outcome.get("reconciliation_state"),
            name="outcome.reconciliation_state",
        )
        intended = payload.get("intended_action")
        if not isinstance(intended, Mapping):
            raise ValueError("intended_action must be a mapping")
        action_side = _text(intended.get("side"), name="intended_action.side").upper()
        no_trade = action_side == "NO_TRADE"
        if no_trade and outcome_class != "NULL":
            raise ValueError("NO_TRADE semantic subtype must use NULL outcome class")

        corrections = raw.get("correction_lineage")
        tombstones = raw.get("tombstone_lineage")
        if not isinstance(corrections, tuple) or not isinstance(tombstones, tuple):
            raise TypeError("population lineage must use immutable tuples")
        lineage = {
            "episode_id": episode_id,
            "episode_hash": episode_hash,
            "decision_time": decision_time,
            "information_cutoff": information_cutoff,
            "created_at": created_at,
            "regime": regime,
            "permission_class": permission,
            "effective_payload": payload,
            "correction_lineage": corrections,
            "tombstone_lineage": tombstones,
            "outcome_class": outcome_class,
            "label_mature": label_mature,
            "reconciliation_state": reconciliation_state,
            "no_trade": no_trade,
        }
        fact_digest = _digest(lineage)
        records[episode_id] = {
            "regime": regime,
            "outcome_class": outcome_class,
            "no_trade": no_trade,
            "label_mature": label_mature,
            "tombstoned": bool(tombstones),
            "digest": fact_digest,
        }
        episode_digests.append((episode_id, fact_digest))
        record_rows.append((outcome_class, fact_digest, no_trade))

    eligible = tuple(sorted(records))
    included_set = set(included)
    excluded_set = set(normalized_exclusions)
    if included_set & excluded_set:
        raise ValueError("episode cannot be both included and excluded")
    unknown = (included_set | excluded_set) - set(eligible)
    if unknown:
        raise ValueError("coverage references unknown episode identities")
    missing = set(eligible) - included_set - excluded_set
    if missing:
        raise ValueError(
            "eligible population is not fully accounted for: " + ", ".join(sorted(missing))
        )
    for episode_id in included:
        if records[episode_id]["tombstoned"]:
            raise ValueError("tombstoned episode requires explicit exclusion")

    for episode_id in included:
        record = records[episode_id]
        included_rows.append(
            (record["outcome_class"], record["digest"], record["no_trade"])
        )
        regime = record["regime"]
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
        regime_labels_complete[regime] = (
            regime_labels_complete.get(regime, True) and record["label_mature"]
        )

    eligible_no_trade = sum(1 for _klass, _digest_value, flag in record_rows if flag)
    included_no_trade = sum(1 for _klass, _digest_value, flag in included_rows if flag)
    exclusions_tuple = tuple(sorted(normalized_exclusions.items()))
    episode_digests_tuple = tuple(sorted(episode_digests))
    eligible_summary = _summary(record_rows)
    included_summary = _summary(included_rows)
    regime_counts_tuple = tuple(sorted(regime_counts.items()))
    label_tuple = tuple(sorted(regime_labels_complete.items()))

    payload = {
        "candidate_hash": candidate,
        "frozen_protocol_hash": protocol,
        "input_snapshot_hash": snapshot,
        "causal_cutoff": cutoff,
        "permission_classes": permissions,
        "task": normalized_task,
        "instrument_family": normalized_family,
        "eligible_episode_ids": eligible,
        "included_episode_ids": included,
        "exclusions": exclusions_tuple,
        "episode_digests": episode_digests_tuple,
        "eligible_outcomes": eligible_summary,
        "included_outcomes": included_summary,
        "eligible_no_trade_count": eligible_no_trade,
        "included_no_trade_count": included_no_trade,
        "included_regime_counts": regime_counts_tuple,
        "included_labels_complete_by_regime": label_tuple,
    }
    digest = _digest(payload)
    return PopulationCoverageManifest(
        candidate_hash=candidate,
        frozen_protocol_hash=protocol,
        input_snapshot_hash=snapshot,
        causal_cutoff=cutoff,
        permission_classes=permissions,
        task=normalized_task,
        instrument_family=normalized_family,
        eligible_episode_ids=eligible,
        included_episode_ids=included,
        exclusions=exclusions_tuple,
        episode_digests=episode_digests_tuple,
        eligible_outcomes=eligible_summary,
        included_outcomes=included_summary,
        eligible_no_trade_count=eligible_no_trade,
        included_no_trade_count=included_no_trade,
        included_regime_counts=regime_counts_tuple,
        included_labels_complete_by_regime=label_tuple,
        digest=digest,
    )
