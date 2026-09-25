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
        if not isinstance(self.permission_classes, tuple) or not self.permission_classes:
            raise ValueError("permission_classes must be a non-empty tuple")
        if len(self.eligible_episode_ids) != len(set(self.eligible_episode_ids)):
            raise ValueError("eligible episode identities must be unique")
        if len(self.included_episode_ids) != len(set(self.included_episode_ids)):
            raise ValueError("included episode identities must be unique")

    @property
    def complete(self) -> bool:
        accounted = set(self.included_episode_ids) | {
            episode_id for episode_id, _reason in self.exclusions
        }
        return accounted == set(self.eligible_episode_ids)


def build_population_coverage(
    population: Sequence[Mapping[str, object]],
    *,
    candidate_hash: str,
    frozen_protocol_hash: str,
    input_snapshot_hash: str,
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

    candidate = _sha_identity(candidate_hash, name="candidate_hash")
    protocol = _sha_identity(frozen_protocol_hash, name="frozen_protocol_hash")
    snapshot = _sha_identity(input_snapshot_hash, name="input_snapshot_hash")
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

    for raw in population:
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
