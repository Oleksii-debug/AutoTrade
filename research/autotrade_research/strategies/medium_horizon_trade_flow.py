"""Locked-final medium-horizon aggressor-flow research.

This screen is deliberately separate from the permanent short-horizon protocol.
It asks whether completed five-second flow can forecast moves large enough to
clear a conservative spot round trip over three to fifteen minutes.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from .trade_flow_screen import FlowCandidate, _hold, _read_rows, _simulate
from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-whitebit-medium-horizon-flow-screen-v1"
CANDIDATE_REGISTRY_VERSION = "MEDIUM_FLOW_PRICE_RESPONSE_V1"
FAMILIES = (
    "MOMENTUM",
    "CONTRARIAN",
    "CONFIRMED_MOMENTUM",
    "ABSORPTION_REVERSAL",
)


def candidates() -> tuple[FlowCandidate, ...]:
    """Return the immutable 96-candidate medium-horizon registry."""
    return tuple(
        FlowCandidate(family, lookback, threshold, horizon)
        for family in FAMILIES
        for lookback in (12, 36, 60)
        for threshold in ("0.5", "0.8")
        for horizon in (36, 60, 120, 180)
    )


def _failure_reasons(row: dict | None, *, minimum_trades: int,
                     drawdown_limit: Decimal, benchmark: Decimal) -> list[str]:
    if row is None:
        return ["NO_CANDIDATES"]
    reasons: list[str] = []
    if row["trade_count"] < minimum_trades:
        reasons.append("INSUFFICIENT_TRADES")
    if Decimal(row["maximum_drawdown_fraction"]) > drawdown_limit:
        reasons.append("DRAWDOWN_LIMIT")
    if Decimal(row["net_return_fraction"]) <= 0:
        reasons.append("NOT_POSITIVE_AFTER_COSTS")
    if Decimal(row["net_return_fraction"]) <= benchmark:
        reasons.append("DID_NOT_BEAT_CASH_OR_HOLD")
    return reasons


def screen_medium_horizon_flow(
    path: Path,
    market: str,
    *,
    minimum_buckets: int = 900,
    maximum_buckets: int = 4320,
    minimum_trades: int = 2,
    round_trip_cost_bps: str = "26",
    maximum_drawdown_fraction: str = "0.20",
) -> dict:
    if type(minimum_buckets) is not int or minimum_buckets < 900:
        raise ValueError("minimum_buckets must be at least 900")
    if (type(maximum_buckets) is not int or maximum_buckets < minimum_buckets
            or maximum_buckets > 17280):
        raise ValueError("maximum_buckets is invalid")
    if type(minimum_trades) is not int or not 1 <= minimum_trades <= 10000:
        raise ValueError("minimum_trades is invalid")
    try:
        cost = Decimal(round_trip_cost_bps) / Decimal(10000)
        drawdown_limit = Decimal(maximum_drawdown_fraction)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid screen economics") from None
    if (not cost.is_finite() or not drawdown_limit.is_finite()
            or not 0 <= cost < 1 or not 0 <= drawdown_limit <= 1):
        raise ValueError("invalid screen economics")

    market = _symbol(market)
    rows = _read_rows(path, market)
    base = {
        "schema": SCHEMA,
        "market": market,
        "candidate_registry_version": CANDIDATE_REGISTRY_VERSION,
        "available_causal_buckets": len(rows),
        "minimum_buckets": minimum_buckets,
        "maximum_buckets": maximum_buckets,
        "round_trip_cost_bps": format(Decimal(round_trip_cost_bps), "f"),
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
        "paid_model_calls": 0,
    }
    if len(rows) < minimum_buckets:
        result = {
            **base,
            "status": "WAITING_FOR_CAUSAL_BUCKETS",
            "analysis_bucket_count": 0,
            "prospective_qualification": False,
            "final_consumed": False,
        }
        result["result_sha256"] = sha256(json.dumps(
            result, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        return result

    rows = rows[-maximum_buckets:]
    train_end = int(len(rows) * 0.6)
    development_end = int(len(rows) * 0.8)
    partitions = {
        "TRAIN": rows[:train_end],
        "DEVELOPMENT": rows[train_end:development_end],
        "FINAL": rows[development_end:],
    }
    registry = candidates()
    training = [_simulate(partitions["TRAIN"], item, cost) for item in registry]
    train_hold = _hold(partitions["TRAIN"], cost)
    train_benchmark = max(Decimal(0), train_hold)
    best_train = max(
        training, key=lambda row: Decimal(row["net_return_fraction"]),
        default=None,
    )
    eligible = [
        row for row in training
        if row["trade_count"] >= minimum_trades
        and Decimal(row["maximum_drawdown_fraction"]) <= drawdown_limit
        and Decimal(row["net_return_fraction"]) > train_benchmark
    ]
    selected = None if not eligible else max(
        eligible, key=lambda row: Decimal(row["net_return_fraction"]),
    )

    development = final = None
    development_hold = development_benchmark = None
    development_failure_reasons: list[str] = []
    development_passed = False
    if selected is not None:
        chosen = FlowCandidate(**selected["candidate"])
        development = _simulate(partitions["DEVELOPMENT"], chosen, cost)
        development_hold = _hold(partitions["DEVELOPMENT"], cost)
        development_benchmark = max(Decimal(0), development_hold)
        development_passed = (
            development["trade_count"] >= minimum_trades
            and Decimal(development["maximum_drawdown_fraction"]) <= drawdown_limit
            and Decimal(development["net_return_fraction"]) > development_benchmark
        )
        if not development_passed:
            development_failure_reasons = _failure_reasons(
                development, minimum_trades=minimum_trades,
                drawdown_limit=drawdown_limit,
                benchmark=development_benchmark,
            )
        if development_passed:
            final = _simulate(partitions["FINAL"], chosen, cost)

    qualified = False
    if final is not None:
        final_benchmark = max(Decimal(0), _hold(partitions["FINAL"], cost))
        qualified = (
            final["trade_count"] >= minimum_trades
            and Decimal(final["maximum_drawdown_fraction"]) <= drawdown_limit
            and Decimal(final["net_return_fraction"]) > final_benchmark
        )

    result = {
        **base,
        "status": "QUALIFIED" if qualified else "REJECTED",
        "analysis_bucket_count": len(rows),
        "partition_counts": {key: len(value) for key, value in partitions.items()},
        "candidate_count": len(training),
        "selected_train": selected,
        "best_train_observed": best_train,
        "train_hold_return_fraction": format(train_hold, "f"),
        "train_required_return_fraction": format(train_benchmark, "f"),
        "train_failure_reasons": _failure_reasons(
            best_train, minimum_trades=minimum_trades,
            drawdown_limit=drawdown_limit, benchmark=train_benchmark,
        ) if selected is None else [],
        "development": development,
        "development_hold_return_fraction": (
            None if development_hold is None else format(development_hold, "f")
        ),
        "development_required_return_fraction": (
            None if development_benchmark is None
            else format(development_benchmark, "f")
        ),
        "development_failure_reasons": development_failure_reasons,
        "development_passed": development_passed,
        "final": final,
        "final_consumed": final is not None,
        "prospective_qualification": qualified,
        "limitations": [
            "SPOT_PRICE_SHORT_PROXY",
            "FIXED_COST_MODEL",
            "PUBLIC_WEBSOCKET_TRADE_TAPE",
            "NO_LIVE_ORDER_AUTHORITY",
        ],
    }
    result["result_sha256"] = sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return result
