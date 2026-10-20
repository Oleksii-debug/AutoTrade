"""Deterministic BTC/ETH relative-strength screen for ZERO research only.

The strategy is deliberately narrow: it may hold BTC or cash, never ETH,
never borrows, and never submits a network order.  Signals use completed,
aligned hourly bars and are filled at the following BTC open with explicit
adverse costs.  TRAIN and DEVELOPMENT must pass before FINAL is touched.
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


VERSION = "cross-market-relative-strength-v1"
EVIDENCE_CLASS = "ZERO_RESEARCH_ONLY_RETROSPECTIVE_SIMULATION"
CANDIDATES = ("CASH", "BTC_HOLD", "BTC_MOMENTUM", "CROSS_RELATIVE_STRENGTH")


def digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


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
    ratio_lookback: int = 48
    volatility_lookback: int = 24
    volatility_cap_fraction: str = "0.03"
    momentum_lookback: int = 24
    min_hold_bars: int = 3
    max_hold_bars: int = 24

    def validate(self) -> None:
        for name in ("ratio_lookback", "volatility_lookback", "momentum_lookback"):
            value = getattr(self, name)
            if type(value) is not int or not 2 <= value <= 1000:
                raise ValueError(f"{name} must be an integer in [2,1000]")
        if type(self.min_hold_bars) is not int or not 3 <= self.min_hold_bars <= 48:
            raise ValueError("minimum hold must be in [3,48]")
        if (
            type(self.max_hold_bars) is not int
            or not self.min_hold_bars <= self.max_hold_bars <= 48
        ):
            raise ValueError("maximum hold must be between minimum hold and 48")
        if not Decimal(0) <= decimal(self.volatility_cap_fraction) <= Decimal(1):
            raise ValueError("volatility cap must be in [0,1]")


@dataclass(frozen=True)
class GateConfiguration:
    minimum_closed_cycles: int = 8
    maximum_drawdown_fraction: str = "0.20"

    def validate(self) -> None:
        if type(self.minimum_closed_cycles) is not int or not 1 <= self.minimum_closed_cycles <= 100000:
            raise ValueError("minimum closed cycles must be in [1,100000]")
        if not Decimal(0) <= decimal(self.maximum_drawdown_fraction) <= Decimal(1):
            raise ValueError("maximum drawdown must be in [0,1]")


def validate_aligned_bars(btc_bars: list[dict], eth_bars: list[dict]) -> None:
    if len(btc_bars) != len(eth_bars) or len(btc_bars) < 4:
        raise ValueError("BTC and ETH require equal aligned cuts of at least four bars")
    prior_close = None
    for btc, eth in zip(btc_bars, eth_bars):
        for bar in (btc, eth):
            opened_at, closed_at = bar.get("opened_at"), bar.get("closed_at")
            if (
                type(opened_at) is not datetime
                or type(closed_at) is not datetime
                or opened_at.utcoffset() is None
                or closed_at.utcoffset() is None
                or opened_at >= closed_at
            ):
                raise ValueError("bars require timezone-aware open and close times")
            opened, closed = decimal(bar.get("open")), decimal(bar.get("close"))
            if opened <= 0 or closed <= 0:
                raise ValueError("bar prices must be positive")
        if btc["opened_at"] != eth["opened_at"] or btc["closed_at"] != eth["closed_at"]:
            raise ValueError("BTC and ETH bars are not timestamp aligned")
        if prior_close is not None and btc["opened_at"] != prior_close:
            raise ValueError("aligned bars must be continuous")
        prior_close = btc["closed_at"]


def _realized_volatility(closes: list[Decimal], lookback: int) -> Decimal | None:
    if len(closes) < lookback + 1:
        return None
    selected = closes[-(lookback + 1):]
    returns = [current / previous - 1 for previous, current in zip(selected, selected[1:])]
    return (sum((item * item for item in returns), Decimal(0)) / Decimal(lookback)).sqrt()


def _signal(
    candidate: str,
    *,
    btc_closes: list[Decimal],
    ratios: list[Decimal],
    in_position: bool,
    held_completed_bars: int,
    strategy: StrategyConfiguration,
) -> tuple[str, dict]:
    if candidate == "CASH":
        return ("SELL" if in_position else "HOLD"), {}
    if candidate == "BTC_HOLD":
        return ("HOLD" if in_position else "BUY"), {}
    if candidate == "BTC_MOMENTUM":
        if len(btc_closes) <= strategy.momentum_lookback:
            return "HOLD", {"history_ready": False}
        positive = btc_closes[-1] > btc_closes[-strategy.momentum_lookback - 1]
        return (
            "BUY" if positive and not in_position else
            "SELL" if not positive and in_position else "HOLD"
        ), {"history_ready": True, "positive": positive}

    history_ready = (
        len(ratios) > strategy.ratio_lookback
        and len(btc_closes) > strategy.volatility_lookback
    )
    if not history_ready:
        return "HOLD", {"history_ready": False}
    prior_ratios = ratios[-strategy.ratio_lookback - 1:-1]
    breakout = ratios[-1] > max(prior_ratios)
    volatility = _realized_volatility(btc_closes, strategy.volatility_lookback)
    below_cap = volatility is not None and volatility <= decimal(strategy.volatility_cap_fraction)
    details = {
        "history_ready": True,
        "ratio": str(ratios[-1]),
        "prior_ratio_max": str(max(prior_ratios)),
        "prior_ratio_mean": str(sum(prior_ratios, Decimal(0)) / Decimal(len(prior_ratios))),
        "realized_volatility_fraction": str(volatility),
        "breakout": breakout,
        "below_volatility_cap": below_cap,
    }
    if not in_position:
        return ("BUY" if breakout and below_cap else "HOLD"), details
    causal_exit = ratios[-1] <= sum(prior_ratios, Decimal(0)) / Decimal(len(prior_ratios))
    if held_completed_bars >= strategy.max_hold_bars:
        details["exit_reason"] = "MAXIMUM_HOLD"
        return "SELL", details
    if held_completed_bars >= strategy.min_hold_bars and causal_exit:
        details["exit_reason"] = "RATIO_BELOW_TRAILING_MEAN"
        return "SELL", details
    return "HOLD", details


def simulate(
    btc_bars: list[dict],
    eth_bars: list[dict],
    *,
    candidate: str,
    costs: ExecutionCosts = ExecutionCosts(),
    strategy: StrategyConfiguration = StrategyConfiguration(),
    trace: bool = False,
) -> dict:
    """Simulate one fresh-cash partition with close signal / next-open fill."""
    if candidate not in CANDIDATES:
        raise ValueError("unknown cross-market candidate")
    costs.validate()
    strategy.validate()
    validate_aligned_bars(btc_bars, eth_bars)
    with localcontext() as context:
        context.prec = 50
        cash = initial = peak = decimal(costs.initial_cash)
        quantity = fees = maximum_drawdown = Decimal(0)
        fee_rate = decimal(costs.fee_bps) / Decimal(10000)
        adverse_rate = (
            decimal(costs.spread_bps) / Decimal(2) + decimal(costs.slippage_bps)
        ) / Decimal(10000)
        capital_fraction = decimal(costs.capital_fraction)
        pending = "HOLD"
        pending_signal_at = None
        btc_closes: list[Decimal] = []
        ratios: list[Decimal] = []
        held_completed_bars = 0
        fills: list[dict] = []
        closed_cycles = entry_signals = 0
        evidence = sha256()

        def mark(reference: Decimal) -> None:
            nonlocal peak, maximum_drawdown
            liquidation = cash + quantity * reference * (1 - adverse_rate) * (1 - fee_rate)
            peak = max(peak, liquidation)
            maximum_drawdown = max(maximum_drawdown, (peak - liquidation) / peak)

        def execute(action: str, reference: Decimal, at: datetime, signal_at: str | None) -> None:
            nonlocal cash, quantity, fees, closed_cycles, held_completed_bars
            if action == "BUY" and quantity == 0:
                execution_price = reference * (1 + adverse_rate)
                budget = cash * capital_fraction
                bought = (budget / (execution_price * (1 + fee_rate))).quantize(
                    Decimal("1e-18"), rounding=ROUND_DOWN,
                )
                if bought == 0:
                    return
                value = bought * execution_price
                charge = value * fee_rate
                cash -= value + charge
                quantity = bought
                held_completed_bars = 0
            elif action == "SELL" and quantity > 0:
                execution_price = reference * (1 - adverse_rate)
                value = quantity * execution_price
                charge = value * fee_rate
                cash += value - charge
                quantity = Decimal(0)
                closed_cycles += 1
                held_completed_bars = 0
            else:
                return
            if cash < 0 or quantity < 0:
                raise ArithmeticError("long-only capital conservation failed")
            fees += charge
            row = {
                "action": action,
                "at": at.isoformat(),
                "signal_at": signal_at,
                "btc_price": str(execution_price),
                "fee": str(charge),
                "cash": str(cash),
                "btc_quantity": str(quantity),
            }
            evidence.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
            if trace:
                fills.append(row)

        last_details = {}
        for btc, eth in zip(btc_bars, eth_bars):
            execute(pending, decimal(btc["open"]), btc["opened_at"], pending_signal_at)
            mark(decimal(btc["open"]))
            btc_close = decimal(btc["close"])
            eth_close = decimal(eth["close"])
            btc_closes.append(btc_close)
            ratios.append(btc_close / eth_close)
            if quantity > 0:
                held_completed_bars += 1
            mark(btc_close)
            pending, last_details = _signal(
                candidate,
                btc_closes=btc_closes,
                ratios=ratios,
                in_position=quantity > 0,
                held_completed_bars=held_completed_bars,
                strategy=strategy,
            )
            pending_signal_at = btc["closed_at"].isoformat()
            if pending == "BUY" and quantity == 0:
                entry_signals += 1

        execute(
            "SELL", decimal(btc_bars[-1]["close"]), btc_bars[-1]["closed_at"],
            "PRECOMMITTED_PARTITION_END",
        )
        mark(decimal(btc_bars[-1]["close"]))
        result = {
            "candidate": candidate,
            "observations": len(btc_bars),
            "initial_cash": str(initial),
            "final_cash": str(cash),
            "net_pnl": str(cash - initial),
            "return_fraction": str((cash - initial) / initial),
            "max_drawdown_fraction": str(maximum_drawdown),
            "fees": str(fees),
            "fills": len(fills) if trace else closed_cycles * 2 + (1 if quantity > 0 else 0),
            "closed_cycles": closed_cycles,
            "entry_signals": entry_signals,
            "execution_digest": evidence.hexdigest(),
            "first_open": btc_bars[0]["opened_at"].isoformat(),
            "last_close": btc_bars[-1]["closed_at"].isoformat(),
            "last_signal_details": last_details,
            "single_leg": "BTC_LONG_OR_CASH",
            "network_orders": False,
        }
        if trace:
            result["trace"] = fills
            result["fills"] = len(fills)
        return result


def _gate_reasons(result: dict, cash_result: dict, gate: GateConfiguration) -> list[str]:
    reasons = []
    if result["closed_cycles"] < gate.minimum_closed_cycles:
        reasons.append("MINIMUM_CLOSED_CYCLES_NOT_MET")
    if Decimal(result["net_pnl"]) <= Decimal(cash_result["net_pnl"]):
        reasons.append("DID_NOT_BEAT_CASH_AFTER_COSTS")
    if Decimal(result["max_drawdown_fraction"]) > decimal(gate.maximum_drawdown_fraction):
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    return reasons


class CheckpointStore:
    """Content-authenticated completed-stage cache; partial stages are discarded."""

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
                or envelope["schema"] != "cross-market-stage-v1"
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
            "schema": "cross-market-stage-v1",
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
    btc_bars: list[dict],
    eth_bars: list[dict],
    *,
    train_count: int,
    development_count: int,
    dataset_sha256: str,
    costs: ExecutionCosts = ExecutionCosts(),
    strategy: StrategyConfiguration = StrategyConfiguration(),
    gate: GateConfiguration = GateConfiguration(),
    checkpoint=None,
) -> dict:
    """Run TRAIN -> DEVELOPMENT -> one FINAL check with strict early rejection."""
    validate_aligned_bars(btc_bars, eth_bars)
    costs.validate()
    strategy.validate()
    gate.validate()
    if type(train_count) is not int or type(development_count) is not int:
        raise ValueError("partition counts must be integers")
    if train_count < 4 or development_count < 4 or train_count + development_count > len(btc_bars) - 4:
        raise ValueError("TRAIN, DEVELOPMENT and FINAL must each contain at least four bars")
    if (
        type(dataset_sha256) is not str
        or len(dataset_sha256) != 64
        or any(character not in "0123456789abcdef" for character in dataset_sha256)
    ):
        raise ValueError("dataset identity must be lowercase SHA-256")
    consumed = [
        [
            btc["opened_at"].isoformat(), btc["closed_at"].isoformat(),
            str(decimal(btc["open"])), str(decimal(btc["close"])),
            str(decimal(eth["open"])), str(decimal(eth["close"])),
        ]
        for btc, eth in zip(btc_bars, eth_bars)
    ]
    config = {
        "version": VERSION,
        "dataset_sha256": dataset_sha256,
        "consumed_bars_sha256": digest(consumed),
        "observations": len(btc_bars),
        "train_count": train_count,
        "development_count": development_count,
        "final_count": len(btc_bars) - train_count - development_count,
        "costs": asdict(costs),
        "strategy": asdict(strategy),
        "gate": asdict(gate),
        "candidates": CANDIDATES,
        "signal_policy": "COMPLETED_ALIGNED_BAR_CLOSE",
        "fill_policy": "FOLLOWING_BTC_OPEN_ADVERSE_COSTS",
        "end_policy": "PRECOMMITTED_FORCED_CLOSE_EACH_PARTITION",
    }
    config_sha256 = digest(config)
    run = checkpoint or (lambda key, producer: producer())

    def evaluate_partition(label: str, start: int, stop: int) -> list[dict]:
        return [
            run(
                f"{config_sha256}-{label}-{candidate}",
                lambda candidate=candidate: simulate(
                    btc_bars[start:stop], eth_bars[start:stop], candidate=candidate,
                    costs=costs, strategy=strategy,
                ),
            )
            for candidate in CANDIDATES
        ]

    training = evaluate_partition("train", 0, train_count)
    training_by_name = {row["candidate"]: row for row in training}
    training_reasons = _gate_reasons(
        training_by_name["CROSS_RELATIVE_STRENGTH"], training_by_name["CASH"], gate,
    )
    development: list[dict] = []
    development_reasons: list[str] = []
    final: list[dict] = []
    final_reasons: list[str] = []
    phase = "REJECTED_TRAIN"
    if not training_reasons:
        development = evaluate_partition(
            "development", train_count, train_count + development_count,
        )
        development_by_name = {row["candidate"]: row for row in development}
        development_reasons = _gate_reasons(
            development_by_name["CROSS_RELATIVE_STRENGTH"],
            development_by_name["CASH"], gate,
        )
        phase = "REJECTED_DEVELOPMENT"
        if not development_reasons:
            final = evaluate_partition(
                "final", train_count + development_count, len(btc_bars),
            )
            final_by_name = {row["candidate"]: row for row in final}
            final_reasons = _gate_reasons(
                final_by_name["CROSS_RELATIVE_STRENGTH"], final_by_name["CASH"], gate,
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
            "CROSS_RELATIVE_STRENGTH" if phase == "FINAL_PASS" else "CASH"
        ),
        "network_orders": False,
        "paid_model_calls": 0,
    }
    result["result_sha256"] = digest(result)
    return result
