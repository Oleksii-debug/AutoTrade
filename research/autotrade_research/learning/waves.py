"""Deterministic coordinator for product-spec learning waves (Section 15).

This module coordinates *when* a market-learning wave should pause and whether
an externally evaluated candidate is ready to be handed to the existing
promotion authority.  It deliberately does not train models, publish champion
state, mutate risk, or grant financial authority.

The core invariant is that candidate construction is bound to the paused causal
cut while validation uses an independently identified, non-overlapping
population that is not opened to the candidate process before the candidate is
fixed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ..artifacts.store import ArtifactStore
from .champion import CandidateApproval
from .population_coverage import PopulationCoverageManifest


_SHA256_PREFIX = "sha256:"


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be canonical text")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if not text.startswith(_SHA256_PREFIX):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    payload = text[len(_SHA256_PREFIX) :]
    if len(payload) != 64 or any(char not in "0123456789abcdef" for char in payload):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return text


def _time(value: object, *, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _population_cutoff(value: object, *, name: str) -> datetime:
    """Read the canonical ISO cutoff stored by PopulationCoverageManifest."""

    if type(value) is not str:
        raise TypeError(f"{name} must be a canonical ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical ISO timestamp") from error
    admitted = _time(parsed, name=name)
    if admitted.isoformat() != value:
        raise ValueError(f"{name} must be canonical UTC ISO form")
    return admitted


def _positive_int_or_none(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer or None")
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use exact decimal input")
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive_decimal_or_none(value: object, *, name: str) -> Decimal | None:
    if value is None:
        return None
    result = _decimal(value, name=name)
    if result <= 0:
        raise ValueError(f"{name} must be positive or None")
    return result


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash_payload(value: Any) -> str:
    return _SHA256_PREFIX + sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class LearningWavePolicy:
    """Pre-registered pause and promotion policy.

    All pause thresholds are optional and product-specific.  At least one must
    be enabled; the product therefore has no hidden universal trade/day count.
    """

    policy_id: str
    max_market_seconds: int | None = None
    max_trades: int | None = None
    min_evidence_events: int | None = None
    max_drawdown: Decimal | None = None
    pause_on_regime_change: bool = False
    promotion_mode: str = "CONFIRMATION"

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(
            self,
            "max_market_seconds",
            _positive_int_or_none(self.max_market_seconds, name="max_market_seconds"),
        )
        object.__setattr__(
            self,
            "max_trades",
            _positive_int_or_none(self.max_trades, name="max_trades"),
        )
        object.__setattr__(
            self,
            "min_evidence_events",
            _positive_int_or_none(self.min_evidence_events, name="min_evidence_events"),
        )
        object.__setattr__(
            self,
            "max_drawdown",
            _positive_decimal_or_none(self.max_drawdown, name="max_drawdown"),
        )
        if type(self.pause_on_regime_change) is not bool:
            raise TypeError("pause_on_regime_change must be boolean")
        mode = _text(self.promotion_mode, name="promotion_mode")
        if mode not in {"AUTO", "CONFIRMATION"}:
            raise ValueError("promotion_mode must be AUTO or CONFIRMATION")
        object.__setattr__(self, "promotion_mode", mode)
        if not any(
            (
                self.max_market_seconds is not None,
                self.max_trades is not None,
                self.min_evidence_events is not None,
                self.max_drawdown is not None,
                self.pause_on_regime_change,
            )
        ):
            raise ValueError("at least one learning-wave pause trigger is required")

    @property
    def policy_hash(self) -> str:
        return _hash_payload(
            {
                "policy_id": self.policy_id,
                "max_market_seconds": self.max_market_seconds,
                "max_trades": self.max_trades,
                "min_evidence_events": self.min_evidence_events,
                "max_drawdown": (
                    None if self.max_drawdown is None else str(self.max_drawdown)
                ),
                "pause_on_regime_change": self.pause_on_regime_change,
                "promotion_mode": self.promotion_mode,
            }
        )


@dataclass(frozen=True, slots=True)
class MarketWaveSnapshot:
    wave_id: str
    segment_id: str
    champion_artifact_hash: str
    source_cut_hash: str
    segment_started_at: datetime
    observed_at: datetime
    trades_since_pause: int
    evidence_events_since_pause: int
    drawdown_since_pause: Decimal
    previous_regime_id: str
    current_regime_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "wave_id", _text(self.wave_id, name="wave_id"))
        object.__setattr__(self, "segment_id", _text(self.segment_id, name="segment_id"))
        object.__setattr__(
            self,
            "champion_artifact_hash",
            _digest(self.champion_artifact_hash, name="champion_artifact_hash"),
        )
        object.__setattr__(
            self,
            "champion_artifact_hash",
            _digest(self.champion_artifact_hash, name="champion_artifact_hash"),
        )
        object.__setattr__(
            self,
            "source_cut_hash",
            _digest(self.source_cut_hash, name="source_cut_hash"),
        )
        started = _time(self.segment_started_at, name="segment_started_at")
        observed = _time(self.observed_at, name="observed_at")
        if observed < started:
            raise ValueError("observed_at cannot precede segment_started_at")
        object.__setattr__(self, "segment_started_at", started)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(
            self,
            "trades_since_pause",
            _non_negative_int(self.trades_since_pause, name="trades_since_pause"),
        )
        object.__setattr__(
            self,
            "evidence_events_since_pause",
            _non_negative_int(
                self.evidence_events_since_pause,
                name="evidence_events_since_pause",
            ),
        )
        drawdown = _decimal(self.drawdown_since_pause, name="drawdown_since_pause")
        if drawdown < 0:
            raise ValueError("drawdown_since_pause must be non-negative")
        object.__setattr__(self, "drawdown_since_pause", drawdown)
        object.__setattr__(
            self,
            "previous_regime_id",
            _text(self.previous_regime_id, name="previous_regime_id"),
        )
        object.__setattr__(
            self,
            "current_regime_id",
            _text(self.current_regime_id, name="current_regime_id"),
        )


@dataclass(frozen=True, slots=True)
class PauseDecision:
    wave_id: str
    policy_hash: str
    champion_artifact_hash: str
    source_cut_hash: str
    paused_at: datetime
    should_pause: bool
    reasons: tuple[str, ...]
    decision_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "wave_id", _text(self.wave_id, name="wave_id"))
        object.__setattr__(
            self,
            "policy_hash",
            _digest(self.policy_hash, name="policy_hash"),
        )
        object.__setattr__(
            self,
            "champion_artifact_hash",
            _digest(self.champion_artifact_hash, name="champion_artifact_hash"),
        )
        object.__setattr__(
            self,
            "source_cut_hash",
            _digest(self.source_cut_hash, name="source_cut_hash"),
        )
        object.__setattr__(self, "paused_at", _time(self.paused_at, name="paused_at"))
        if type(self.should_pause) is not bool:
            raise TypeError("should_pause must be boolean")
        if type(self.reasons) is not tuple or any(type(item) is not str for item in self.reasons):
            raise TypeError("reasons must be a tuple of strings")
        if self.should_pause and not self.reasons:
            raise ValueError("paused decision requires at least one reason")
        if not self.should_pause and self.reasons:
            raise ValueError("continue decision cannot carry pause reasons")
        object.__setattr__(
            self,
            "decision_hash",
            _digest(self.decision_hash, name="decision_hash"),
        )


def evaluate_pause(
    policy: LearningWavePolicy,
    snapshot: MarketWaveSnapshot,
) -> PauseDecision:
    if not isinstance(policy, LearningWavePolicy):
        raise TypeError("policy must be LearningWavePolicy")
    if not isinstance(snapshot, MarketWaveSnapshot):
        raise TypeError("snapshot must be MarketWaveSnapshot")

    reasons: list[str] = []
    elapsed = int((snapshot.observed_at - snapshot.segment_started_at).total_seconds())

    if policy.max_market_seconds is not None and elapsed >= policy.max_market_seconds:
        reasons.append("LEARNING_WAVE.TIME_THRESHOLD")
    if policy.max_trades is not None and snapshot.trades_since_pause >= policy.max_trades:
        reasons.append("LEARNING_WAVE.TRADE_THRESHOLD")
    if (
        policy.min_evidence_events is not None
        and snapshot.evidence_events_since_pause >= policy.min_evidence_events
    ):
        reasons.append("LEARNING_WAVE.EVIDENCE_THRESHOLD")
    if (
        policy.pause_on_regime_change
        and snapshot.current_regime_id != snapshot.previous_regime_id
    ):
        reasons.append("LEARNING_WAVE.REGIME_CHANGE")
    if policy.max_drawdown is not None and snapshot.drawdown_since_pause >= policy.max_drawdown:
        reasons.append("LEARNING_WAVE.DRAWDOWN_THRESHOLD")

    payload = {
        "policy_id": policy.policy_id,
        "policy_hash": policy.policy_hash,
        "wave_id": snapshot.wave_id,
        "segment_id": snapshot.segment_id,
        "champion_artifact_hash": snapshot.champion_artifact_hash,
        "source_cut_hash": snapshot.source_cut_hash,
        "segment_started_at": snapshot.segment_started_at.isoformat(),
        "observed_at": snapshot.observed_at.isoformat(),
        "trades_since_pause": snapshot.trades_since_pause,
        "evidence_events_since_pause": snapshot.evidence_events_since_pause,
        "drawdown_since_pause": str(snapshot.drawdown_since_pause),
        "previous_regime_id": snapshot.previous_regime_id,
        "current_regime_id": snapshot.current_regime_id,
        "reasons": reasons,
    }
    return PauseDecision(
        wave_id=snapshot.wave_id,
        policy_hash=policy.policy_hash,
        champion_artifact_hash=snapshot.champion_artifact_hash,
        source_cut_hash=snapshot.source_cut_hash,
        paused_at=snapshot.observed_at,
        should_pause=bool(reasons),
        reasons=tuple(reasons),
        decision_hash=_hash_payload(payload),
    )


@dataclass(frozen=True, slots=True)
class CandidateWave:
    wave_id: str
    policy: LearningWavePolicy
    policy_hash: str
    champion_artifact_hash: str
    candidate_id: str
    candidate_artifact_hash: str
    candidate_created_at: datetime
    pause_decision_hash: str
    pause_reasons: tuple[str, ...]
    paused_source_cut_hash: str
    paused_at: datetime
    error_analysis_hash: str
    error_analysis_at: datetime
    change_summary: str
    training_population: PopulationCoverageManifest
    validation_population: PopulationCoverageManifest
    validation_opened_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "wave_id", _text(self.wave_id, name="wave_id"))
        if not isinstance(self.policy, LearningWavePolicy):
            raise TypeError("policy must be LearningWavePolicy")
        policy_hash = _digest(self.policy_hash, name="policy_hash")
        if self.policy.policy_hash != policy_hash:
            raise ValueError("candidate policy does not match bound policy hash")
        object.__setattr__(self, "policy_hash", policy_hash)
        champion = _digest(
            self.champion_artifact_hash,
            name="champion_artifact_hash",
        )
        candidate = _digest(
            self.candidate_artifact_hash,
            name="candidate_artifact_hash",
        )
        if champion == candidate:
            raise ValueError("candidate artifact must differ from current champion")
        object.__setattr__(self, "champion_artifact_hash", champion)
        object.__setattr__(self, "candidate_artifact_hash", candidate)
        object.__setattr__(
            self,
            "candidate_id",
            _text(self.candidate_id, name="candidate_id"),
        )
        created = _time(self.candidate_created_at, name="candidate_created_at")
        opened = _time(self.validation_opened_at, name="validation_opened_at")
        if opened < created:
            raise ValueError("validation cannot be opened before candidate creation")
        object.__setattr__(self, "candidate_created_at", created)
        object.__setattr__(self, "validation_opened_at", opened)
        object.__setattr__(
            self,
            "pause_decision_hash",
            _digest(self.pause_decision_hash, name="pause_decision_hash"),
        )
        if (
            type(self.pause_reasons) is not tuple
            or not self.pause_reasons
            or any(type(reason) is not str or not reason for reason in self.pause_reasons)
        ):
            raise ValueError("pause_reasons must be a non-empty immutable tuple")
        paused_cut = _digest(
            self.paused_source_cut_hash,
            name="paused_source_cut_hash",
        )
        object.__setattr__(self, "paused_source_cut_hash", paused_cut)
        paused_at = _time(self.paused_at, name="paused_at")
        if created < paused_at:
            raise ValueError("candidate cannot be created before the learning-wave pause")
        object.__setattr__(self, "paused_at", paused_at)
        object.__setattr__(
            self,
            "error_analysis_hash",
            _digest(self.error_analysis_hash, name="error_analysis_hash"),
        )
        analysis_at = _time(self.error_analysis_at, name="error_analysis_at")
        if analysis_at < paused_at:
            raise ValueError("error analysis cannot precede the learning-wave pause")
        if analysis_at > created:
            raise ValueError("candidate cannot be created before error analysis completes")
        object.__setattr__(self, "error_analysis_at", analysis_at)
        object.__setattr__(
            self,
            "change_summary",
            _text(self.change_summary, name="change_summary"),
        )
        if not isinstance(self.training_population, PopulationCoverageManifest):
            raise TypeError(
                "training_population must be canonical PopulationCoverageManifest"
            )
        if not isinstance(self.validation_population, PopulationCoverageManifest):
            raise TypeError(
                "validation_population must be canonical PopulationCoverageManifest"
            )
        if not self.training_population.complete:
            raise ValueError("training population coverage must be complete")
        if not self.validation_population.complete:
            raise ValueError("validation population coverage must be complete")
        if self.training_population.candidate_hash != candidate:
            raise ValueError("training population must bind the exact candidate artifact")
        if self.validation_population.candidate_hash != candidate:
            raise ValueError("validation population must bind the exact candidate artifact")
        if (
            self.training_population.frozen_protocol_hash
            != self.validation_population.frozen_protocol_hash
        ):
            raise ValueError("training and validation must bind one frozen protocol")
        if self.training_population.input_snapshot_hash != paused_cut:
            raise ValueError(
                "training population must bind the paused canonical population root"
            )
        training_cutoff = _population_cutoff(
            self.training_population.causal_cutoff,
            name="training_population.causal_cutoff",
        )
        validation_cutoff = _population_cutoff(
            self.validation_population.causal_cutoff,
            name="validation_population.causal_cutoff",
        )
        if training_cutoff > created:
            raise ValueError(
                "training population cannot include evidence after candidate creation"
            )
        if validation_cutoff > opened:
            raise ValueError(
                "validation population cannot be opened before its causal cutoff"
            )
        if self.training_population.digest == self.validation_population.digest:
            raise ValueError("training and validation population manifests must differ")
        if (
            self.training_population.input_snapshot_hash
            == self.validation_population.input_snapshot_hash
        ):
            raise ValueError(
                "validation must use an independently identified population snapshot"
            )
        overlap = set(self.training_population.included_episode_ids).intersection(
            self.validation_population.included_episode_ids
        )
        if overlap:
            raise ValueError(
                "validation episodes must be disjoint from candidate training"
            )

    @property
    def promotion_mode(self) -> str:
        return self.policy.promotion_mode

    @classmethod
    def from_pause(
        cls,
        *,
        pause: PauseDecision,
        policy: LearningWavePolicy,
        champion_artifact_hash: str,
        candidate_id: str,
        candidate_artifact_hash: str,
        candidate_created_at: datetime,
        error_analysis_hash: str,
        error_analysis_at: datetime,
        change_summary: str,
        training_population: PopulationCoverageManifest,
        validation_population: PopulationCoverageManifest,
        validation_opened_at: datetime,
    ) -> "CandidateWave":
        if not isinstance(pause, PauseDecision):
            raise TypeError("pause must be PauseDecision")
        if not pause.should_pause:
            raise ValueError("candidate wave requires a real pause decision")
        if not isinstance(policy, LearningWavePolicy):
            raise TypeError("policy must be LearningWavePolicy")
        if policy.policy_hash != pause.policy_hash:
            raise ValueError("policy does not match the exact pause decision")
        champion = _digest(champion_artifact_hash, name="champion_artifact_hash")
        if champion != pause.champion_artifact_hash:
            raise ValueError("champion does not match the exact pause decision")
        return cls(
            wave_id=pause.wave_id,
            policy=policy,
            policy_hash=pause.policy_hash,
            champion_artifact_hash=champion,
            candidate_id=candidate_id,
            candidate_artifact_hash=candidate_artifact_hash,
            candidate_created_at=candidate_created_at,
            pause_decision_hash=pause.decision_hash,
            pause_reasons=pause.reasons,
            paused_source_cut_hash=pause.source_cut_hash,
            paused_at=pause.paused_at,
            error_analysis_hash=error_analysis_hash,
            error_analysis_at=error_analysis_at,
            change_summary=change_summary,
            training_population=training_population,
            validation_population=validation_population,
            validation_opened_at=validation_opened_at,
        )


@dataclass(frozen=True, slots=True)
class CandidateResolution:
    action: str
    reasons: tuple[str, ...]
    resolution_hash: str
    grants_trading_authority: bool = False

    def __post_init__(self) -> None:
        action = _text(self.action, name="action")
        if action not in {
            "HANDOFF_TO_AUTO_PROMOTION_CHECK",
            "AWAITING_CONFIRMATION",
            "REJECTED",
            "CONTINUE_VALIDATION",
        }:
            raise ValueError("unsupported candidate resolution action")
        object.__setattr__(self, "action", action)
        if type(self.reasons) is not tuple or any(type(item) is not str for item in self.reasons):
            raise TypeError("reasons must be a tuple of strings")
        if not self.reasons:
            raise ValueError("candidate resolution requires at least one reason")
        object.__setattr__(
            self,
            "resolution_hash",
            _digest(self.resolution_hash, name="resolution_hash"),
        )
        if self.grants_trading_authority is not False:
            raise ValueError("learning-wave resolution cannot grant trading authority")


def resolve_candidate(
    wave: CandidateWave,
    approval: CandidateApproval,
    *,
    resolved_at: datetime,
) -> CandidateResolution:
    """Resolve wave workflow without changing champion routing.

    CandidateApproval is the existing canonical scientific/promotion handoff.
    This coordinator performs only deterministic workflow checks.  The existing
    ChampionRegistry remains the sole publication/promotion authority and
    re-verifies the approval against ScientificRegistry before routing changes.
    """

    if not isinstance(wave, CandidateWave):
        raise TypeError("wave must be CandidateWave")
    if not isinstance(approval, CandidateApproval):
        raise TypeError("approval must be CandidateApproval")
    current = _time(resolved_at, name="resolved_at")
    if wave.policy.policy_hash != wave.policy_hash:
        raise ValueError("bound learning-wave policy changed after candidate creation")
    if approval.candidate_id != wave.candidate_id:
        raise ValueError("approval does not bind this candidate identity")
    if approval.artifact_hash != wave.candidate_artifact_hash:
        raise ValueError("approval does not bind this candidate artifact")
    if (
        approval.protocol_hash
        != wave.training_population.frozen_protocol_hash
        or approval.protocol_hash
        != wave.validation_population.frozen_protocol_hash
    ):
        raise ValueError("approval does not bind the learning-wave frozen protocol")

    reasons: list[str] = []
    expired = current >= approval.evidence_valid_until
    if expired:
        reasons.append("LEARNING_WAVE.APPROVAL_EVIDENCE_EXPIRED")
    if approval.evaluation_status == "FAIL":
        reasons.append("LEARNING_WAVE.EVALUATION_FAILED")
    if not approval.retention_passed:
        reasons.append("LEARNING_WAVE.RETENTION_GATE_FAILED")
    if not approval.risk_passed:
        reasons.append("LEARNING_WAVE.RISK_GATE_FAILED")

    if reasons:
        action = "REJECTED"
    elif approval.evaluation_status == "INCONCLUSIVE":
        action = "CONTINUE_VALIDATION"
        reasons.append("LEARNING_WAVE.EVALUATION_INCONCLUSIVE")
    elif wave.promotion_mode == "AUTO":
        action = "HANDOFF_TO_AUTO_PROMOTION_CHECK"
        reasons.append("LEARNING_WAVE.APPROVAL_OBJECT_READY_FOR_AUTO_PROMOTION_CHECK")
    else:
        action = "AWAITING_CONFIRMATION"
        reasons.append("LEARNING_WAVE.APPROVAL_OBJECT_AWAITING_CONFIRMATION")

    payload = {
        "wave_id": wave.wave_id,
        "policy_hash": wave.policy_hash,
        "candidate_id": wave.candidate_id,
        "candidate_artifact_hash": wave.candidate_artifact_hash,
        "pause_decision_hash": wave.pause_decision_hash,
        "pause_reasons": list(wave.pause_reasons),
        "paused_at": wave.paused_at.isoformat(),
        "error_analysis_hash": wave.error_analysis_hash,
        "error_analysis_at": wave.error_analysis_at.isoformat(),
        "change_summary": wave.change_summary,
        "training_population_digest": wave.training_population.digest,
        "validation_population_digest": wave.validation_population.digest,
        "validation_opened_at": wave.validation_opened_at.isoformat(),
        "resolved_at": current.isoformat(),
        "approval_evidence_id": approval.evidence_id,
        "approval_valid_until": approval.evidence_valid_until.isoformat(),
        "approval_status": approval.evaluation_status,
        "approval_retention_passed": approval.retention_passed,
        "approval_risk_passed": approval.risk_passed,
        "approval_authority_scope_id": approval.authority_scope_id,
        "approval_protocol_id": approval.protocol_id,
        "approval_protocol_hash": approval.protocol_hash,
        "approval_evaluation_id": approval.evaluation_id,
        "approval_evaluation_result_hash": approval.evaluation_result_hash,
        "promotion_mode": wave.promotion_mode,
        "action": action,
        "reasons": reasons,
        "grants_trading_authority": False,
    }
    return CandidateResolution(
        action=action,
        reasons=tuple(reasons),
        resolution_hash=_hash_payload(payload),
        grants_trading_authority=False,
    )


def publish_wave_resolution(
    artifact_store: ArtifactStore,
    wave: CandidateWave,
    approval: CandidateApproval,
    *,
    resolved_at: datetime,
    rights: dict[str, Any],
) -> tuple[CandidateResolution, dict[str, Any]]:
    """Persist one immutable Section-15 wave outcome in the canonical ArtifactStore.

    Publication is evidence retention only.  It does not call ChampionRegistry,
    mutate routing, change risk, or grant trading authority.
    """

    if not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact_store must be ArtifactStore")
    resolution = resolve_candidate(
        wave,
        approval,
        resolved_at=resolved_at,
    )
    record = {
        "schema_version": "1.0.0",
        "artifact_kind": "LEARNING_WAVE_RESOLUTION",
        "wave_id": wave.wave_id,
        "policy": {
            "policy_id": wave.policy.policy_id,
            "policy_hash": wave.policy_hash,
            "max_market_seconds": wave.policy.max_market_seconds,
            "max_trades": wave.policy.max_trades,
            "min_evidence_events": wave.policy.min_evidence_events,
            "max_drawdown": (
                None
                if wave.policy.max_drawdown is None
                else str(wave.policy.max_drawdown)
            ),
            "pause_on_regime_change": wave.policy.pause_on_regime_change,
            "promotion_mode": wave.policy.promotion_mode,
        },
        "champion_artifact_hash": wave.champion_artifact_hash,
        "candidate_id": wave.candidate_id,
        "candidate_artifact_hash": wave.candidate_artifact_hash,
        "pause_decision_hash": wave.pause_decision_hash,
        "pause_reasons": list(wave.pause_reasons),
        "paused_source_cut_hash": wave.paused_source_cut_hash,
        "paused_at": wave.paused_at.isoformat(),
        "error_analysis_hash": wave.error_analysis_hash,
        "error_analysis_at": wave.error_analysis_at.isoformat(),
        "change_summary": wave.change_summary,
        "training_population": {
            "manifest_digest": wave.training_population.digest,
            "input_snapshot_hash": wave.training_population.input_snapshot_hash,
            "frozen_protocol_hash": wave.training_population.frozen_protocol_hash,
            "causal_cutoff": wave.training_population.causal_cutoff,
            "observation_count": len(wave.training_population.included_episode_ids),
        },
        "validation_population": {
            "manifest_digest": wave.validation_population.digest,
            "input_snapshot_hash": wave.validation_population.input_snapshot_hash,
            "frozen_protocol_hash": wave.validation_population.frozen_protocol_hash,
            "causal_cutoff": wave.validation_population.causal_cutoff,
            "opened_at": wave.validation_opened_at.isoformat(),
            "observation_count": len(wave.validation_population.included_episode_ids),
        },
        "approval": {
            "evidence_id": approval.evidence_id,
            "evidence_valid_until": approval.evidence_valid_until.isoformat(),
            "evaluation_status": approval.evaluation_status,
            "retention_passed": approval.retention_passed,
            "risk_passed": approval.risk_passed,
            "authority_scope_id": approval.authority_scope_id,
            "protocol_id": approval.protocol_id,
            "protocol_hash": approval.protocol_hash,
            "evaluation_id": approval.evaluation_id,
            "evaluation_result_hash": approval.evaluation_result_hash,
        },
        "resolved_at": _time(resolved_at, name="resolved_at").isoformat(),
        "resolution": {
            "action": resolution.action,
            "reasons": list(resolution.reasons),
            "resolution_hash": resolution.resolution_hash,
            "grants_trading_authority": False,
        },
    }
    data = _canonical_bytes(record)
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "autotrade:learning-wave-resolution:" + resolution.resolution_hash,
        )
    )
    manifest = artifact_store.publish_bytes(
        artifact_id=artifact_id,
        data=data,
        media_type="application/json",
        rights=rights,
        source_refs=[
            wave.champion_artifact_hash,
            wave.candidate_artifact_hash,
            wave.training_population.digest,
            wave.validation_population.digest,
            wave.training_population.input_snapshot_hash,
            wave.validation_population.input_snapshot_hash,
            approval.protocol_hash,
            approval.evaluation_result_hash,
        ],
        metadata={
            "artifact_kind": "LEARNING_WAVE_RESOLUTION",
            "wave_id": wave.wave_id,
            "candidate_id": wave.candidate_id,
            "resolution_hash": resolution.resolution_hash,
            "grants_trading_authority": False,
        },
    )
    return resolution, manifest
