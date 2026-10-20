"""Causal BTC spot volatility-expansion breakout for ZERO research.

This deliberately narrow experiment can hold BTC or cash.  It never borrows,
shorts, calls a model, or submits an order.  Entry and exit features are built
after a one-hour candle is complete; every ordinary decision is filled at the
following candle open.  A deterministic partition-end liquidation is the only
exception and exists solely to make isolated TRAIN/DEVELOPMENT/FINAL controls
comparable without carrying state across evidence partitions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4


VERSION = "volatility-expansion-breakout-v1"
EVIDENCE_CLASS = "ZERO_RESEARCH_ONLY_RETROSPECTIVE_SIMULATION"
CANDIDATES = ("CASH", "BTC_HOLD", "VOLATILITY_EXPANSION_BREAKOUT")


def digest(value: object) -> str:
    return sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def decimal(value: str | int | Decimal) -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise ValueError("numeric inputs must be exact decimal text or integers")
    result = Decimal(value)
    if (
        not result.is_finite()
        or result.copy_abs() > Decimal("1e18")
        or abs(result.as_tuple().exponent) > 18
        or len(result.as_tuple().digits) > 36
    ):
        raise ValueError("decimal input outside finite research budget")
    return result


@dataclass(frozen=True)
class ExecutionCosts:
    initial_cash: str = "1000"
    fee_bps: str = "10"
    spread_bps: str = "10"
    slippage_bps: str = "5"
    capital_fraction: str = "1"

    def validate(self) -> None:
        if decimal(self.initial_cash) <= 0:
            raise ValueError("initial cash must be positive")
        if any(
            not Decimal(0) <= decimal(value) < Decimal(1000)
            for value in (self.fee_bps, self.spread_bps, self.slippage_bps)
        ):
            raise ValueError("each execution cost must be in [0,1000) bps")
        if not Decimal(0) < decimal(self.capital_fraction) <= Decimal(1):
            raise ValueError("capital fraction must be in (0,1]")


@dataclass(frozen=True)
class StrategyConfiguration:
    breakout_lookback: int = 72
    volatility_lookback: int = 24
    range_expansion_multiplier: str = "1.5"
    minimum_range_fraction: str = "0.005"
    volume_lookback: int = 48
    volume_multiplier: str = "1.5"
    minimum_mean_quote_volume: str = "1000000"
    breakout_buffer_bps: str = "5"
    stop_loss_fraction: str = "0.04"
    exit_lookback: int = 24
    minimum_hold_bars: int = 12
    maximum_hold_bars: int = 168

    def validate(self) -> None:
        for name in (
            "breakout_lookback", "volatility_lookback", "volume_lookback",
            "exit_lookback",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 2 <= value <= 1000:
                raise ValueError(f"{name} must be an integer in [2,1000]")
        for name in ("range_expansion_multiplier", "volume_multiplier"):
            if not Decimal(1) <= decimal(getattr(self, name)) <= Decimal(100):
                raise ValueError(f"{name} must be in [1,100]")
        if not Decimal(0) <= decimal(self.minimum_range_fraction) <= Decimal(1):
            raise ValueError("minimum range fraction must be in [0,1]")
        if not Decimal(0) <= decimal(self.minimum_mean_quote_volume) <= Decimal("1e18"):
            raise ValueError("minimum mean quote volume must be in [0,1e18]")
        if not Decimal(0) <= decimal(self.breakout_buffer_bps) <= Decimal(1000):
            raise ValueError("breakout buffer must be in [0,1000] bps")
        if not Decimal(0) < decimal(self.stop_loss_fraction) <= Decimal(1):
            raise ValueError("stop loss fraction must be in (0,1]")
        if (
            type(self.minimum_hold_bars) is not int
            or type(self.maximum_hold_bars) is not int
            or not 1 <= self.minimum_hold_bars <= self.maximum_hold_bars <= 10000
        ):
            raise ValueError("holding bounds must satisfy 1 <= minimum <= maximum <= 10000")


@dataclass(frozen=True)
class GateConfiguration:
    minimum_closed_cycles: int = 8
    maximum_drawdown_fraction: str = "0.10"
    require_beats_hold: bool = True

    def validate(self) -> None:
        if (
            type(self.minimum_closed_cycles) is not int
            or not 1 <= self.minimum_closed_cycles <= 100000
        ):
            raise ValueError("minimum closed cycles must be in [1,100000]")
        if not Decimal(0) <= decimal(self.maximum_drawdown_fraction) <= Decimal(1):
            raise ValueError("maximum drawdown must be in [0,1]")
        if type(self.require_beats_hold) is not bool:
            raise ValueError("require_beats_hold must be boolean")


def validate_bars(bars: list[dict]) -> None:
    if len(bars) < 4:
        raise ValueError("at least four chronological bars are required")
    prior_close_time = None
    for bar in bars:
        if type(bar) is not dict:
            raise ValueError("each bar must be an exact dictionary")
        opened_at, closed_at = bar.get("opened_at"), bar.get("closed_at")
        if (
            type(opened_at) is not datetime
            or type(closed_at) is not datetime
            or opened_at.utcoffset() is None
            or closed_at.utcoffset() is None
            or opened_at >= closed_at
            or (prior_close_time is not None and opened_at != prior_close_time)
        ):
            raise ValueError("bars must be continuous timezone-aware completed candles")
        opened = decimal(bar.get("open"))
        high = decimal(bar.get("high"))
        low = decimal(bar.get("low"))
        close = decimal(bar.get("close"))
        volume = decimal(bar.get("volume"))
        if (
            min(opened, high, low, close) <= 0
            or high < max(opened, close)
            or low > min(opened, close)
            or volume < 0
        ):
            raise ValueError("bar OHLCV envelope is invalid")
        prior_close_time = closed_at


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _entry_features(
    history: list[dict], configuration: StrategyConfiguration,
) -> tuple[bool, dict]:
    required = max(
        configuration.breakout_lookback,
        configuration.volatility_lookback + 1,
        configuration.volume_lookback,
    )
    if len(history) < required + 1:
        return False, {"history_ready": False}
    current = history[-1]
    prior = history[:-1]
    prior_breakout = prior[-configuration.breakout_lookback:]
    prior_volatility = prior[-configuration.volatility_lookback:]
    prior_volume = prior[-configuration.volume_lookback:]
    current_close = decimal(current["close"])
    previous_close = decimal(prior[-1]["close"])
    current_range = (decimal(current["high"]) - decimal(current["low"])) / previous_close
    prior_ranges = [
        (decimal(bar["high"]) - decimal(bar["low"]))
        / decimal(prior[index - 1]["close"])
        for index, bar in enumerate(prior_volatility, start=len(prior) - len(prior_volatility))
    ]
    baseline_range = _mean(prior_ranges)
    prior_mean_volume = _mean([decimal(bar["volume"]) for bar in prior_volume])
    mean_quote_volume = _mean([
        decimal(bar["close"]) * decimal(bar["volume"]) for bar in prior_volume
    ])
    prior_high = max(decimal(bar["high"]) for bar in prior_breakout)
    breakout_level = prior_high * (
        Decimal(1) + decimal(configuration.breakout_buffer_bps) / Decimal(10000)
    )
    checks = {
        "positive_candle": current_close > decimal(current["open"]),
        "price_breakout": current_close > breakout_level,
        "range_expansion": (
            current_range >= decimal(configuration.minimum_range_fraction)
            and current_range >= baseline_range * decimal(configuration.range_expansion_multiplier)
        ),
        "volume_confirmation": (
            decimal(current["volume"])
            >= prior_mean_volume * decimal(configuration.volume_multiplier)
        ),
        "liquidity_confirmation": (
            mean_quote_volume >= decimal(configuration.minimum_mean_quote_volume)
        ),
    }
    return all(checks.values()), {
        "history_ready": True,
        "current_close": str(current_close),
        "breakout_level": str(breakout_level),
        "current_range_fraction": str(current_range),
        "prior_mean_range_fraction": str(baseline_range),
        "current_volume": str(decimal(current["volume"])),
        "prior_mean_volume": str(prior_mean_volume),
        "prior_mean_quote_volume": str(mean_quote_volume),
        **checks,
    }


def _exit_signal(
    history: list[dict], *, entry_price: Decimal, held_completed_bars: int,
    configuration: StrategyConfiguration,
) -> tuple[bool, str | None, dict]:
    close = decimal(history[-1]["close"])
    stop_level = entry_price * (Decimal(1) - decimal(configuration.stop_loss_fraction))
    details = {"close": str(close), "close_stop_level": str(stop_level)}
    if close <= stop_level:
        return True, "CLOSE_STOP", details
    if held_completed_bars >= configuration.maximum_hold_bars:
        return True, "MAXIMUM_HOLD", details
    if (
        held_completed_bars >= configuration.minimum_hold_bars
        and len(history) > configuration.exit_lookback
    ):
        prior_lows = [
            decimal(bar["low"])
            for bar in history[-configuration.exit_lookback - 1:-1]
        ]
        exit_level = min(prior_lows)
        details["trailing_exit_level"] = str(exit_level)
        if close < exit_level:
            return True, "LOWER_CHANNEL_CLOSE", details
    return False, None, details


def simulate(
    bars: list[dict], *, candidate: str,
    costs: ExecutionCosts = ExecutionCosts(),
    strategy: StrategyConfiguration = StrategyConfiguration(),
    trace: bool = False,
) -> dict:
    """Simulate one isolated partition with close decision / next-open fill."""
    if candidate not in CANDIDATES:
        raise ValueError("unknown volatility-expansion candidate")
    costs.validate()
    strategy.validate()
    validate_bars(bars)
    with localcontext() as context:
        context.prec = 50
        cash = initial = peak = decimal(costs.initial_cash)
        quantity = fees = spread_cost = slippage_cost = maximum_drawdown = Decimal(0)
        fee_rate = decimal(costs.fee_bps) / Decimal(10000)
        half_spread_rate = decimal(costs.spread_bps) / Decimal(20000)
        slippage_rate = decimal(costs.slippage_bps) / Decimal(10000)
        adverse_rate = half_spread_rate + slippage_rate
        capital_fraction = decimal(costs.capital_fraction)
        pending = "BUY" if candidate == "BTC_HOLD" else "HOLD"
        pending_reason = "PRECOMMITTED_PARTITION_START" if candidate == "BTC_HOLD" else None
        pending_signal_at = None
        entry_price = None
        held_completed_bars = 0
        history: list[dict] = []
        fills: list[dict] = []
        fill_count = closed_cycles = entry_signals = exit_signals = 0
        entry_filter_checks = entry_filter_passes = 0
        exit_reasons: dict[str, int] = {}
        evidence = sha256()

        def mark(reference: Decimal) -> None:
            nonlocal peak, maximum_drawdown
            liquidation = cash + quantity * reference * (1 - adverse_rate) * (1 - fee_rate)
            peak = max(peak, liquidation)
            maximum_drawdown = max(maximum_drawdown, (peak - liquidation) / peak)

        def execute(action: str, reference: Decimal, at: datetime) -> str | None:
            nonlocal cash, quantity, fees, spread_cost, slippage_cost
            nonlocal closed_cycles, fill_count, held_completed_bars, entry_price
            if action == "BUY" and quantity == 0:
                execution_price = reference * (1 + adverse_rate)
                budget = cash * capital_fraction
                bought = (budget / (execution_price * (1 + fee_rate))).quantize(
                    Decimal("1e-18"), rounding=ROUND_DOWN,
                )
                if bought == 0:
                    return None
                value = bought * execution_price
                charge = value * fee_rate
                cash -= value + charge
                quantity = bought
                traded_quantity = bought
                entry_price = execution_price
                held_completed_bars = 0
            elif action == "SELL" and quantity > 0:
                execution_price = reference * (1 - adverse_rate)
                traded_quantity = quantity
                value = quantity * execution_price
                charge = value * fee_rate
                cash += value - charge
                quantity = Decimal(0)
                closed_cycles += 1
                held_completed_bars = 0
                entry_price = None
            else:
                return None
            if cash < 0 or quantity < 0:
                raise ArithmeticError("long-only capital conservation failed")
            fees += charge
            spread_cost += traded_quantity * reference * half_spread_rate
            slippage_cost += traded_quantity * reference * slippage_rate
            fill_count += 1
            row = {
                "action": action,
                "at": at.isoformat(),
                "signal_at": pending_signal_at,
                "reason": pending_reason,
                "reference_price": str(reference),
                "execution_price": str(execution_price),
                "fee": str(charge),
                "cash": str(cash),
                "btc_quantity": str(quantity),
            }
            evidence.update(json.dumps(
                row, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"))
            if trace:
                fills.append(row)
            return action

        for index, bar in enumerate(bars):
            action = execute(pending, decimal(bar["open"]), bar["opened_at"])
            if action == "BUY":
                entry_signals += 1
            elif action == "SELL":
                exit_signals += 1
                if pending_reason is not None:
                    exit_reasons[pending_reason] = exit_reasons.get(pending_reason, 0) + 1
            pending = "HOLD"
            pending_reason = None
            pending_signal_at = None
            mark(decimal(bar["open"]))
            history.append(bar)
            close = decimal(bar["close"])
            if quantity > 0:
                held_completed_bars += 1
            mark(close)

            if candidate != "VOLATILITY_EXPANSION_BREAKOUT":
                continue
            if quantity == 0:
                should_enter, _ = _entry_features(history, strategy)
                if len(history) > max(
                    strategy.breakout_lookback,
                    strategy.volatility_lookback + 1,
                    strategy.volume_lookback,
                ):
                    entry_filter_checks += 1
                if should_enter:
                    pending = "BUY"
                    pending_reason = "VOLATILITY_VOLUME_BREAKOUT"
                    pending_signal_at = bar["closed_at"].isoformat()
                    entry_filter_passes += 1
            else:
                should_exit, reason, _ = _exit_signal(
                    history, entry_price=entry_price,
                    held_completed_bars=held_completed_bars,
                    configuration=strategy,
                )
                if should_exit:
                    pending = "SELL"
                    pending_reason = reason
                    pending_signal_at = bar["closed_at"].isoformat()

        # This is a precommitted evidence-partition boundary action, not an
        # intrabar stop/exit decision.  It does not inspect a future partition.
        if quantity > 0:
            pending = "SELL"
            pending_reason = "PARTITION_END_LIQUIDATION"
            pending_signal_at = "PRECOMMITTED_PARTITION_END"
            execute("SELL", decimal(bars[-1]["close"]), bars[-1]["closed_at"])
            exit_reasons["PARTITION_END_LIQUIDATION"] = (
                exit_reasons.get("PARTITION_END_LIQUIDATION", 0) + 1
            )
        mark(decimal(bars[-1]["close"]))
        result = {
            "candidate": candidate,
            "observations": len(bars),
            "initial_cash": str(initial),
            "final_cash": str(cash),
            "net_pnl": str(cash - initial),
            "return_fraction": str((cash - initial) / initial),
            "max_drawdown_fraction": str(maximum_drawdown),
            "fees": str(fees),
            "modeled_spread_cost": str(spread_cost),
            "modeled_slippage_cost": str(slippage_cost),
            "fills": fill_count,
            "closed_cycles": closed_cycles,
            "entry_signals": entry_signals,
            "exit_signals": exit_signals,
            "entry_filter_checks": entry_filter_checks,
            "entry_filter_passes": entry_filter_passes,
            "exit_reasons": dict(sorted(exit_reasons.items())),
            "execution_digest": evidence.hexdigest(),
            "first_open": bars[0]["opened_at"].isoformat(),
            "last_close": bars[-1]["closed_at"].isoformat(),
            "position_at_end": "CASH",
            "network_orders": False,
            "paid_model_calls": 0,
        }
        if trace:
            result["trace"] = fills
        return result


def _gate_reasons(
    candidate: dict, cash: dict, hold: dict, gate: GateConfiguration,
) -> list[str]:
    reasons = []
    if candidate["closed_cycles"] < gate.minimum_closed_cycles:
        reasons.append("MINIMUM_CLOSED_CYCLES_NOT_MET")
    if Decimal(candidate["net_pnl"]) <= Decimal(cash["net_pnl"]):
        reasons.append("DID_NOT_BEAT_CASH_AFTER_COSTS")
    if gate.require_beats_hold and Decimal(candidate["net_pnl"]) <= Decimal(hold["net_pnl"]):
        reasons.append("DID_NOT_BEAT_HOLD_AFTER_COSTS")
    if Decimal(candidate["max_drawdown_fraction"]) > decimal(gate.maximum_drawdown_fraction):
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    return reasons


class CheckpointStore:
    """Content-authenticated completed-stage cache; partial stages are ignored."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def __call__(self, key: str, producer) -> dict:
        if type(key) is not str or not key or len(key) > 500:
            raise ValueError("invalid checkpoint key")
        target = self.root / f"{sha256(key.encode()).hexdigest()}.json"
        if target.exists():
            envelope = json.loads(target.read_text(encoding="utf-8"))
            if (
                type(envelope) is not dict
                or set(envelope) != {"schema", "key", "value", "value_sha256"}
                or envelope["schema"] != VERSION
                or envelope["key"] != key
                or type(envelope["value"]) is not dict
                or envelope["value_sha256"] != digest(envelope["value"])
            ):
                raise ValueError("checkpoint authentication failed")
            return envelope["value"]
        value = producer()
        if type(value) is not dict:
            raise ValueError("checkpoint producer must return an object")
        envelope = {
            "schema": VERSION,
            "key": key,
            "value": value,
            "value_sha256": digest(value),
        }
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        temporary = target.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return value


