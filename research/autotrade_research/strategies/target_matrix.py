"""Causal target-conditioned candidates and a fail-closed research lifecycle.

The module produces deterministic BUY/SELL/HOLD plans for the canonical ZERO
simulator.  It owns no account, OMS, risk, fill, fee or promotion authority.
Every plan is derived from observations available at its decision cut and is
content-addressed before it crosses into the simulator.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..agents.goal_directed import WorkstreamSpec, evaluate_objective
from ..learning.waves import (
    LearningWavePolicy,
    MarketWaveSnapshot,
    evaluate_pause,
)
from .deterministic import (
    BreakoutThresholdBaseline,
    CausalObservation,
    MeanReversionThresholdBaseline,
    NoTradeBaseline,
    ReturnThresholdBaseline,
    StrategyDescriptor,
)


PLAN_SCHEMA = "autotrade-target-conditioned-strategy-plan-v1"
MATRIX_SCHEMA = "autotrade-target-conditioned-candidate-matrix-v1"
LIFECYCLE_SCHEMA = "autotrade-target-conditioned-lifecycle-v1"
PHASES = (
    "ACTIVE_SIMULATION",
    "AUTO_PAUSE_ON_WINDOW_OR_FAILURE",
    "ROOT_CAUSE_ANALYSIS",
    "TRAIN_CANDIDATES",
    "INDEPENDENT_EVALUATION",
    "RESUME_ONLY_IF_QUALIFIED",
    "HOLD_RESEARCH_ONLY",
)
_SHA_PREFIX = "sha256:"


def _canonical(value: object) -> bytes:
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _hash(value: object) -> str:
    return _SHA_PREFIX + sha256(_canonical(value)).hexdigest()


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or not value.startswith(_SHA_PREFIX):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    body = value[len(_SHA_PREFIX):]
    if len(body) != 64 or any(char not in "0123456789abcdef" for char in body):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must use exact decimal text")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite exact decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite exact decimal")
    return result


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(f"{name} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    candidate_id: str
    family: str
    lookback: int
    threshold: Decimal
    proposal_quantity: Decimal

    def __post_init__(self) -> None:
        if (type(self.candidate_id) is not str or not self.candidate_id
                or self.candidate_id != self.candidate_id.strip()):
            raise ValueError("candidate_id must be canonical text")
        allowed = {
            "NO_TRADE_CONTROL",
            "DETERMINISTIC_RETURN_THRESHOLD",
            "DETERMINISTIC_MEAN_REVERSION_THRESHOLD",
            "DETERMINISTIC_BREAKOUT_THRESHOLD",
        }
        if self.family not in allowed:
            raise ValueError("candidate family is unsupported")
        if type(self.lookback) is not int or self.lookback < 1:
            raise ValueError("candidate lookback must be positive")
        threshold = _decimal(self.threshold, name="threshold")
        quantity = _decimal(self.proposal_quantity, name="proposal_quantity")
        if threshold < 0 or quantity <= 0:
            raise ValueError("candidate parameters are outside bounded range")
        if self.family == "NO_TRADE_CONTROL" and (self.lookback != 1 or threshold != 0):
            raise ValueError("no-trade control parameters are fixed")
        if self.family != "NO_TRADE_CONTROL" and self.lookback < 2:
            raise ValueError("threshold candidate lookback must be at least two")
        object.__setattr__(self, "threshold", threshold)
        object.__setattr__(self, "proposal_quantity", quantity)

    def document(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "lookback": self.lookback,
            "threshold": format(self.threshold, "f"),
            "proposal_quantity": format(self.proposal_quantity, "f"),
        }

    @property
    def fingerprint(self) -> str:
        return _hash(self.document())


def default_candidates(*, proposal_quantity: object) -> tuple[CandidateSpec, ...]:
    quantity = _decimal(proposal_quantity, name="proposal_quantity")
    return (
        CandidateSpec("no-trade-control", "NO_TRADE_CONTROL", 1, Decimal(0), quantity),
        CandidateSpec("return-3-002", "DETERMINISTIC_RETURN_THRESHOLD", 3,
                      Decimal("0.002"), quantity),
        CandidateSpec("mean-reversion-3-002", "DETERMINISTIC_MEAN_REVERSION_THRESHOLD", 3,
                      Decimal("0.002"), quantity),
        CandidateSpec("breakout-4-002", "DETERMINISTIC_BREAKOUT_THRESHOLD", 4,
                      Decimal("0.002"), quantity),
    )


def _descriptor(spec: CandidateSpec, *, interval_seconds: int) -> StrategyDescriptor:
    source = {
        "module": "research.autotrade_research.strategies.deterministic",
        "candidate": spec.document(),
    }
    return StrategyDescriptor(
        strategy_id=spec.candidate_id,
        version=1,
        family=spec.family,
        feature_schema="CAUSAL_CLOSE_PRICE_V1",
        market_requirements=("CLOSED_BAR_PRICE",),
        minimum_history=spec.lookback,
        horizon_seconds=interval_seconds,
        decision_schedule="ON_COMPLETED_BAR",
        proposal_semantics="LONG_ONLY_TARGET_BUY_SELL_HOLD",
        parameter_bounds=(
            ("threshold", "0", "1"),
            ("proposal_quantity", "0.00000001", "1000000000"),
        ),
        resource_profile="ZERO_MODEL_CPU_BOUNDED",
        supported_regimes=("UNKNOWN", "TREND", "RANGE", "VOLATILE"),
        source_license="AUTOTRADE_FIRST_PARTY_PRIVATE_RESEARCH",
        evaluation_protocol_sha256=_hash({
            "execution": "CANONICAL_ZERO_NEXT_COMPLETED_BAR",
            "costs": "FEE_SPREAD_SLIPPAGE_PARTICIPATION",
            "causality": "OBSERVATIONS_AVAILABLE_AT_OR_BEFORE_DECISION",
        }),
        artifact_sha256=_hash(source),
    )


def _strategy(spec: CandidateSpec, *, interval_seconds: int):
    descriptor = _descriptor(spec, interval_seconds=interval_seconds)
    if spec.family == "NO_TRADE_CONTROL":
        return NoTradeBaseline(descriptor=descriptor), descriptor
    classes = {
        "DETERMINISTIC_RETURN_THRESHOLD": ReturnThresholdBaseline,
        "DETERMINISTIC_MEAN_REVERSION_THRESHOLD": MeanReversionThresholdBaseline,
        "DETERMINISTIC_BREAKOUT_THRESHOLD": BreakoutThresholdBaseline,
    }
    return classes[spec.family](
        lookback=spec.lookback,
        threshold=spec.threshold,
        proposal_quantity=spec.proposal_quantity,
        descriptor=descriptor,
    ), descriptor


def build_strategy_plan(
    spec: CandidateSpec,
    *,
    symbol: str,
    interval: str,
    interval_seconds: int,
    dataset_identity_sha256: str,
    signal_prices: list[str],
    available_at: list[datetime],
) -> dict[str, object]:
    """Freeze one candidate plan using only each causal signal prefix."""

    if type(spec) is not CandidateSpec:
        raise TypeError("spec must be exact CandidateSpec")
    if type(symbol) is not str or not symbol or type(interval) is not str or not interval:
        raise ValueError("symbol and interval must be canonical text")
    if type(interval_seconds) is not int or interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")
    dataset_digest = _digest(dataset_identity_sha256, name="dataset_identity_sha256")
    if (type(signal_prices) is not list or type(available_at) is not list
            or not signal_prices or len(signal_prices) != len(available_at)):
        raise ValueError("signal prices and availability cuts must be equal non-empty lists")
    cuts = [_utc(item, name="available_at") for item in available_at]
    if any(right <= left for left, right in zip(cuts, cuts[1:])):
        raise ValueError("strategy availability cuts must be strictly increasing")
    strategy, descriptor = _strategy(spec, interval_seconds=interval_seconds)
    sides: list[str] = []
    evidence: list[dict[str, object]] = []
    seen: list[str] = []
    for index, (price, cutoff) in enumerate(zip(signal_prices, cuts), 1):
        observation = CausalObservation.create(
            event_id=f"{dataset_digest}:closed-bar:{index}",
            symbol=symbol,
            available_at=cutoff,
            price=price,
        )
        seen.append(observation.event_id)
        if spec.family == "NO_TRADE_CONTROL":
            proposal = strategy.propose(
                symbol=symbol,
                decision_time=cutoff,
                evidence_event_ids=(observation.event_id,),
            )
        else:
            strategy.ingest(observation, simulation_time=cutoff)
            proposal = strategy.propose(symbol=symbol, decision_time=cutoff)
        if proposal.information_cutoff != cutoff or proposal.action not in {
            "BUY", "SELL", "HOLD"
        }:
            raise ValueError("deterministic strategy emitted a non-causal action")
        if any(event_id not in seen for event_id in proposal.evidence_event_ids):
            raise ValueError("strategy proposal references unseen evidence")
        sides.append(proposal.action)
        evidence.append({
            "episode": index,
            "decision_time": cutoff.isoformat(),
            "action": proposal.action,
            "evidence_event_ids": list(proposal.evidence_event_ids),
            "strategy_configuration_fingerprint": (
                proposal.strategy_configuration_fingerprint
            ),
        })
    body = {
        "schema": PLAN_SCHEMA,
        "candidate": spec.document(),
        "candidate_fingerprint": spec.fingerprint,
        "descriptor_fingerprint": descriptor.fingerprint,
        "symbol": symbol,
        "interval": interval,
        "dataset_identity_sha256": dataset_digest,
        "observation_count": len(signal_prices),
        "first_information_cut_utc": cuts[0].isoformat(),
        "last_information_cut_utc": cuts[-1].isoformat(),
        "side_plan": sides,
        "causal_evidence_sha256": _hash(evidence),
        "model_calls": 0,
        "network_orders": False,
        "trading_authority": False,
    }
    return {**body, "plan_sha256": _hash(body)}


def validate_strategy_plan(
    value: object,
    *,
    dataset_identity_sha256: str,
    symbol: str,
    interval: str,
    observation_count: int,
    signal_prices: list[str] | None = None,
    available_at: list[datetime] | None = None,
    interval_seconds: int | None = None,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("strategy plan must be an exact object")
    expected = {
        "schema", "candidate", "candidate_fingerprint", "descriptor_fingerprint",
        "symbol", "interval", "dataset_identity_sha256", "observation_count",
        "first_information_cut_utc", "last_information_cut_utc", "side_plan",
        "causal_evidence_sha256", "model_calls", "network_orders",
        "trading_authority", "plan_sha256",
    }
    if set(value) != expected or value.get("schema") != PLAN_SCHEMA:
        raise ValueError("strategy plan schema differs")
    body = {key: value[key] for key in value if key != "plan_sha256"}
    if value["plan_sha256"] != _hash(body):
        raise ValueError("strategy plan digest differs")
    if (
        value["dataset_identity_sha256"] != _digest(
            dataset_identity_sha256, name="dataset_identity_sha256"
        )
        or value["symbol"] != symbol
        or value["interval"] != interval
        or value["observation_count"] != observation_count
    ):
        raise ValueError("strategy plan is bound to another causal dataset cut")
    sides = value["side_plan"]
    if (type(sides) is not list or len(sides) != observation_count
            or any(type(side) is not str or side not in {"BUY", "SELL", "HOLD"}
                   for side in sides)):
        raise ValueError("strategy plan actions are malformed")
    if value["model_calls"] != 0 or value["network_orders"] is not False or value[
        "trading_authority"
    ] is not False:
        raise ValueError("strategy plan attempted to expand research authority")
    _digest(value["candidate_fingerprint"], name="candidate_fingerprint")
    _digest(value["descriptor_fingerprint"], name="descriptor_fingerprint")
    _digest(value["causal_evidence_sha256"], name="causal_evidence_sha256")
    _digest(value["plan_sha256"], name="plan_sha256")
    reconstruction_inputs = (signal_prices, available_at, interval_seconds)
    if any(item is not None for item in reconstruction_inputs):
        if any(item is None for item in reconstruction_inputs):
            raise ValueError(
                "strategy plan causal reconstruction requires prices, cuts and interval"
            )
        candidate = value["candidate"]
        expected_candidate_fields = {
            "candidate_id", "family", "lookback", "threshold",
            "proposal_quantity",
        }
        if type(candidate) is not dict or set(candidate) != expected_candidate_fields:
            raise ValueError("strategy plan candidate is malformed")
        try:
            spec = CandidateSpec(
                candidate_id=candidate["candidate_id"],
                family=candidate["family"],
                lookback=candidate["lookback"],
                threshold=candidate["threshold"],
                proposal_quantity=candidate["proposal_quantity"],
            )
            rebuilt = build_strategy_plan(
                spec,
                symbol=symbol,
                interval=interval,
                interval_seconds=interval_seconds,
                dataset_identity_sha256=dataset_identity_sha256,
                signal_prices=signal_prices,
                available_at=available_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "strategy plan candidate cannot be deterministically reconstructed"
            ) from exc
        if rebuilt != value:
            raise ValueError(
                "strategy plan differs from deterministic causal reconstruction"
            )
    return json.loads(_canonical(value))


def _candidate_allowed(spec: CandidateSpec, workstream: WorkstreamSpec) -> bool:
    if spec.family == "NO_TRADE_CONTROL":
        return True
    aliases = {
        "DETERMINISTIC_RETURN_THRESHOLD": {"MOMENTUM", "TREND"},
        "DETERMINISTIC_MEAN_REVERSION_THRESHOLD": {"MEAN_REVERSION"},
        "DETERMINISTIC_BREAKOUT_THRESHOLD": {"BREAKOUT", "VOLATILITY_BREAKOUT"},
    }
    return bool(aliases[spec.family].intersection(workstream.profile.strategy_families))


def select_training_candidate(
    *,
    candidates: Iterable[CandidateSpec],
    training_reports: dict[str, dict[str, object]],
    training_dataset_sha256: str,
    incumbent_candidate_id: str,
    max_drawdown_fraction: object,
) -> dict[str, object]:
    """Freeze selection from TRAIN only, before independent evidence is opened."""

    specs = tuple(candidates)
    if not specs or len({item.candidate_id for item in specs}) != len(specs):
        raise ValueError("training selection requires unique canonical candidates")
    by_id = {item.candidate_id: item for item in specs}
    if incumbent_candidate_id not in by_id:
        raise ValueError("training selection incumbent is absent")
    dataset = _digest(training_dataset_sha256, name="training_dataset_sha256")
    maximum_drawdown = _decimal(
        max_drawdown_fraction, name="max_drawdown_fraction"
    )
    if not Decimal("0") < maximum_drawdown <= Decimal("0.10"):
        raise ValueError("training selection drawdown cap must remain within 10%")
    ranking = []
    for spec in specs:
        report = training_reports.get(spec.candidate_id)
        if type(report) is not dict:
            raise ValueError("every candidate requires a training report")
        if report.get("dataset_identity_sha256") != dataset.removeprefix(_SHA_PREFIX):
            raise ValueError("training candidate report uses another dataset")
        simulation = report.get("simulation")
        comparative = report.get("comparative_report")
        if (type(simulation) is not dict or simulation.get("reconciled") is not True
                or simulation.get("status") not in {"COMPLETED", "PAUSED"}
                or type(comparative) is not dict):
            raise ValueError("training candidate lacks reconciled canonical economics")
        pnl = _decimal(comparative.get("agent_mark_pnl_usd"), name="agent_mark_pnl_usd")
        drawdown = _decimal(
            comparative.get("agent_max_mark_drawdown_fraction"),
            name="agent_max_mark_drawdown_fraction",
        )
        ranking.append({
            "candidate_id": spec.candidate_id,
            "candidate_fingerprint": spec.fingerprint,
            "net_pnl_after_costs_usd": format(pnl, "f"),
            "max_drawdown_fraction": format(drawdown, "f"),
            "risk_eligible": drawdown <= maximum_drawdown,
            "training_report_sha256": _hash(report),
        })
    incumbent = next(row for row in ranking
                     if row["candidate_id"] == incumbent_candidate_id)
    incumbent_pnl = Decimal(incumbent["net_pnl_after_costs_usd"])
    eligible = [
        row for row in ranking
        if row["candidate_id"] != incumbent_candidate_id
        and row["risk_eligible"]
        and Decimal(row["net_pnl_after_costs_usd"]) > incumbent_pnl
    ]
    selected = (
        sorted(
            eligible,
            key=lambda row: (
                -Decimal(row["net_pnl_after_costs_usd"]),
                Decimal(row["max_drawdown_fraction"]),
                row["candidate_id"],
            ),
        )[0]["candidate_id"]
        if eligible else incumbent_candidate_id
    )
    body = {
        "schema": "autotrade-target-training-selection-v1",
        "training_dataset_sha256": dataset,
        "selection_rule": (
            "MAX_AFTER_COST_PNL_THEN_MIN_DRAWDOWN_THEN_CANDIDATE_ID;"
            "MUST_BEAT_INCUMBENT_AND_PASS_TRAIN_DRAWDOWN"
        ),
        "incumbent_candidate_id": incumbent_candidate_id,
        "selected_candidate_id": selected,
        "ranking": sorted(ranking, key=lambda row: row["candidate_id"]),
        "independent_evaluation_opened": False,
        "automatic_champion_promotion": False,
        "network_orders": False,
    }
    return {**body, "selection_sha256": _hash(body)}


def evaluate_candidate_matrix(
    *,
    workstreams: Iterable[WorkstreamSpec],
    manifest_sha256: str,
    training_dataset_sha256: str,
    evaluation_dataset_sha256: str,
    candidates: Iterable[CandidateSpec],
    evaluation_reports: dict[str, dict[str, object]],
    evaluation_report_sha256: dict[str, str],
    evaluation_started_at: datetime,
    evaluation_ended_at: datetime,
    evaluation_window_seconds: int,
    minimum_evaluation_episodes: int,
    incumbent_candidate_id: str,
    selected_candidate_id: str,
) -> dict[str, object]:
    """Compare candidates after canonical costs; never change champion routing."""

    manifest_digest = _digest(manifest_sha256, name="manifest_sha256")
    training_digest = _digest(training_dataset_sha256, name="training_dataset_sha256")
    evaluation_digest = _digest(evaluation_dataset_sha256, name="evaluation_dataset_sha256")
    if training_digest == evaluation_digest:
        raise ValueError("candidate training and independent evaluation cuts must differ")
    measured_start = _utc(evaluation_started_at, name="evaluation_started_at")
    measured_end = _utc(evaluation_ended_at, name="evaluation_ended_at")
    measured_delta = measured_end - measured_start
    if measured_delta.total_seconds() <= 0 or measured_delta.microseconds:
        raise ValueError("evaluation measurement window must be positive whole seconds")
    measured_seconds = measured_delta.days * 86400 + measured_delta.seconds
    if type(evaluation_window_seconds) is not int or evaluation_window_seconds < 60:
        raise ValueError("evaluation_window_seconds must be bounded whole seconds")
    if type(minimum_evaluation_episodes) is not int or minimum_evaluation_episodes < 1:
        raise ValueError("minimum_evaluation_episodes must be positive")
    streams = tuple(workstreams)
    specs = tuple(candidates)
    if not streams or any(type(item) is not WorkstreamSpec for item in streams):
        raise ValueError("candidate matrix requires canonical workstreams")
    if not specs or any(type(item) is not CandidateSpec for item in specs):
        raise ValueError("candidate matrix requires canonical candidates")
    if len({item.candidate_id for item in specs}) != len(specs):
        raise ValueError("candidate ids must be unique")
    if incumbent_candidate_id not in {item.candidate_id for item in specs}:
        raise ValueError("incumbent candidate is absent")
    if selected_candidate_id not in {item.candidate_id for item in specs}:
        raise ValueError("TRAIN-selected candidate is absent")
    rows = []
    qualified: list[str] = []
    for spec in specs:
        report = evaluation_reports.get(spec.candidate_id)
        report_digest = evaluation_report_sha256.get(spec.candidate_id)
        if type(report) is not dict or report_digest is None:
            raise ValueError("every candidate requires an independent report")
        report_digest = _digest(report_digest, name="evaluation_report_sha256")
        if _hash(report) != report_digest:
            raise ValueError("candidate evaluation report digest differs")
        if report.get("dataset_identity_sha256") != evaluation_digest.removeprefix(_SHA_PREFIX):
            raise ValueError("candidate report uses another evaluation dataset")
        simulation = report.get("simulation")
        comparative = report.get("comparative_report")
        if type(simulation) is not dict or type(comparative) is not dict:
            raise ValueError("candidate report lacks canonical simulator economics")
        if simulation.get("reconciled") is not True:
            raise ValueError("candidate evaluation is not reconciled")
        try:
            report_start = datetime.fromisoformat(
                report["historical_start_utc"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            report_end = datetime.fromisoformat(
                comparative["asof_execution_open_utc"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (AttributeError, KeyError, ValueError) as exc:
            raise ValueError("candidate report lacks measured evaluation bounds") from exc
        if report_start != measured_start or report_end != measured_end:
            raise ValueError("candidate reports do not share the measured evaluation window")
        episodes = simulation.get("completed_episodes")
        if type(episodes) is not int or episodes < 1:
            raise ValueError("candidate evaluation episode count is invalid")
        initial = _decimal(comparative.get("starting_cash_usd"), name="starting_cash_usd")
        pnl = _decimal(comparative.get("agent_mark_pnl_usd"), name="agent_mark_pnl_usd")
        drawdown = _decimal(
            comparative.get("agent_max_mark_drawdown_fraction"),
            name="agent_max_mark_drawdown_fraction",
        )
        objective_rows = []
        candidate_qualified = False
        for workstream in streams:
            allowed = _candidate_allowed(spec, workstream)
            required_horizon = (
                workstream.profile.target_horizon_seconds
                if workstream.profile.target_horizon_seconds is not None
                else evaluation_window_seconds
            )
            profile_horizon_complete = measured_seconds == required_horizon
            receipt = evaluate_objective(
                workstream,
                initial_equity=initial,
                final_equity_after_costs=initial + pnl,
                max_drawdown_fraction=drawdown,
                horizon_complete=profile_horizon_complete,
            )
            profile_pass = receipt["status"] in {"TARGET_MET", "MEASURED_NO_TARGET"}
            if workstream.profile.target_return_fraction is None:
                profile_pass = profile_pass and pnl > 0
            gate_pass = (
                allowed
                and spec.candidate_id == selected_candidate_id
                and profile_pass
                and episodes >= minimum_evaluation_episodes
                and simulation.get("status") in {"COMPLETED", "PAUSED"}
                and profile_horizon_complete
                and drawdown <= workstream.max_session_loss_fraction
                and spec.family != "NO_TRADE_CONTROL"
            )
            candidate_qualified = candidate_qualified or gate_pass
            objective_rows.append({
                "session_id": workstream.session_id,
                "profile_id": workstream.profile.profile_id,
                "candidate_allowed": allowed,
                "required_horizon_seconds": required_horizon,
                "measured_horizon_seconds": measured_seconds,
                "horizon_exact_match": profile_horizon_complete,
                "qualification_passed": gate_pass,
                "objective": receipt,
            })
        if candidate_qualified:
            qualified.append(spec.candidate_id)
        rows.append({
            "candidate": spec.document(),
            "candidate_fingerprint": spec.fingerprint,
            "evaluation_report_sha256": report_digest,
            "completed_episodes": episodes,
            "net_pnl_after_costs_usd": format(pnl, "f"),
            "max_drawdown_fraction": format(drawdown, "f"),
            "objectives": objective_rows,
            "eligible_for_external_promotion_review": candidate_qualified,
            "selected_on_training": spec.candidate_id == selected_candidate_id,
        })
    body = {
        "schema": MATRIX_SCHEMA,
        "manifest_sha256": manifest_digest,
        "training_dataset_sha256": training_digest,
        "evaluation_dataset_sha256": evaluation_digest,
        "causal_split": "TRAINING_STRICTLY_PRECEDES_INDEPENDENT_EVALUATION",
        "evaluation_started_at": measured_start.isoformat(),
        "evaluation_ended_at": measured_end.isoformat(),
        "measured_horizon_seconds": measured_seconds,
        "registered_evaluation_window_seconds": evaluation_window_seconds,
        "minimum_evaluation_episodes": minimum_evaluation_episodes,
        "incumbent_candidate_id": incumbent_candidate_id,
        "selected_candidate_id": selected_candidate_id,
        "selection_authority": "FROZEN_TRAINING_CUT_ONLY",
        "candidates": rows,
        "qualified_candidate_ids": sorted(qualified),
        "automatic_champion_promotion": False,
        "promotion_authority_invoked": False,
        "network_orders": False,
        "trading_authority": False,
    }
    return {**body, "matrix_sha256": _hash(body)}


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    max_episodes: int
    max_market_seconds: int
    max_drawdown_fraction: Decimal

    def __post_init__(self) -> None:
        if type(self.max_episodes) is not int or not 1 <= self.max_episodes <= 10000:
            raise ValueError("max_episodes is outside bounded range")
        if (type(self.max_market_seconds) is not int
                or not 60 <= self.max_market_seconds <= 31_536_000):
            raise ValueError("max_market_seconds is outside bounded range")
        drawdown = _decimal(self.max_drawdown_fraction, name="max_drawdown_fraction")
        if not Decimal("0") < drawdown <= Decimal("0.10"):
            raise ValueError("lifecycle drawdown trigger must remain within 10%")
        object.__setattr__(self, "max_drawdown_fraction", drawdown)

    def document(self) -> dict[str, object]:
        return {
            "max_episodes": self.max_episodes,
            "max_market_seconds": self.max_market_seconds,
            "max_drawdown_fraction": format(self.max_drawdown_fraction, "f"),
        }

    def learning_wave_policy(self) -> LearningWavePolicy:
        """Project lifecycle bounds into the existing canonical wave policy."""
        return LearningWavePolicy(
            policy_id="target-conditioned-historical-v1",
            max_market_seconds=self.max_market_seconds,
            max_trades=self.max_episodes,
            max_drawdown=self.max_drawdown_fraction,
            promotion_mode="CONFIRMATION",
        )


def new_lifecycle(
    *, input_sha256: str, incumbent_candidate_id: str, policy: LifecyclePolicy,
    observed_at: datetime,
) -> dict[str, object]:
    if type(policy) is not LifecyclePolicy:
        raise TypeError("policy must be exact LifecyclePolicy")
    body = {
        "schema": LIFECYCLE_SCHEMA,
        "input_sha256": _digest(input_sha256, name="input_sha256"),
        "incumbent_candidate_id": incumbent_candidate_id,
        "policy": policy.document(),
        "phase": "ACTIVE_SIMULATION",
        "phase_sequence": 1,
        "phase_evidence_sha256": _hash({"phase": "ACTIVE_SIMULATION"}),
        "pause_reasons": [],
        "qualified_candidate_ids": [],
        "resume_candidate_id": incumbent_candidate_id,
        "automatic_champion_promotion": False,
        "network_orders": False,
        "trading_authority": False,
        "updated_at": _utc(observed_at, name="observed_at").isoformat(),
    }
    return {**body, "checkpoint_sha256": _hash(body)}


def transition_lifecycle(
    checkpoint: dict[str, object], *, phase: str, evidence_sha256: str,
    observed_at: datetime, pause_reasons: Iterable[str] = (),
    qualified_candidate_ids: Iterable[str] = (),
) -> dict[str, object]:
    if type(checkpoint) is not dict or checkpoint.get("schema") != LIFECYCLE_SCHEMA:
        raise ValueError("lifecycle checkpoint schema differs")
    current_body = {key: checkpoint[key] for key in checkpoint if key != "checkpoint_sha256"}
    if checkpoint.get("checkpoint_sha256") != _hash(current_body):
        raise ValueError("lifecycle checkpoint digest differs")
    current = checkpoint.get("phase")
    transitions = {
        "ACTIVE_SIMULATION": {"AUTO_PAUSE_ON_WINDOW_OR_FAILURE"},
        "AUTO_PAUSE_ON_WINDOW_OR_FAILURE": {"ROOT_CAUSE_ANALYSIS"},
        "ROOT_CAUSE_ANALYSIS": {"TRAIN_CANDIDATES"},
        "TRAIN_CANDIDATES": {"INDEPENDENT_EVALUATION"},
        "INDEPENDENT_EVALUATION": {
            "RESUME_ONLY_IF_QUALIFIED", "HOLD_RESEARCH_ONLY",
        },
    }
    if phase not in PHASES or phase not in transitions.get(current, set()):
        raise ValueError("lifecycle phase transition is not allowed")
    reasons = tuple(pause_reasons)
    qualified = tuple(qualified_candidate_ids)
    if any(type(item) is not str or not item for item in (*reasons, *qualified)):
        raise ValueError("lifecycle reasons and candidates must be canonical text")
    if len(set(reasons)) != len(reasons) or len(set(qualified)) != len(qualified):
        raise ValueError("lifecycle reasons and candidates must be unique")
    if phase == "AUTO_PAUSE_ON_WINDOW_OR_FAILURE" and not reasons:
        raise ValueError("automatic pause requires a bounded trigger or failure")
    if phase == "RESUME_ONLY_IF_QUALIFIED" and not qualified:
        raise ValueError("resume phase requires independently qualified candidates")
    if phase == "HOLD_RESEARCH_ONLY" and qualified:
        raise ValueError("research-only hold cannot claim qualified candidates")
    body = dict(current_body)
    body.update({
        "phase": phase,
        "phase_sequence": checkpoint["phase_sequence"] + 1,
        "phase_evidence_sha256": _digest(evidence_sha256, name="evidence_sha256"),
        "pause_reasons": list(reasons) if reasons else checkpoint["pause_reasons"],
        "qualified_candidate_ids": list(qualified),
        # Qualification permits a promotion review only.  Until the external
        # champion authority approves one exact candidate, the incumbent stays
        # routed and a candidate cannot silently resume financial simulation.
        "resume_candidate_id": checkpoint["incumbent_candidate_id"],
        "automatic_champion_promotion": False,
        "network_orders": False,
        "trading_authority": False,
        "updated_at": _utc(observed_at, name="observed_at").isoformat(),
    })
    return {**body, "checkpoint_sha256": _hash(body)}


def pause_reasons(
    *, completed_episodes: int, market_seconds: int, drawdown_fraction: object,
    policy: LifecyclePolicy, failed: bool,
) -> tuple[str, ...]:
    if type(policy) is not LifecyclePolicy or type(failed) is not bool:
        raise TypeError("pause trigger inputs are not canonical")
    if (type(completed_episodes) is not int or completed_episodes < 0
            or type(market_seconds) is not int or market_seconds < 0):
        raise ValueError("pause counters must be non-negative integers")
    drawdown = _decimal(drawdown_fraction, name="drawdown_fraction")
    if drawdown < 0:
        raise ValueError("drawdown_fraction must be non-negative")
    reasons = ["SIMULATION_FAILURE"] if failed else []
    started = datetime(2000, 1, 1, tzinfo=timezone.utc)
    source_cut = _hash({
        "completed_episodes": completed_episodes,
        "market_seconds": market_seconds,
        "drawdown_fraction": format(drawdown, "f"),
    })
    decision = evaluate_pause(
        policy.learning_wave_policy(),
        MarketWaveSnapshot(
            wave_id="target-conditioned-historical",
            segment_id="bounded-causal-cut",
            champion_artifact_hash=_hash({"incumbent": "external-authority"}),
            source_cut_hash=source_cut,
            segment_started_at=started,
            observed_at=started + timedelta(seconds=market_seconds),
            trades_since_pause=completed_episodes,
            evidence_events_since_pause=completed_episodes,
            drawdown_since_pause=drawdown,
            previous_regime_id="UNSPECIFIED",
            current_regime_id="UNSPECIFIED",
        ),
    )
    reasons.extend(decision.reasons)
    return tuple(reasons)


def write_checkpoint(path: Path, checkpoint: dict[str, object]) -> None:
    """Atomically persist a self-authenticating lifecycle checkpoint."""
    if type(path) is not type(Path()):
        raise TypeError("path must be exact Path")
    if path.exists() and path.is_symlink():
        raise ValueError("checkpoint path cannot be a symlink")
    body = {key: checkpoint[key] for key in checkpoint if key != "checkpoint_sha256"}
    if checkpoint.get("checkpoint_sha256") != _hash(body):
        raise ValueError("cannot persist an invalid lifecycle checkpoint")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(_canonical(checkpoint))
    os.replace(temporary, path)


def read_checkpoint(path: Path, *, input_sha256: str) -> dict[str, object]:
    if type(path) is not type(Path()) or path.is_symlink() or not path.is_file():
        raise ValueError("durable lifecycle checkpoint is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("durable lifecycle checkpoint is malformed") from exc
    if type(value) is not dict or value.get("schema") != LIFECYCLE_SCHEMA:
        raise ValueError("durable lifecycle checkpoint schema differs")
    body = {key: value[key] for key in value if key != "checkpoint_sha256"}
    if value.get("checkpoint_sha256") != _hash(body):
        raise ValueError("durable lifecycle checkpoint digest differs")
    if value.get("input_sha256") != _digest(input_sha256, name="input_sha256"):
        raise ValueError("durable lifecycle checkpoint belongs to another input cut")
    if value.get("phase") not in PHASES:
        raise ValueError("durable lifecycle checkpoint phase differs")
    return value

