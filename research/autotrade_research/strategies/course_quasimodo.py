"""Frozen causal approximation of the private course's Quasimodo setup.

The stable family combines higher-timeframe trend context, an active fair-value
gap proxy, premium/discount, a liquidity sweep, an aggressive reaction and a
later structure shift.  It risks a fixed equity fraction, requires a minimum
reward/risk ratio and uses precommitted stops/targets.  No network or order API
is reachable from this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
import json

from .fast_screen import decimal, validate_bars
from .technical_confluence import TechnicalCosts


VERSION = "private-course-quasimodo-fvg-v1-frozen-100-cycle"


def _bounded_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("1e-18")), "f")


@dataclass(frozen=True)
class CourseConfiguration:
    context_lookback: int = 384
    context_fast_ema: int = 64
    context_slow_ema: int = 192
    poi_lookback: int = 96
    sweep_lookback: int = 24
    shift_lookback: int = 12
    shift_expiry_bars: int = 16
    atr_period: int = 14
    minimum_reaction_atr: str = "1.0"
    minimum_reward_risk: str = "2.0"
    minimum_reward_fraction: str = "0.003"
    stop_buffer_atr: str = "0.10"
    risk_fraction: str = "0.01"
    maximum_notional_multiple: str = "2"
    maximum_hold_bars: int = 96
    cooldown_bars: int = 16

    def validate(self) -> None:
        integer_bounds = {
            "context_lookback": (64, 5000), "context_fast_ema": (4, 1000),
            "context_slow_ema": (8, 2000), "poi_lookback": (3, 2000),
            "sweep_lookback": (3, 1000), "shift_lookback": (2, 500),
            "shift_expiry_bars": (1, 500), "atr_period": (2, 200),
            "maximum_hold_bars": (1, 10000), "cooldown_bars": (0, 10000),
        }
        for name, (low, high) in integer_bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} is outside its bounded range")
        if not self.context_fast_ema < self.context_slow_ema <= self.context_lookback:
            raise ValueError("context EMA periods must be ordered inside lookback")
        if max(self.poi_lookback, self.sweep_lookback,
               self.shift_lookback, self.atr_period + 1) >= self.context_lookback:
            raise ValueError("local lookbacks must be smaller than context lookback")
        for name, low, high in (
                ("minimum_reaction_atr", "0.1", "10"),
                ("minimum_reward_risk", "1", "20"),
                ("minimum_reward_fraction", "0", "0.25"),
                ("stop_buffer_atr", "0", "5"),
                ("risk_fraction", "0.0001", "0.05"),
                ("maximum_notional_multiple", "0.1", "10")):
            value = decimal(getattr(self, name))
            if not Decimal(low) <= value <= Decimal(high):
                raise ValueError(f"{name} is outside its bounded range")


def _ema(values: list[Decimal], period: int) -> Decimal:
    alpha = Decimal(2) / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (Decimal(1) - alpha) * result
    return result


def _atr(bars: list[dict], period: int) -> Decimal:
    total = Decimal(0)
    for index in range(len(bars) - period, len(bars)):
        previous = decimal(bars[index - 1]["close"])
        high, low = decimal(bars[index]["high"]), decimal(bars[index]["low"])
        total += max(high - low, abs(high - previous), abs(low - previous))
    return total / Decimal(period)


def _active_fvg(history: list[dict], *, direction: int,
                lookback: int, current_low: Decimal,
                current_high: Decimal, current_close: Decimal) -> tuple[Decimal, Decimal] | None:
    start = max(2, len(history) - lookback)
    for index in range(len(history) - 2, start - 1, -1):
        older_high = decimal(history[index - 2]["high"])
        older_low = decimal(history[index - 2]["low"])
        newer_high = decimal(history[index]["high"])
        newer_low = decimal(history[index]["low"])
        if direction > 0 and newer_low > older_high:
            lower, upper = older_high, newer_low
            if current_low <= upper and current_close >= lower:
                return lower, upper
        if direction < 0 and newer_high < older_low:
            lower, upper = newer_high, older_low
            if current_high >= lower and current_close <= upper:
                return lower, upper
    return None


def detect_sweep(history: list[dict], config: CourseConfiguration) -> dict | None:
    """Detect one course-compatible sweep at the latest completed bar."""
    config.validate()
    if len(history) < config.context_lookback:
        return None
    current = history[-1]
    prior = history[-1-config.sweep_lookback:-1]
    context = history[-config.context_lookback:-1]
    closes = [decimal(row["close"]) for row in context]
    close, opened = decimal(current["close"]), decimal(current["open"])
    high, low = decimal(current["high"]), decimal(current["low"])
    context_high = max(decimal(row["high"]) for row in context)
    context_low = min(decimal(row["low"]) for row in context)
    midpoint = (context_high + context_low) / Decimal(2)
    fast = _ema(closes[-config.context_fast_ema * 3:], config.context_fast_ema)
    slow = _ema(closes[-config.context_slow_ema * 3:], config.context_slow_ema)
    atr = _atr(history, config.atr_period)
    prior_low = min(decimal(row["low"]) for row in prior)
    prior_high = max(decimal(row["high"]) for row in prior)
    reaction = high - low
    direction = 0
    if (fast > slow and close <= midpoint and low < prior_low and close > prior_low
            and close > opened and reaction >= atr * decimal(config.minimum_reaction_atr)):
        direction = 1
    elif (fast < slow and close >= midpoint and high > prior_high and close < prior_high
          and close < opened and reaction >= atr * decimal(config.minimum_reaction_atr)):
        direction = -1
    if not direction:
        return None
    fvg = _active_fvg(history[:-1], direction=direction, lookback=config.poi_lookback,
                      current_low=low, current_high=high, current_close=close)
    if fvg is None:
        return None
    local = history[-1-config.shift_lookback:-1]
    shift = (max(decimal(row["high"]) for row in local) if direction > 0
             else min(decimal(row["low"]) for row in local))
    return {"direction": direction, "sweep_extreme": _bounded_text(low if direction > 0 else high),
            "shift_level": _bounded_text(shift),
            "target": _bounded_text(context_high if direction > 0 else context_low),
            "atr": _bounded_text(atr), "fvg_lower": _bounded_text(fvg[0]),
            "fvg_upper": _bounded_text(fvg[1]),
            "age": 0, "detected_at": current["closed_at"].isoformat()}


def simulate(bars: list[dict], *, costs: TechnicalCosts = TechnicalCosts(),
             configuration: CourseConfiguration = CourseConfiguration()) -> dict:
    """Run the frozen course setup with causal next-open entries."""
    validate_bars(bars, require_ohlcv=True); costs.validate(); configuration.validate()
    with localcontext() as context:
        context.prec = 50
        balance = initial = peak = decimal(costs.initial_equity)
        fee_ratio = decimal(costs.fee_bps_per_fill) / Decimal(10000)
        adverse = (decimal(costs.spread_bps) / Decimal(2)
                   + decimal(costs.slippage_bps_per_fill)) / Decimal(10000)
        funding_day = decimal(costs.funding_bps_per_day) / Decimal(10000)
        risk_fraction = decimal(configuration.risk_fraction)
        maximum_notional = decimal(configuration.maximum_notional_multiple)
        quantity = entry = stop = target = Decimal(0)
        cycle_start_balance = balance
        history: list[dict] = []
        pending_setup = pending_entry = None
        cooldown = holding = closed = wins = losses = fills = signals = 0
        fees = funding = maximum_drawdown = Decimal(0)
        exits: dict[str, int] = {}
        evidence = sha256()

        def mark(price: Decimal) -> None:
            nonlocal peak, maximum_drawdown
            equity = balance + quantity * (price - entry)
            peak = max(peak, equity)
            if peak > 0:
                maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)

        def close_position(price: Decimal, reason: str, at: str) -> None:
            nonlocal balance, quantity, entry, stop, target, fees, fills
            nonlocal closed, wins, losses, holding, cooldown
            actual = price * (Decimal(1) - adverse if quantity > 0 else Decimal(1) + adverse)
            charge = abs(quantity) * actual * fee_ratio
            balance += quantity * (actual - entry) - charge
            pnl = balance - cycle_start_balance
            fees += charge; fills += 1; closed += 1
            wins += int(pnl > 0); losses += int(pnl <= 0)
            exits[reason] = exits.get(reason, 0) + 1
            evidence.update(json.dumps({"at": at, "reason": reason,
                "pnl": str(pnl), "balance": str(balance)}, sort_keys=True).encode())
            quantity = entry = stop = target = Decimal(0)
            holding = 0; cooldown = configuration.cooldown_bars

        for bar in bars:
            opened = decimal(bar["open"])
            if pending_entry is not None and quantity == 0:
                direction = pending_entry["direction"]
                actual = opened * (Decimal(1) + adverse if direction > 0 else Decimal(1) - adverse)
                candidate_stop = decimal(pending_entry["stop"])
                candidate_target = decimal(pending_entry["target"])
                risk = (actual - candidate_stop if direction > 0 else candidate_stop - actual)
                reward = (candidate_target - actual if direction > 0 else actual - candidate_target)
                if (risk > 0 and reward > 0
                        and reward / risk >= decimal(configuration.minimum_reward_risk)
                        and reward / actual >= decimal(configuration.minimum_reward_fraction)):
                    risk_budget = max(Decimal(0), balance) * risk_fraction
                    opened_quantity = min(risk_budget / risk,
                        max(Decimal(0), balance) * maximum_notional / actual)
                    opened_quantity = opened_quantity.quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                    if opened_quantity > 0:
                        cycle_start_balance = balance
                        quantity = opened_quantity if direction > 0 else -opened_quantity
                        entry, stop, target = actual, candidate_stop, candidate_target
                        charge = abs(quantity) * entry * fee_ratio
                        balance -= charge; fees += charge; fills += 1; holding = 0
                pending_entry = None
            high, low, close = decimal(bar["high"]), decimal(bar["low"]), decimal(bar["close"])
            if quantity:
                holding += 1
                seconds = Decimal(str((bar["closed_at"] - bar["opened_at"]).total_seconds()))
                carry = abs(quantity) * close * funding_day * seconds / Decimal(86400)
                balance -= carry; funding += carry
                stopped = low <= stop if quantity > 0 else high >= stop
                targeted = high >= target if quantity > 0 else low <= target
                if stopped:
                    close_position(stop, "PRECOMMITTED_STOP", bar["closed_at"].isoformat())
                elif targeted:
                    close_position(target, "OPPOSING_LIQUIDITY_TARGET", bar["closed_at"].isoformat())
                elif holding >= configuration.maximum_hold_bars:
                    close_position(close, "MAXIMUM_HOLD_EXIT", bar["closed_at"].isoformat())
            mark(close)
            history.append(bar)
            if quantity or pending_entry is not None:
                continue
            if cooldown:
                cooldown -= 1; continue
            if pending_setup is not None:
                pending_setup["age"] += 1
                direction = pending_setup["direction"]
                invalid = (low <= decimal(pending_setup["sweep_extreme"]) if direction > 0
                           else high >= decimal(pending_setup["sweep_extreme"]))
                shifted = (close > decimal(pending_setup["shift_level"]) if direction > 0
                           else close < decimal(pending_setup["shift_level"]))
                if invalid or pending_setup["age"] > configuration.shift_expiry_bars:
                    pending_setup = None
                elif shifted:
                    atr = decimal(pending_setup["atr"])
                    extreme = decimal(pending_setup["sweep_extreme"])
                    buffer = atr * decimal(configuration.stop_buffer_atr)
                    pending_entry = {"direction": direction,
                        "stop": _bounded_text(extreme - buffer if direction > 0 else extreme + buffer),
                        "target": pending_setup["target"]}
                    pending_setup = None; signals += 1
                continue
            pending_setup = detect_sweep(history, configuration)
        if quantity:
            close_position(decimal(bars[-1]["close"]), "PARTITION_END_EXIT",
                           bars[-1]["closed_at"].isoformat())
        return {"schema": VERSION, "configuration": asdict(configuration),
            "costs": asdict(costs), "round_trip_cost_bps": str(costs.round_trip_bps),
            "initial_equity": str(initial), "final_equity": str(balance),
            "net_pnl": str(balance - initial), "return_fraction": str((balance - initial) / initial),
            "maximum_drawdown_fraction": str(maximum_drawdown),
            "closed_cycles": closed, "wins": wins, "losses": losses,
            "win_rate": str(Decimal(wins) / Decimal(closed)) if closed else None,
            "fill_count": fills, "signal_count": signals, "fees": str(fees),
            "funding": str(funding), "exit_reasons": exits,
            "evidence_sha256": evidence.hexdigest(), "frozen_until_cycles": 100,
            "network_orders": False, "trading_authority": False,
            "automatic_strategy_mutation": False}
