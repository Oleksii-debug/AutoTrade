"""Causal offline parameter learning for the canonical ZERO moving-average agent.

This module chooses strategy parameters from completed close-to-close labels.
It neither creates orders nor calculates simulated account balances. The
canonical risk, OMS, provider and financial journal remain the only execution
path. A trained candidate must be evaluated on a later independent cut.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json

from .exact_decimal import parse_bounded_exact_decimal


SCHEMA = "autotrade-causal-ma-training-v1"
CLOSED_SIMULATION_SCHEMA = "autotrade-closed-simulation-training-v2"
EXPERTS = ((2, 3), (3, 5), (5, 8))
UPDATE_RATE = Decimal("0.05")


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _direction(values: list[Decimal], fast: int, slow: int) -> int | None:
    if len(values) < slow:
        return None
    fast_sum = sum(values[-fast:]) * slow
    slow_sum = sum(values[-slow:]) * fast
    return 1 if fast_sum > slow_sum else -1 if fast_sum < slow_sum else 0


def train_closed_candles(
    *, dates: list[datetime], prices: list[str], dataset_sha256: str,
    symbol: str, interval: str,
) -> dict:
    """Update candidate weights only after the next closed-bar label matures.

    For each historical close t, a candidate predicts the direction of the
    subsequent close. The label for t is applied at t+1, never at t. Direction
    classification is training evidence, not a claim of executed profitability.
    """
    if len(dates) != len(prices) or not 10 <= len(prices) <= 10000:
        raise ValueError("training requires 10..10000 aligned closed candles")
    if type(dataset_sha256) is not str or len(dataset_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in dataset_sha256
    ):
        raise ValueError("training source requires a canonical SHA-256 digest")
    if type(symbol) is not str or type(interval) is not str or not symbol or not interval:
        raise ValueError("training instrument identity is required")
    if any(type(day) is not datetime or day.tzinfo is None or
           day.utcoffset() != timezone.utc.utcoffset(day) for day in dates):
        raise ValueError("training timestamps must be exact UTC instants")
    from datetime import timedelta
    step_seconds = {"1h": 3600}.get(interval)
    if step_seconds is None or any(
        later - earlier != timedelta(seconds=step_seconds)
        for earlier, later in zip(dates, dates[1:])
    ):
        raise ValueError("training candles must form one continuous hourly UTC cut")
    values = [parse_bounded_exact_decimal(price) for price in prices]
    if any(value <= 0 for value in values) or any(
        later <= earlier for earlier, later in zip(dates, dates[1:])
    ):
        raise ValueError("training input order or positive prices are invalid")
    weights = [Decimal(1) for _ in EXPERTS]
    correct = [0 for _ in EXPERTS]
    labels = [0 for _ in EXPERTS]
    with localcontext() as context:
        context.prec = 42
        for index in range(1, len(values)):
            # The prediction is frozen on the previous close. The current
            # close supplies its outcome only now, after it is complete.
            past = values[:index]
            actual = 1 if values[index] > values[index-1] else -1 if values[index] < values[index-1] else 0
            for candidate, (fast, slow) in enumerate(EXPERTS):
                predicted = _direction(past, fast, slow)
                if predicted is None or actual == 0:
                    continue
                labels[candidate] += 1
                if predicted == actual:
                    correct[candidate] += 1
                reward = 1 if predicted == actual else -1
                weights[candidate] *= 1 + UPDATE_RATE * reward
            # Normalize to prevent unbounded decimal growth. This does not
            # change the ordering used to select a candidate.
            total = sum(weights)
            weights = [weight / total for weight in weights]
    champion = max(range(len(EXPERTS)), key=lambda i: (weights[i], -i))
    payload = {
        "schema": SCHEMA,
        "strategy_id": "moving-average-causal-experts",
        "strategy_version": "1",
        "source_dataset_sha256": dataset_sha256,
        "symbol": symbol, "interval": interval,
        "training_start_open_utc": dates[0].isoformat(),
        "training_last_close_utc": (dates[-1] + timedelta(seconds=step_seconds)).isoformat(),
        "label_rule": "NEXT_CLOSED_BAR_DIRECTION_KNOWN_ONLY_AT_ITS_CLOSE",
        "update_rule": "MULTIPLICATIVE_DIRECTIONAL_SCORE_ETA_0.05_V1",
        "candidates": [
            {"fast": fast, "slow": slow, "weight": format(weights[i], "f"),
             "matured_labels": labels[i], "correct_labels": correct[i]}
            for i, (fast, slow) in enumerate(EXPERTS)
        ],
        "selected_parameters": {"fast": EXPERTS[champion][0], "slow": EXPERTS[champion][1]},
        "economic_edge_status": "NOT_EVALUATED_USE_INDEPENDENT_HOLDOUT",
    }
    return {**payload, "model_sha256": sha256(_canonical(payload)).hexdigest()}


def verify_model(model: dict, *, symbol: str, interval: str,
                 holdout_start_open: datetime) -> tuple[int, int, str]:
    if type(model) is dict and model.get("schema") == CLOSED_SIMULATION_SCHEMA:
        return _verify_closed_simulation_model(
            model,
            symbol=symbol,
            interval=interval,
            holdout_start_open=holdout_start_open,
        )
    if type(model) is not dict or set(model) != {
        "schema", "strategy_id", "strategy_version", "source_dataset_sha256",
        "symbol", "interval", "training_start_open_utc", "training_last_close_utc",
        "label_rule", "update_rule", "candidates", "selected_parameters",
        "economic_edge_status", "model_sha256",
    }:
        raise ValueError("trained model schema differs")
    payload = {key: value for key, value in model.items() if key != "model_sha256"}
    digest = model["model_sha256"]
    if type(digest) is not str or sha256(_canonical(payload)).hexdigest() != digest:
        raise ValueError("trained model integrity differs")
    if (model["schema"] != SCHEMA or model["strategy_id"] != "moving-average-causal-experts"
        or model["strategy_version"] != "1" or model["symbol"] != symbol
        or model["interval"] != interval or model["update_rule"] != "MULTIPLICATIVE_DIRECTIONAL_SCORE_ETA_0.05_V1"
        or model["label_rule"] != "NEXT_CLOSED_BAR_DIRECTION_KNOWN_ONLY_AT_ITS_CLOSE"
        or model["economic_edge_status"] != "NOT_EVALUATED_USE_INDEPENDENT_HOLDOUT"
    ):
        raise ValueError("trained model identity differs")
    try:
        last_close = datetime.fromisoformat(model["training_last_close_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("trained model clock is invalid") from exc
    if last_close.tzinfo is None or last_close.utcoffset() != timezone.utc.utcoffset(last_close):
        raise ValueError("trained model clock is not UTC")
    if type(holdout_start_open) is not datetime or holdout_start_open.tzinfo is None or last_close > holdout_start_open:
        raise ValueError("training overlaps the independent holdout")
    selected = model["selected_parameters"]
    if type(selected) is not dict or set(selected) != {"fast", "slow"}:
        raise ValueError("trained model parameters are malformed")
    pair = (selected["fast"], selected["slow"])
    if pair not in EXPERTS or any(type(value) is not int for value in pair):
        raise ValueError("trained model parameters are not registered")
    candidates = model["candidates"]
    if type(candidates) is not list or len(candidates) != len(EXPERTS) or any(
        type(row) is not dict or set(row) != {"fast", "slow", "weight", "matured_labels", "correct_labels"}
        or (row["fast"], row["slow"]) != expert
        or type(row["matured_labels"]) is not int or type(row["correct_labels"]) is not int
        or not 0 <= row["correct_labels"] <= row["matured_labels"]
        or parse_bounded_exact_decimal(row["weight"]) < 0
        for row, expert in zip(candidates, EXPERTS)
    ):
        raise ValueError("trained candidate weights are malformed")
    if pair != EXPERTS[max(range(len(EXPERTS)), key=lambda i: (
        parse_bounded_exact_decimal(candidates[i]["weight"]), -i
    ))]:
        raise ValueError("trained selected policy conflicts with weights")
    return pair[0], pair[1], digest


def _verify_closed_simulation_model(
    model: dict, *, symbol: str, interval: str,
    holdout_start_open: datetime,
) -> tuple[int, int, str]:
    """Verify a frozen history-only candidate without granting routing authority."""
    expected = {
        "schema", "strategy_id", "strategy_version", "model_registry",
        "source_dataset_sha256", "symbol", "interval",
        "training_start_open_utc", "training_last_close_utc", "training_split",
        "embargo_seconds", "rng_seed", "label_rule", "update_rule",
        "candidates", "selected_parameters", "learning_audit",
        "economic_edge_status", "model_sha256",
    }
    if set(model) != expected:
        raise ValueError("trained closed-simulation model schema differs")
    payload = {key: value for key, value in model.items() if key != "model_sha256"}
    digest = model["model_sha256"]
    if type(digest) is not str or sha256(_canonical(payload)).hexdigest() != digest:
        raise ValueError("trained model integrity differs")
    if (
        model["strategy_id"] != "moving-average-closed-simulation-experts"
        or model["strategy_version"] != "2"
        or model["symbol"] != symbol
        or model["interval"] != interval
        or model["training_split"] != "TRAIN"
        or model["label_rule"] != "CLOSED_SIMULATION_POSITION_CYCLE_ONLY"
        or model["update_rule"] != "CLOSED_RECONCILED_CYCLE_MULTIPLICATIVE_SCORE_V1"
        or model["economic_edge_status"]
        != "NOT_EVALUATED_USE_INDEPENDENT_VALIDATION_AND_HOLDOUT"
    ):
        raise ValueError("trained closed-simulation model identity differs")
    source = model["source_dataset_sha256"]
    if type(source) is not str or len(source) != 64 or any(
        character not in "0123456789abcdef" for character in source
    ):
        raise ValueError("trained model source digest is malformed")
    if type(model["rng_seed"]) is not int or isinstance(model["rng_seed"], bool) or model["rng_seed"] < 0:
        raise ValueError("trained model seed is malformed")
    embargo = model["embargo_seconds"]
    if type(embargo) is not int or isinstance(embargo, bool) or embargo < 3600:
        raise ValueError("trained model embargo is malformed")
    try:
        training_start = datetime.fromisoformat(model["training_start_open_utc"])
        last_close = datetime.fromisoformat(model["training_last_close_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("trained model clock is invalid") from exc
    if (
        training_start.tzinfo is None or last_close.tzinfo is None
        or training_start.utcoffset() != timezone.utc.utcoffset(training_start)
        or last_close.utcoffset() != timezone.utc.utcoffset(last_close)
        or training_start > last_close
        or type(holdout_start_open) is not datetime
        or holdout_start_open.tzinfo is None
    ):
        raise ValueError("trained model clock is not UTC")
    from datetime import timedelta
    if last_close + timedelta(seconds=embargo) > holdout_start_open:
        raise ValueError("training plus embargo overlaps the independent holdout")
    registry = model["model_registry"]
    if (
        type(registry) is not dict
        or set(registry) != {"candidate_id", "version", "routing_status"}
        or registry.get("routing_status") != "FROZEN_CANDIDATE_NOT_PROMOTED"
        or type(registry.get("candidate_id")) is not str
        or not registry["candidate_id"].startswith(f"historical-ma:{symbol}:")
        or type(registry.get("version")) is not str
        or not registry["version"]
        or not registry["candidate_id"].endswith(registry["version"])
    ):
        raise ValueError("trained model registry freeze is malformed")
    audit = model["learning_audit"]
    if (
        type(audit) is not dict
        or set(audit) != {
            "trail", "population_root", "episode_count", "input_digest",
            "dag_snapshot_id", "dag_scheduled_roles", "replay_contract",
        }
        or audit["trail"] != "EXPERIENCE_MEMORY_APPEND_ONLY"
        or type(audit["population_root"]) is not str
        or not audit["population_root"].startswith("sha256:")
        or type(audit["episode_count"]) is not int
        or audit["episode_count"] < len(EXPERTS)
        or audit["dag_snapshot_id"] != "sha256:" + audit["input_digest"]
        or audit["dag_scheduled_roles"]
        != [f"moving-average-{fast}-{slow}" for fast, slow in EXPERTS]
        or audit["replay_contract"]
        != "IDENTICAL_INPUT_AND_MEMORY_PATH_MUST_NOT_APPEND_OR_CHANGE_MODEL"
    ):
        raise ValueError("trained model learning audit is malformed")
    candidates = model["candidates"]
    if type(candidates) is not list or len(candidates) != len(EXPERTS):
        raise ValueError("trained candidate weights are malformed")
    for row, expert in zip(candidates, EXPERTS):
        if (
            type(row) is not dict
            or set(row) != {
                "fast", "slow", "weight", "closed_outcomes",
                "positive_outcomes", "negative_outcomes", "audit_episode_hashes",
            }
            or (row["fast"], row["slow"]) != expert
            or type(row["closed_outcomes"]) is not int
            or type(row["positive_outcomes"]) is not int
            or type(row["negative_outcomes"]) is not int
            or row["closed_outcomes"] < 1
            or row["positive_outcomes"] + row["negative_outcomes"] > row["closed_outcomes"]
            or type(row["audit_episode_hashes"]) is not list
            or len(row["audit_episode_hashes"]) != row["closed_outcomes"]
            or any(type(value) is not str or not value.startswith("sha256:")
                   for value in row["audit_episode_hashes"])
            or parse_bounded_exact_decimal(row["weight"]) < 0
        ):
            raise ValueError("trained candidate closed outcomes are malformed")
    if sum(row["closed_outcomes"] for row in candidates) != audit["episode_count"]:
        raise ValueError("trained model audit population count differs")
    selected = model["selected_parameters"]
    if type(selected) is not dict or set(selected) != {"fast", "slow"}:
        raise ValueError("trained model parameters are malformed")
    pair = (selected["fast"], selected["slow"])
    winner = EXPERTS[max(range(len(EXPERTS)), key=lambda index: (
        parse_bounded_exact_decimal(candidates[index]["weight"]), -index
    ))]
    if pair != winner:
        raise ValueError("trained selected policy conflicts with closed outcomes")
    return pair[0], pair[1], digest


def validate_closed_candles(model: dict, *, dates: list[datetime],
                            prices: list[str], dataset_sha256: str,
                            symbol: str, interval: str) -> dict:
    """Score a frozen model on a disjoint period without updating weights."""
    if len(dates) != len(prices) or not 10 <= len(prices) <= 10000:
        raise ValueError("validation requires 10..10000 aligned closed candles")
    fast, slow, digest = verify_model(
        model, symbol=symbol, interval=interval,
        holdout_start_open=dates[0],
    )
    if type(dataset_sha256) is not str or len(dataset_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in dataset_sha256
    ):
        raise ValueError("validation requires an immutable source digest")
    from datetime import timedelta
    step = timedelta(hours=1)
    if interval != "1h" or any(
        type(day) is not datetime or day.tzinfo is None or
        day.utcoffset() != timezone.utc.utcoffset(day)
        for day in dates
    ) or any(b-a != step for a, b in zip(dates, dates[1:])):
        raise ValueError("validation requires continuous hourly UTC bars")
    values = [parse_bounded_exact_decimal(price) for price in prices]
    if any(value <= 0 for value in values):
        raise ValueError("validation prices must be positive")
    correct = matured = 0
    for index in range(1, len(values)):
        predicted = _direction(values[:index], fast, slow)
        actual = 1 if values[index] > values[index - 1] else -1 if values[index] < values[index - 1] else 0
        if predicted is None or actual == 0:
            continue
        matured += 1
        correct += predicted == actual
    report = {
        "status": "INDEPENDENT_DIRECTIONAL_VALIDATION_NOT_TRADING_PERFORMANCE",
        "model_sha256": digest,
        "validation_dataset_sha256": dataset_sha256,
        "symbol": symbol, "interval": interval,
        "validation_start_open_utc": dates[0].isoformat(),
        "validation_last_close_utc": (dates[-1] + step).isoformat(),
        "selected_parameters": {"fast": fast, "slow": slow},
        "matured_labels": matured, "correct_labels": correct,
        "accuracy_fraction": (format(Decimal(correct) / Decimal(matured), "f")
                              if matured else None),
        "model_updated": False,
    }
    return {**report, "report_sha256": sha256(_canonical(report)).hexdigest()}


def verify_validation_report(report: dict, *, model_sha256: str,
                             symbol: str, interval: str,
                             holdout_start_open: datetime,
                             selected_parameters: tuple[int, int] | None = None) -> str:
    if type(report) is not dict or set(report) != {
        "status", "model_sha256", "validation_dataset_sha256", "symbol",
        "interval", "validation_start_open_utc", "validation_last_close_utc",
        "selected_parameters", "matured_labels", "correct_labels",
        "accuracy_fraction", "model_updated", "report_sha256",
    }:
        raise ValueError("validation report schema differs")
    payload = {key: value for key, value in report.items() if key != "report_sha256"}
    digest = report["report_sha256"]
    if type(digest) is not str or digest != sha256(_canonical(payload)).hexdigest():
        raise ValueError("validation report integrity differs")
    if (report["status"] != "INDEPENDENT_DIRECTIONAL_VALIDATION_NOT_TRADING_PERFORMANCE"
        or report["model_sha256"] != model_sha256 or report["symbol"] != symbol
        or report["interval"] != interval or report["model_updated"] is not False
        or type(report["validation_dataset_sha256"]) is not str
        or len(report["validation_dataset_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in report["validation_dataset_sha256"])
    ):
        raise ValueError("validation report model or instrument differs")
    selected = report["selected_parameters"]
    if (type(selected) is not dict or set(selected) != {"fast", "slow"}
        or (selected["fast"], selected["slow"]) not in EXPERTS
        or (selected_parameters is not None and
            (selected["fast"], selected["slow"]) != selected_parameters)
    ):
        raise ValueError("validation selected policy differs from trained model")
    try:
        start = datetime.fromisoformat(report["validation_start_open_utc"])
        end = datetime.fromisoformat(report["validation_last_close_utc"])
    except (TypeError, ValueError) as exc:
        raise ValueError("validation report clock is invalid") from exc
    if (start.tzinfo is None or end.tzinfo is None or
        start.utcoffset() != timezone.utc.utcoffset(start) or
        end.utcoffset() != timezone.utc.utcoffset(end) or
        not start < end <= holdout_start_open):
        raise ValueError("validation overlaps the independent holdout")
    count, correct = report["matured_labels"], report["correct_labels"]
    if (type(count) is not int or type(correct) is not int or
        not 0 <= correct <= count or
        count > int((end-start).total_seconds() // 3600) or
        report["accuracy_fraction"] != (
            format(Decimal(correct) / Decimal(count), "f") if count else None
        )):
        raise ValueError("validation label arithmetic differs")
    return digest