def screen(
    bars: list[dict], *, train_count: int, development_count: int,
    dataset_sha256: str, costs: ExecutionCosts = ExecutionCosts(),
    strategy: StrategyConfiguration = StrategyConfiguration(),
    gate: GateConfiguration = GateConfiguration(), checkpoint=None,
) -> dict:
    """Run TRAIN, then DEVELOPMENT, then FINAL only after each prior gate."""
    costs.validate()
    strategy.validate()
    gate.validate()
    if type(bars) is not list:
        raise ValueError("bars must be a list")
    if type(train_count) is not int or type(development_count) is not int:
        raise ValueError("partition counts must be integers")
    if train_count < 4 or development_count < 4 or train_count + development_count > len(bars) - 4:
        raise ValueError("TRAIN, DEVELOPMENT and FINAL must each contain at least four bars")
    if (
        type(dataset_sha256) is not str
        or len(dataset_sha256) != 64
        or any(character not in "0123456789abcdef" for character in dataset_sha256)
    ):
        raise ValueError("dataset identity must be lowercase SHA-256")
    config = {
        "version": VERSION,
        "dataset_sha256": dataset_sha256,
        "observations": len(bars),
        "train_count": train_count,
        "development_count": development_count,
        "final_count": len(bars) - train_count - development_count,
        "costs": asdict(costs),
        "strategy": asdict(strategy),
        "gate": asdict(gate),
        "candidates": CANDIDATES,
        "market_semantics": "WHITEBIT_COMPATIBLE_BTC_USDT_SPOT_LONG_ONLY",
        "signal_policy": "COMPLETED_1H_CANDLE_CLOSE",
        "fill_policy": "FOLLOWING_OPEN_WITH_ADVERSE_COSTS",
        "stop_policy": "CLOSE_ONLY_SIGNAL_FOLLOWING_OPEN_FILL",
        "end_policy": "PRECOMMITTED_PARTITION_END_LIQUIDATION",
        "evaluation_policy": "TRAIN_THEN_DEVELOPMENT_THEN_FINAL",
    }
    config_sha256 = digest(config)
    run = checkpoint or (lambda key, producer: producer())

    def evaluate_partition(label: str, partition: list[dict]) -> list[dict]:
        return [
            run(
                f"{config_sha256}-{label}-{candidate}",
                lambda candidate=candidate: simulate(
                    partition, candidate=candidate, costs=costs, strategy=strategy,
                ),
            )
            for candidate in CANDIDATES
        ]

    training = evaluate_partition("train", bars[:train_count])
    training_by_name = {row["candidate"]: row for row in training}
    training_reasons = _gate_reasons(
        training_by_name["VOLATILITY_EXPANSION_BREAKOUT"],
        training_by_name["CASH"], training_by_name["BTC_HOLD"], gate,
    )
    development: list[dict] = []
    development_reasons: list[str] = []
    final: list[dict] = []
    final_reasons: list[str] = []
    phase = "REJECTED_TRAIN"
    if not training_reasons:
        development = evaluate_partition(
            "development", bars[train_count:train_count + development_count],
        )
        development_by_name = {row["candidate"]: row for row in development}
        development_reasons = _gate_reasons(
            development_by_name["VOLATILITY_EXPANSION_BREAKOUT"],
            development_by_name["CASH"], development_by_name["BTC_HOLD"], gate,
        )
        phase = "REJECTED_DEVELOPMENT"
        if not development_reasons:
            final = evaluate_partition(
                "final", bars[train_count + development_count:],
            )
            final_by_name = {row["candidate"]: row for row in final}
            final_reasons = _gate_reasons(
                final_by_name["VOLATILITY_EXPANSION_BREAKOUT"],
                final_by_name["CASH"], final_by_name["BTC_HOLD"], gate,
            )
            phase = "FINAL_PASS" if not final_reasons else "FINAL_FAIL"
    result = {
        "schema": VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "config": config,
        "config_sha256": config_sha256,
        "phase": phase,
        "training": training,
        "training_rejection_reasons": training_reasons,
        "development": development,
        "development_rejection_reasons": development_reasons,
        "final": final,
        "final_rejection_reasons": final_reasons,
        "development_consumed": bool(development),
        "final_consumed": bool(final),
        "selected_candidate": (
            "VOLATILITY_EXPANSION_BREAKOUT" if phase == "FINAL_PASS" else "CASH"
        ),
        "limitations": [
            "NO_INTRABAR_STOP_OR_DRAWDOWN",
            "NO_CAPACITY_OR_QUEUE_MODEL",
            "NO_EXCHANGE_LOT_RULES",
            "FIXED_SLIPPAGE_DURING_VOLATILITY_EXPANSION",
            "RETROSPECTIVE_BAR_AVAILABILITY",
            "NO_DEPLOYMENT_OR_PROFIT_CLAIM",
        ],
        "network_orders": False,
        "paid_model_calls": 0,
    }
    result["result_sha256"] = digest(result)
    return result
