"""Causal history-only learning adapter over canonical CLOSED ZERO outcomes.

The adapter deliberately does not infer labels from a later market candle.  It
accepts only completed, reconciled results emitted by the existing simulation
authority, turns each flat-to-position-to-flat cycle into an immutable
ExperienceMemory episode, and derives a research candidate from those closed
economic outcomes.  It never promotes the candidate or grants trading/risk
authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.adaptive_ma import EXPERTS, UPDATE_RATE
from mvp.autotrade_mvp.exact_decimal import parse_bounded_exact_decimal
from research.autotrade_research.agents.dag import SpecialistSpec, plan_specialists
from research.autotrade_research.memory.episodes import ExperienceMemory


SCHEMA = "autotrade-closed-simulation-training-v2"
ALGORITHM = "CLOSED_RECONCILED_CYCLE_MULTIPLICATIVE_SCORE_V1"
LABEL_RULE = "CLOSED_SIMULATION_POSITION_CYCLE_ONLY"
PERMISSION = "HISTORICAL_SIMULATION_RESEARCH"
TASK = "historical-closed-outcome-learning"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DECISION_KEYS = {
    "episode", "status", "decision", "cash", "position", "equity",
    "reconciled", "order_id", "fill_id", "reconciliation_event_id",
    "protocol_digest", "emergency",
}
_HISTORICAL_EXECUTION_KEYS = {
    "status", "requested_quantity", "executed_quantity", "completed_at",
    "available_at", "model_fingerprint", "bar_sha256",
}


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return sha256(_canonical(payload)).hexdigest()


def _utc(value: datetime, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"{name} must be an exact timezone-aware datetime")
    normalized = value.astimezone(timezone.utc)
    if normalized.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return normalized


def _candidate_role(fast: int, slow: int) -> str:
    return f"moving-average-{fast}-{slow}"


def _historical_execution_available_at(
    decision: dict,
    *,
    expected_at: datetime,
) -> datetime:
    """Admit only the canonical completed-bar receipt, never arbitrary labels."""
    evidence = decision.get("historical_execution")
    if evidence is None:
        return expected_at
    if type(evidence) is not dict or set(evidence) != _HISTORICAL_EXECUTION_KEYS:
        raise ValueError("historical execution evidence schema differs")
    if (evidence["status"] not in {"FILLED", "PARTIAL", "NO_FILL"}
            or decision["status"] != evidence["status"]):
        raise ValueError("historical execution status differs from the decision")
    requested = parse_bounded_exact_decimal(evidence["requested_quantity"])
    executed = parse_bounded_exact_decimal(evidence["executed_quantity"])
    if requested <= 0 or executed < 0 or executed > requested:
        raise ValueError("historical execution quantity is invalid")
    expected_status = (
        "NO_FILL" if executed == 0 else
        "FILLED" if executed == requested else
        "PARTIAL"
    )
    if evidence["status"] != expected_status:
        raise ValueError("historical execution quantity/status differs")
    if (type(evidence["completed_at"]) is not str
            or not evidence["completed_at"].endswith("Z")
            or type(evidence["available_at"]) is not str
            or not evidence["available_at"].endswith("Z")):
        raise ValueError("historical execution availability is malformed")
    try:
        completed = datetime.fromisoformat(
            evidence["completed_at"].replace("Z", "+00:00")
        )
        available = datetime.fromisoformat(
            evidence["available_at"].replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("historical execution availability is malformed") from exc
    if (completed.tzinfo is None or available.tzinfo is None
            or completed.astimezone(timezone.utc) != available.astimezone(timezone.utc)
            or available.astimezone(timezone.utc) != expected_at):
        raise ValueError("historical execution is not the expected newly completed bar")
    fingerprint = evidence["model_fingerprint"]
    bar_digest = evidence["bar_sha256"]
    if (type(fingerprint) is not str or _DIGEST.fullmatch(fingerprint) is None
            or type(bar_digest) is not str
            or _PROTOCOL_DIGEST.fullmatch(bar_digest) is None):
        raise ValueError("historical execution identity is malformed")
    return available.astimezone(timezone.utc)


def _closed_cycles(
    simulation: dict,
    *,
    initial_cash: Decimal,
    first_execution_at: datetime,
    interval_seconds: int,
) -> tuple[dict, ...]:
    """Return only terminal flat-to-position-to-flat reconciled cycles."""
    if type(simulation) is not dict:
        raise TypeError("simulation result must be an exact object")
    if (
        simulation.get("status") not in {"COMPLETED", "PAUSED"}
        or simulation.get("mode") != "ZERO"
        or simulation.get("reconciled") is not True
        or simulation.get("economic_edge_status") != "INCONCLUSIVE"
    ):
        raise ValueError("training requires a completed-prefix reconciled ZERO simulation")
    protocol = simulation.get("protocol_digest")
    if type(protocol) is not str or _PROTOCOL_DIGEST.fullmatch(protocol) is None:
        raise ValueError("simulation protocol digest is not canonical")
    decisions = simulation.get("decisions")
    if type(decisions) is not list or not decisions:
        raise ValueError("simulation has no completed decisions")
    if simulation.get("completed_episodes") != len(decisions):
        raise ValueError("simulation completion count differs from decisions")

    prior_position = Decimal("0")
    flat_cash = initial_cash
    opened: dict | None = None
    cycles: list[dict] = []
    for expected_episode, decision in enumerate(decisions, 1):
        if (type(decision) is not dict
                or frozenset(decision) not in {
                    frozenset(_DECISION_KEYS),
                    frozenset(_DECISION_KEYS | {"historical_execution"}),
                }):
            # An exact schema gate is intentional: a caller cannot smuggle a
            # future_close, future_label or hindsight field into learning.
            raise ValueError("simulation decision schema differs; future/outcome leakage refused")
        if decision["episode"] != expected_episode or type(decision["episode"]) is not int:
            raise ValueError("simulation decisions are not gap-free chronological episodes")
        if decision["protocol_digest"] != protocol or decision["reconciled"] is not True:
            raise ValueError("simulation decision is not bound to reconciled protocol authority")
        position = parse_bounded_exact_decimal(decision["position"])
        cash = parse_bounded_exact_decimal(decision["cash"])
        parse_bounded_exact_decimal(decision["equity"])
        if position < 0 or cash < 0:
            raise ValueError("historical SPOT learning does not admit negative position/cash")
        expected_at = first_execution_at + timedelta(
            seconds=(expected_episode - 1) * interval_seconds,
        )
        if "historical_execution" in decision:
            # The production next-completed-bar adapter publishes this receipt
            # one interval after its decision cut.
            expected_at += timedelta(seconds=interval_seconds)
        available_at = _historical_execution_available_at(
            decision,
            expected_at=expected_at,
        )
        if prior_position == 0 and position > 0:
            if decision["status"] not in {"FILLED", "PARTIAL"} or not decision["fill_id"]:
                raise ValueError("position opening lacks canonical completed fill evidence")
            opened = {
                "episode": expected_episode,
                "cash_before": flat_cash,
                "fill_id": decision["fill_id"],
                "available_at": available_at,
            }
        elif prior_position > 0 and position == 0:
            if (opened is None
                    or decision["status"] not in {"FILLED", "PARTIAL"}
                    or not decision["fill_id"]):
                raise ValueError("position close lacks matching canonical completed fill evidence")
            pnl = cash - opened["cash_before"]
            cycles.append({
                "open_episode": opened["episode"],
                "close_episode": expected_episode,
                "open_fill_id": opened["fill_id"],
                "close_fill_id": decision["fill_id"],
                "closed_at": available_at,
                "net_cash_outcome": pnl,
                "outcome_class": "POSITIVE" if pnl > 0 else "NEGATIVE" if pnl < 0 else "NULL",
                "protocol_digest": protocol,
                "reconciliation_event_id": decision["reconciliation_event_id"],
            })
            flat_cash = cash
            opened = None
        prior_position = position
    if (
        decisions[-1]["cash"] != simulation.get("cash")
        or decisions[-1]["position"] != simulation.get("position")
    ):
        raise ValueError("simulation terminal account state differs from decisions")
    # An open tail is intentionally not a label.  It is ignored until a later
    # resumed simulation produces its reconciled closing fill.
    return tuple(cycles)


def train_closed_simulation_outcomes(
    *,
    candidate_simulations: list[dict],
    memory_path: str | Path,
    dataset_sha256: str,
    symbol: str,
    interval: str,
    first_execution_at: datetime,
    initial_cash: str = "1000",
    seed: int = 0,
    embargo_seconds: int = 3600,
    split_role: str = "TRAIN",
    expected_execution_count: int | None = None,
) -> dict:
    """Build one immutable candidate from CLOSED simulation outcomes only."""
    if type(candidate_simulations) is not list or not candidate_simulations:
        raise ValueError("candidate_simulations must be a non-empty exact list")
    if type(dataset_sha256) is not str or _DIGEST.fullmatch(dataset_sha256) is None:
        raise ValueError("training source requires a canonical SHA-256 digest")
    if type(symbol) is not str or not symbol or interval != "1h":
        raise ValueError("historical learning requires a named 1h instrument")
    start = _utc(first_execution_at, name="first_execution_at")
    if type(seed) is not int or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative exact integer")
    if type(embargo_seconds) is not int or isinstance(embargo_seconds, bool) or embargo_seconds < 3600:
        raise ValueError("training embargo must be at least one complete interval")
    if split_role != "TRAIN":
        raise ValueError("learning updates are forbidden outside the TRAIN split")
    if (
        expected_execution_count is not None
        and (type(expected_execution_count) is not int or expected_execution_count < 1)
    ):
        raise ValueError("expected execution count must be a positive exact integer")
    cash = parse_bounded_exact_decimal(initial_cash)
    if cash <= 0:
        raise ValueError("initial cash must be positive")

    admitted: list[tuple[tuple[int, int], dict, tuple[dict, ...]]] = []
    seen_parameters: set[tuple[int, int]] = set()
    seen_protocols: set[str] = set()
    seen_execution_counts: set[int] = set()
    for item in candidate_simulations:
        if type(item) is not dict or set(item) != {
            "parameters", "dataset_identity_sha256", "execution_count", "simulation",
        }:
            raise ValueError("candidate entry schema differs")
        if item["dataset_identity_sha256"] != dataset_sha256:
            raise ValueError("candidate simulation is not bound to the training dataset")
        execution_count = item["execution_count"]
        if (
            type(execution_count) is not int or execution_count < 1
            or item["simulation"].get("completed_episodes") != execution_count
            or (expected_execution_count is not None
                and execution_count > expected_execution_count)
        ):
            raise ValueError("candidate simulation does not cover the frozen execution cut")
        parameters = item["parameters"]
        if type(parameters) is not dict or set(parameters) != {"fast", "slow"}:
            raise ValueError("candidate parameters are malformed")
        pair = (parameters["fast"], parameters["slow"])
        if any(type(value) is not int for value in pair) or pair not in EXPERTS:
            raise ValueError("candidate parameters are not registered experts")
        if pair in seen_parameters:
            raise ValueError("candidate parameters cannot be reused")
        cycles = _closed_cycles(
            item["simulation"], initial_cash=cash,
            first_execution_at=start, interval_seconds=3600,
        )
        protocol = item["simulation"]["protocol_digest"]
        if protocol in seen_protocols:
            raise ValueError("one simulation outcome cannot label multiple candidates")
        seen_parameters.add(pair)
        seen_protocols.add(protocol)
        seen_execution_counts.add(execution_count)
        admitted.append((pair, item["simulation"], cycles))

    if seen_parameters != set(EXPERTS):
        raise ValueError("training must evaluate every registered expert exactly once")
    if len(seen_execution_counts) != 1:
        raise ValueError("candidate simulations must cover the same frozen execution cut")
    if any(not cycles for _pair, _simulation, cycles in admitted):
        raise ValueError("every candidate requires at least one CLOSED simulated position cycle")

    input_identity = {
        "schema": SCHEMA,
        "dataset_sha256": dataset_sha256,
        "symbol": symbol,
        "interval": interval,
        "first_execution_at": start.isoformat(),
        "initial_cash": format(cash, "f"),
        "seed": seed,
        "embargo_seconds": embargo_seconds,
        "execution_count": next(iter(seen_execution_counts)),
        "candidate_protocols": [
            {"fast": pair[0], "slow": pair[1], "protocol_digest": simulation["protocol_digest"]}
            for pair, simulation, _cycles in sorted(admitted)
        ],
    }
    input_digest = _digest(input_identity)
    specs = tuple(
        SpecialistSpec(
            role_id=_candidate_role(*pair), correlation_group="moving-average",
            max_cost=Decimal("1"), expected_incremental_value=Decimal("1"),
            required_inputs=("closed_simulation_outcomes",),
        )
        for pair in EXPERTS
    )
    dag = plan_specialists(
        specs, input_snapshot_id="sha256:" + input_digest,
        available_inputs=("closed_simulation_outcomes",), total_budget=str(len(EXPERTS)),
    )
    if dag.scheduled_roles != tuple(_candidate_role(*pair) for pair in EXPERTS):
        raise ValueError("canonical research DAG did not schedule the frozen expert registry")

    memory = ExperienceMemory(memory_path)
    episode_rows: dict[tuple[int, int], list[dict]] = {pair: [] for pair in EXPERTS}
    latest_close = start
    for pair, simulation, cycles in sorted(admitted):
        # Bind the embargo to when the latest admitted CLOSED outcome actually
        # became available. A next-completed-bar receipt is intentionally one
        # interval later than its decision cut.
        latest_close = max(latest_close, *(cycle["closed_at"] for cycle in cycles))
        role = _candidate_role(*pair)
        for cycle in cycles:
            identity = str(uuid5(
                NAMESPACE_URL,
                f"autotrade:{input_digest}:{role}:{cycle['open_episode']}:{cycle['close_episode']}",
            ))
            pnl = cycle["net_cash_outcome"]
            payload = {
                "evidence_refs": [
                    f"simulation:{cycle['protocol_digest']}",
                    f"reconciliation:{cycle['reconciliation_event_id']}",
                    f"fill:{cycle['open_fill_id']}",
                    f"fill:{cycle['close_fill_id']}",
                ],
                "intended_action": "UPDATE_RESEARCH_CANDIDATE_WEIGHT",
                "actual_execution": "NONE_LEARNING_CANNOT_ROUTE_ORDERS",
                "outcome": {
                    "status": "CLOSED", "class": cycle["outcome_class"],
                    "net_cash": format(pnl, "f"),
                    "open_episode": cycle["open_episode"],
                    "close_episode": cycle["close_episode"],
                },
                "costs": "INCLUDED_IN_RECONCILED_SIMULATION_CASH_DELTA",
                "learning": {
                    "algorithm": ALGORITHM, "algorithm_version": "1",
                    "rng_seed": seed, "candidate_role": role,
                    "label_version": "CLOSED_SIMULATION_OUTCOME_V1",
                    "label_available_at": cycle["closed_at"].isoformat(),
                    "source_dataset_sha256": dataset_sha256,
                    "split_role": "TRAIN",
                },
            }
            episode_id, _inserted = memory.append_episode(
                decision_time=cycle["closed_at"], information_cutoff=cycle["closed_at"],
                task=TASK, regime="HISTORICAL", instrument_family=symbol,
                permission_class=PERMISSION, payload=payload, episode_id=identity,
            )
            stored = memory.source_episode(
                episode_id, information_cutoff=datetime(9999, 12, 31, tzinfo=timezone.utc),
                granted_permissions={PERMISSION},
            )
            episode_rows[pair].append({
                "episode_id": episode_id, "episode_hash": stored["episode_hash"],
                "outcome_class": cycle["outcome_class"],
                "net_cash": format(pnl, "f"),
                "label_available_at": cycle["closed_at"].isoformat(),
            })

    population = memory.coverage_population_snapshot(
        causal_cutoff=datetime(9999, 12, 31, tzinfo=timezone.utc),
        granted_permissions={PERMISSION}, task=TASK, instrument_family=symbol,
    )
    expected_episode_count = sum(len(rows) for rows in episode_rows.values())
    if population.eligible_count != expected_episode_count:
        raise ValueError("learning memory contains outcomes outside this frozen training input")
    # ExperienceMemory availability timestamps correctly preserve actual append
    # chronology, so its query root varies across independent machines.  The
    # versioned model binds a deterministic projection of the same immutable
    # episode identities/hashes; the SQLite trail remains the append authority.
    deterministic_episode_root = "sha256:" + _digest({
        "schema": "historical-learning-episode-root-v1",
        "episodes": sorted(
            (row["episode_id"], row["episode_hash"])
            for rows in episode_rows.values() for row in rows
        ),
    })
    weights = [Decimal("1") for _ in EXPERTS]
    with localcontext() as context:
        context.prec = 42
        for index, pair in enumerate(EXPERTS):
            for row in episode_rows[pair]:
                reward = Decimal("1") if row["outcome_class"] == "POSITIVE" else Decimal("-1") if row["outcome_class"] == "NEGATIVE" else Decimal("0")
                weights[index] *= Decimal("1") + UPDATE_RATE * reward
        total = sum(weights)
        weights = [weight / total for weight in weights]
    champion = max(range(len(EXPERTS)), key=lambda index: (weights[index], -index))
    candidates = [
        {
            "fast": pair[0], "slow": pair[1], "weight": format(weights[index], "f"),
            "closed_outcomes": len(episode_rows[pair]),
            "positive_outcomes": sum(row["outcome_class"] == "POSITIVE" for row in episode_rows[pair]),
            "negative_outcomes": sum(row["outcome_class"] == "NEGATIVE" for row in episode_rows[pair]),
            "audit_episode_hashes": [row["episode_hash"] for row in episode_rows[pair]],
        }
        for index, pair in enumerate(EXPERTS)
    ]
    audit = {
        "trail": "EXPERIENCE_MEMORY_APPEND_ONLY",
        "population_root": deterministic_episode_root,
        "episode_count": population.eligible_count,
        "input_digest": input_digest,
        "dag_snapshot_id": dag.input_snapshot_id,
        "dag_scheduled_roles": list(dag.scheduled_roles),
        "replay_contract": "IDENTICAL_INPUT_AND_MEMORY_PATH_MUST_NOT_APPEND_OR_CHANGE_MODEL",
    }
    version = f"closed-zero-v1-{input_digest[:16]}-{deterministic_episode_root[-16:]}"
    payload = {
        "schema": SCHEMA,
        "strategy_id": "moving-average-closed-simulation-experts",
        "strategy_version": "2",
        "model_registry": {
            "candidate_id": f"historical-ma:{symbol}:{version}",
            "version": version,
            "routing_status": "FROZEN_CANDIDATE_NOT_PROMOTED",
        },
        "source_dataset_sha256": dataset_sha256,
        "symbol": symbol, "interval": interval,
        "training_start_open_utc": start.isoformat(),
        "training_last_close_utc": latest_close.isoformat(),
        "training_split": "TRAIN",
        "embargo_seconds": embargo_seconds,
        "rng_seed": seed,
        "label_rule": LABEL_RULE,
        "update_rule": ALGORITHM,
        "candidates": candidates,
        "selected_parameters": {
            "fast": EXPERTS[champion][0], "slow": EXPERTS[champion][1],
        },
        "learning_audit": audit,
        "economic_edge_status": "NOT_EVALUATED_USE_INDEPENDENT_VALIDATION_AND_HOLDOUT",
    }
    return {**payload, "model_sha256": _digest(payload)}
