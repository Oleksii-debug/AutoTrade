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
    source_cut_hash: str
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
            "source_cut_hash",
            _digest(self.source_cut_hash, name="source_cut_hash"),
        )
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
        source_cut_hash=snapshot.source_cut_hash,
        should_pause=bool(reasons),
        reasons=tuple(reasons),
        decision_hash=_hash_payload(payload),
    )


@dataclass(frozen=True, slots=True)
class EvidencePopulation:
    population_id: str
    root_hash: str
    causal_cut_hash: str
    available_at: datetime
    observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "population_id",
            _text(self.population_id, name="population_id"),
        )
        object.__setattr__(self, "root_hash", _digest(self.root_hash, name="root_hash"))
        object.__setattr__(
            self,
            "causal_cut_hash",
            _digest(self.causal_cut_hash, name="causal_cut_hash"),
        )
        object.__setattr__(
            self,
            "available_at",
            _time(self.available_at, name="available_at"),
        )
        if type(self.observation_ids) is not tuple:
            raise TypeError("observation_ids must be an immutable tuple")
        normalized = tuple(
            _text(value, name="observation_id") for value in self.observation_ids
        )
        if not normalized:
            raise ValueError("observation_ids must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("observation_ids must be unique")
        object.__setattr__(self, "observation_ids", normalized)


@dataclass(frozen=True, slots=True)
class CandidateWave:
    wave_id: str
    policy_hash: str
    champion_artifact_hash: str
    candidate_id: str
    candidate_artifact_hash: str
    candidate_created_at: datetime
    pause_decision_hash: str
    paused_source_cut_hash: str
    training_population: EvidencePopulation
    validation_population: EvidencePopulation
    validation_opened_at: datetime
    promotion_mode: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "wave_id", _text(self.wave_id, name="wave_id"))
        object.__setattr__(
            self,
            "policy_hash",
            _digest(self.policy_hash, name="policy_hash"),
        )
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
        paused_cut = _digest(
            self.paused_source_cut_hash,
            name="paused_source_cut_hash",
        )
        object.__setattr__(self, "paused_source_cut_hash", paused_cut)
        if not isinstance(self.training_population, EvidencePopulation):
            raise TypeError("training_population must be EvidencePopulation")
        if not isinstance(self.validation_population, EvidencePopulation):
            raise TypeError("validation_population must be EvidencePopulation")
        if self.training_population.causal_cut_hash != paused_cut:
            raise ValueError("training population must bind the paused causal cut")
        if self.training_population.available_at > created:
            raise ValueError("training population cannot become available after candidate creation")
        if self.training_population.root_hash == self.validation_population.root_hash:
            raise ValueError("training and validation populations must differ")
        if self.training_population.causal_cut_hash == self.validation_population.causal_cut_hash:
            raise ValueError("validation must use an independently identified causal cut")
        overlap = set(self.training_population.observation_ids).intersection(
            self.validation_population.observation_ids
        )
        if overlap:
            raise ValueError("validation observations must be disjoint from candidate training")
        mode = _text(self.promotion_mode, name="promotion_mode")
        if mode not in {"AUTO", "CONFIRMATION"}:
            raise ValueError("promotion_mode must be AUTO or CONFIRMATION")
        object.__setattr__(self, "promotion_mode", mode)

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
        training_population: EvidencePopulation,
        validation_population: EvidencePopulation,
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
        return cls(
            wave_id=pause.wave_id,
            policy_hash=pause.policy_hash,
            champion_artifact_hash=champion_artifact_hash,
            candidate_id=candidate_id,
            candidate_artifact_hash=candidate_artifact_hash,
            candidate_created_at=candidate_created_at,
            pause_decision_hash=pause.decision_hash,
            paused_source_cut_hash=pause.source_cut_hash,
            training_population=training_population,
            validation_population=validation_population,
            validation_opened_at=validation_opened_at,
            promotion_mode=policy.promotion_mode,
        )


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate_artifact_hash: str
    evaluation_status: str
    evaluation_hash: str
    science_gate_passed: bool
    retention_gate_passed: bool
    risk_gate_passed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_artifact_hash",
            _digest(self.candidate_artifact_hash, name="candidate_artifact_hash"),
        )
        status = _text(self.evaluation_status, name="evaluation_status")
        if status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ValueError("evaluation_status must be PASS, FAIL, or INCONCLUSIVE")
        object.__setattr__(self, "evaluation_status", status)
        object.__setattr__(
            self,
            "evaluation_hash",
            _digest(self.evaluation_hash, name="evaluation_hash"),
        )
        for field_name in (
            "science_gate_passed",
            "retention_gate_passed",
            "risk_gate_passed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be boolean")


@dataclass(frozen=True, slots=True)
class CandidateResolution:
    action: str
    reasons: tuple[str, ...]
    resolution_hash: str
    grants_trading_authority: bool = False

    def __post_init__(self) -> None:
        action = _text(self.action, name="action")
        if action not in {
            "ELIGIBLE_AUTO_PROMOTION",
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
    evaluation: CandidateEvaluation,
) -> CandidateResolution:
    """Resolve workflow state without changing champion routing.

    The existing ChampionRegistry remains the publication/promotion authority.
    This function only produces a deterministic handoff decision.
    """

    if not isinstance(wave, CandidateWave):
        raise TypeError("wave must be CandidateWave")
    if not isinstance(evaluation, CandidateEvaluation):
        raise TypeError("evaluation must be CandidateEvaluation")
    if evaluation.candidate_artifact_hash != wave.candidate_artifact_hash:
        raise ValueError("evaluation does not bind this candidate artifact")

    reasons: list[str] = []
    if evaluation.evaluation_status == "INCONCLUSIVE":
        action = "CONTINUE_VALIDATION"
        reasons.append("LEARNING_WAVE.EVALUATION_INCONCLUSIVE")
    elif evaluation.evaluation_status == "FAIL":
        action = "REJECTED"
        reasons.append("LEARNING_WAVE.EVALUATION_FAILED")
    else:
        if not evaluation.science_gate_passed:
            reasons.append("LEARNING_WAVE.SCIENCE_GATE_FAILED")
        if not evaluation.retention_gate_passed:
            reasons.append("LEARNING_WAVE.RETENTION_GATE_FAILED")
        if not evaluation.risk_gate_passed:
            reasons.append("LEARNING_WAVE.RISK_GATE_FAILED")
        if reasons:
            action = "REJECTED"
        elif wave.promotion_mode == "AUTO":
            action = "ELIGIBLE_AUTO_PROMOTION"
            reasons.append("LEARNING_WAVE.ALL_GATES_PASS_AUTO_POLICY")
        else:
            action = "AWAITING_CONFIRMATION"
            reasons.append("LEARNING_WAVE.ALL_GATES_PASS_CONFIRMATION_POLICY")

    payload = {
        "wave_id": wave.wave_id,
        "policy_hash": wave.policy_hash,
        "candidate_id": wave.candidate_id,
        "candidate_artifact_hash": wave.candidate_artifact_hash,
        "pause_decision_hash": wave.pause_decision_hash,
        "training_population_root": wave.training_population.root_hash,
        "validation_population_root": wave.validation_population.root_hash,
        "validation_opened_at": wave.validation_opened_at.isoformat(),
        "evaluation_hash": evaluation.evaluation_hash,
        "evaluation_status": evaluation.evaluation_status,
        "science_gate_passed": evaluation.science_gate_passed,
        "retention_gate_passed": evaluation.retention_gate_passed,
        "risk_gate_passed": evaluation.risk_gate_passed,
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
