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
from .pretrade_evidence import PreTradeThesis, evaluate_pretrade
from .technical_confluence import HourlyEquity, TechnicalCosts


VERSION = "private-course-quasimodo-fvg-v1.2-frozen-100-cycle"


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
    minimum_shift_confirmation_bars: int = 1
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
    require_true_top_down: bool = False

    def validate(self) -> None:
        integer_bounds = {
            "context_lookback": (64, 5000), "context_fast_ema": (4, 1000),
            "context_slow_ema": (8, 2000), "poi_lookback": (3, 2000),
            "sweep_lookback": (3, 1000), "shift_lookback": (2, 500),
            "minimum_shift_confirmation_bars": (1, 500),
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
        if self.minimum_shift_confirmation_bars > self.shift_expiry_bars:
            raise ValueError("minimum shift confirmation cannot exceed expiry")
        if type(self.require_true_top_down) is not bool:
            raise TypeError("require_true_top_down must be an exact bool")
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


def _ema(values: list[float], period: int) -> float:
    """Return a signal-only EMA.

    Prices, fills, fees and equity remain Decimal.  Signal comparisons use
    binary64 so multi-year research does not repeat millions of expensive
    arbitrary-precision operations for values that originate as exchange
    decimal strings and are ultimately compared, not posted to a ledger.
    """
    alpha = 2.0 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _atr(bars: list[dict], period: int) -> float:
    total = 0.0
    for index in range(len(bars) - period, len(bars)):
        previous = float(bars[index - 1]["close"])
        high, low = float(bars[index]["high"]), float(bars[index]["low"])
        total += max(high - low, abs(high - previous), abs(low - previous))
    return total / period


def _active_fvg(history: list[dict], *, direction: int,
                lookback: int, current_low: float,
                current_high: float, current_close: float) -> tuple[float, float] | None:
    start = max(2, len(history) - lookback)
    for index in range(len(history) - 2, start - 1, -1):
        older_high = float(history[index - 2]["high"])
        older_low = float(history[index - 2]["low"])
        newer_high = float(history[index]["high"])
        newer_low = float(history[index]["low"])
        if direction > 0 and newer_low > older_high:
            lower, upper = older_high, newer_low
            if current_low <= upper and current_close >= lower:
                return lower, upper
        if direction < 0 and newer_high < older_low:
            lower, upper = newer_high, older_low
            if current_high >= lower and current_close <= upper:
                return lower, upper
    return None


def _active_order_or_rejection_block(history: list[dict], *, direction: int,
                                     current_low: float, current_high: float,
                                     current_close: float) -> tuple[float, float] | None:
    """Return a bounded H1 order/rejection-block proxy from completed bars."""
    if len(history) < 16:
        return None
    ranges = [float(row["high"]) - float(row["low"]) for row in history[-16:]]
    normal = sum(ranges) / len(ranges)
    start = max(1, len(history) - 24)
    for index in range(len(history) - 2, start - 1, -1):
        row = history[index]
        opened, close = float(row["open"]), float(row["close"])
        low, high = float(row["low"]), float(row["high"])
        future = history[index + 1:min(len(history), index + 4)]
        if not future:
            continue
        future_close = float(future[-1]["close"])
        impulse = max(float(item["high"]) for item in future) - min(float(item["low"]) for item in future)
        bullish = (direction > 0 and close <= opened and future_close > high
                   and impulse >= normal * 1.5)
        bearish = (direction < 0 and close >= opened and future_close < low
                   and impulse >= normal * 1.5)
        body = abs(close - opened); candle_range = high - low
        rejection = (candle_range > 0 and body / candle_range <= 0.5)
        if (bullish or bearish) and (rejection or body > 0):
            if current_low <= high and current_high >= low:
                if direction > 0 and current_close >= low:
                    return low, high
                if direction < 0 and current_close <= high:
                    return low, high
    return None


def _completed_aggregates(history: list[dict], seconds: int) -> list[dict]:
    """Aggregate only fully completed UTC buckets from causal M15 history."""
    groups: list[dict] = []
    active_key = None
    active = None
    for row in history:
        opened = row["opened_at"]
        key = int(opened.timestamp()) // seconds
        if key != active_key:
            if active is not None:
                groups.append(active)
            active_key = key
            active = {"opened_at": opened, "closed_at": row["closed_at"],
                "open": row["open"], "high": row["high"], "low": row["low"],
                "close": row["close"], "volume": row["volume"]}
        else:
            active["closed_at"] = row["closed_at"]
            active["high"] = str(max(float(active["high"]), float(row["high"])))
            active["low"] = str(min(float(active["low"]), float(row["low"])))
            active["close"] = row["close"]
            active["volume"] = str(float(active["volume"]) + float(row["volume"]))
    if active is not None and int(active["closed_at"].timestamp()) % seconds == 0:
        groups.append(active)
    return groups


def _top_down_from_aggregates(daily: list[dict], hourly: list[dict], current: dict) -> int:
    if len(daily) < 21 or len(hourly) < 25:
        return 0
    daily_closes = [float(row["close"]) for row in daily[-20:]]
    hourly_closes = [float(row["close"]) for row in hourly[-24:]]
    daily_direction = 1 if _ema(daily_closes[-5:], 5) > _ema(daily_closes, 20) else -1
    hourly_direction = 1 if _ema(hourly_closes[-8:], 8) > _ema(hourly_closes, 24) else -1
    if daily_direction != hourly_direction:
        return 0
    context_high = max(float(row["high"]) for row in daily[-20:])
    context_low = min(float(row["low"]) for row in daily[-20:])
    midpoint = (context_high + context_low) / 2.0
    close = float(current["close"]); low = float(current["low"]); high = float(current["high"])
    if daily_direction > 0 and close > midpoint:
        return 0
    if daily_direction < 0 and close < midpoint:
        return 0
    poi = (_active_fvg(hourly, direction=daily_direction, lookback=24,
        current_low=low, current_high=high, current_close=close)
        or _active_order_or_rejection_block(hourly, direction=daily_direction,
            current_low=low, current_high=high, current_close=close))
    return daily_direction if poi is not None else 0


def _top_down_direction(history: list[dict]) -> int:
    """Return causal Daily -> H1 bias with a current H1 FVG POI, or zero."""
    return _top_down_from_aggregates(_completed_aggregates(history, 86400),
        _completed_aggregates(history, 3600), history[-1])


def _precompute_top_down(bars: list[dict]) -> list[int]:
    """Linear causal top-down projection for a complete research partition."""
    all_daily = _completed_aggregates(bars, 86400)
    all_hourly = _completed_aggregates(bars, 3600)
    daily: list[dict] = []; hourly: list[dict] = []
    day_index = hour_index = 0
    result: list[int] = []
    for current in bars:
        closed = current["closed_at"]
        while day_index < len(all_daily) and all_daily[day_index]["closed_at"] <= closed:
            daily.append(all_daily[day_index]); day_index += 1
        while hour_index < len(all_hourly) and all_hourly[hour_index]["closed_at"] <= closed:
            hourly.append(all_hourly[hour_index]); hour_index += 1
        result.append(_top_down_from_aggregates(daily, hourly, current))
    return result


def detect_sweep(history: list[dict], config: CourseConfiguration,
                 *, top_down_direction: int | None = None) -> dict | None:
    """Detect one course-compatible sweep at the latest completed bar."""
    config.validate()
    if len(history) < config.context_lookback:
        return None
    current = history[-1]
    prior = history[-1-config.sweep_lookback:-1]
    context = history[-config.context_lookback:-1]
    closes = [float(row["close"]) for row in context]
    close, opened = float(current["close"]), float(current["open"])
    high, low = float(current["high"]), float(current["low"])
    context_high = max(float(row["high"]) for row in context)
    context_low = min(float(row["low"]) for row in context)
    midpoint = (context_high + context_low) / 2.0
    fast = _ema(closes[-config.context_fast_ema * 3:], config.context_fast_ema)
    slow = _ema(closes[-config.context_slow_ema * 3:], config.context_slow_ema)
    atr = _atr(history, config.atr_period)
    prior_low = min(float(row["low"]) for row in prior)
    prior_high = max(float(row["high"]) for row in prior)
    reaction = high - low
    direction = 0
    if (fast > slow and close <= midpoint and low < prior_low and close > prior_low
            and close > opened and reaction >= atr * float(config.minimum_reaction_atr)):
        direction = 1
    elif (fast < slow and close >= midpoint and high > prior_high and close < prior_high
          and close < opened and reaction >= atr * float(config.minimum_reaction_atr)):
        direction = -1
    if not direction:
        return None
    if (config.require_true_top_down
            and (top_down_direction if top_down_direction is not None
                 else _top_down_direction(history)) != direction):
        return None
    # In strict top-down mode the completed H1 FVG/order/rejection block is the
    # POI.  Requiring a second pre-existing M15 FVG at the same instant would
    # incorrectly collapse two adjacent course layers into one and reject the
    # valid LTF sweep/market-shift confirmation.
    fvg = ((low, high) if config.require_true_top_down else
           _active_fvg(history[:-1], direction=direction, lookback=config.poi_lookback,
                       current_low=low, current_high=high, current_close=close))
    if fvg is None:
        return None
    local = history[-1-config.shift_lookback:-1]
    shift = (max(float(row["high"]) for row in local) if direction > 0
             else min(float(row["low"]) for row in local))
    bounded = lambda value: _bounded_text(Decimal(str(value)))
    return {"direction": direction, "sweep_extreme": bounded(low if direction > 0 else high),
            "shift_level": bounded(shift),
            "target": bounded(context_high if direction > 0 else context_low),
            "atr": bounded(atr), "fvg_lower": bounded(fvg[0]),
            "fvg_upper": bounded(fvg[1]),
            "age": 0, "detected_at": current["closed_at"].isoformat(),
            "top_down_verified": config.require_true_top_down}


def simulate(bars: list[dict], *, costs: TechnicalCosts = TechnicalCosts(),
             configuration: CourseConfiguration = CourseConfiguration(),
             report_hourly_returns: bool = False) -> dict:
    """Run the frozen course setup with causal next-open entries."""
    validate_bars(bars, require_ohlcv=True); costs.validate(); configuration.validate()
    with localcontext() as context:
        context.prec = 50
        balance = initial = peak = decimal(costs.initial_equity)
        clock = HourlyEquity(bars[0]["opened_at"], initial) if report_hourly_returns else None
        fee_ratio = decimal(costs.fee_bps_per_fill) / Decimal(10000)
        adverse = (decimal(costs.spread_bps) / Decimal(2)
                   + decimal(costs.slippage_bps_per_fill)) / Decimal(10000)
        funding_day = decimal(costs.funding_bps_per_day) / Decimal(10000)
        risk_fraction = decimal(configuration.risk_fraction)
        maximum_notional = decimal(configuration.maximum_notional_multiple)
        quantity = entry = stop = target = Decimal(0)
        cycle_start_balance = balance
        cycle_start_fees = fees_at_entry = Decimal(0)
        cycle_start_funding = Decimal(0)
        history: list[dict] = []
        pending_setup = pending_entry = None
        active_episode = None
        episodes: list[dict] = []
        cooldown = holding = closed = wins = losses = fills = signals = 0
        pretrade_holds = 0
        pretrade_hold_reasons: dict[str, int] = {}
        fees = funding = maximum_drawdown = Decimal(0)
        exits: dict[str, int] = {}
        evidence = sha256()
        top_down = (_precompute_top_down(bars) if configuration.require_true_top_down
                    else [0] * len(bars))

        def mark(price: Decimal) -> None:
            nonlocal peak, maximum_drawdown
            equity = balance + quantity * (price - entry)
            peak = max(peak, equity)
            if peak > 0:
                maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)

        def close_position(price: Decimal, reason: str, at: str) -> None:
            nonlocal balance, quantity, entry, stop, target, fees, fills
            nonlocal closed, wins, losses, holding, cooldown
            nonlocal active_episode
            actual = price * (Decimal(1) - adverse if quantity > 0 else Decimal(1) + adverse)
            charge = abs(quantity) * actual * fee_ratio
            balance += quantity * (actual - entry) - charge
            pnl = balance - cycle_start_balance
            fees += charge; fills += 1; closed += 1
            wins += int(pnl > 0); losses += int(pnl <= 0)
            exits[reason] = exits.get(reason, 0) + 1
            evidence.update(json.dumps({"at": at, "reason": reason,
                "pnl": str(pnl), "balance": str(balance)}, sort_keys=True).encode())
            if active_episode is not None:
                episodes.append({**active_episode, "exited_at": at,
                    "exit_reason": reason, "exit_price": str(actual),
                    "pnl_after_costs": str(pnl),
                    "return_on_start_equity": str(pnl / cycle_start_balance),
                    "fees": str(fees - cycle_start_fees),
                    "funding": str(funding - cycle_start_funding),
                    "holding_bars": holding, "profitable": pnl > 0})
                active_episode = None
            quantity = entry = stop = target = Decimal(0)
            holding = 0; cooldown = configuration.cooldown_bars

        for bar_index, bar in enumerate(bars):
            opened = decimal(bar["open"])
            if pending_entry is not None and quantity == 0:
                direction = pending_entry["direction"]
                actual = opened * (Decimal(1) + adverse if direction > 0 else Decimal(1) - adverse)
                candidate_stop = decimal(pending_entry["stop"])
                candidate_target = decimal(pending_entry["target"])
                risk = (actual - candidate_stop if direction > 0 else candidate_stop - actual)
                reward = (candidate_target - actual if direction > 0 else actual - candidate_target)
                gate = evaluate_pretrade(PreTradeThesis(
                    direction="LONG" if direction > 0 else "SHORT",
                    regime="TREND", causal_data_complete=True,
                    regime_confirmed=True, location_confirmed=True,
                    trigger_confirmed=True, entry_price=actual,
                    invalidation_price=candidate_stop,
                    target_price=candidate_target,
                    round_trip_cost_bps=costs.round_trip_bps,
                    minimum_reward_risk=configuration.minimum_reward_risk,
                    microstructure_status="NOT_AVAILABLE_HISTORICAL"))
                if (gate["decision"] == "ENTER"
                        and reward / actual >= decimal(configuration.minimum_reward_fraction)):
                    risk_budget = max(Decimal(0), balance) * risk_fraction
                    opened_quantity = min(risk_budget / risk,
                        max(Decimal(0), balance) * maximum_notional / actual)
                    opened_quantity = opened_quantity.quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                    if opened_quantity > 0:
                        cycle_start_balance = balance
                        cycle_start_fees = fees
                        cycle_start_funding = funding
                        quantity = opened_quantity if direction > 0 else -opened_quantity
                        entry, stop, target = actual, candidate_stop, candidate_target
                        charge = abs(quantity) * entry * fee_ratio
                        balance -= charge; fees += charge; fills += 1; holding = 0
                        fees_at_entry = charge
                        active_episode = {
                            "detected_at": pending_entry["detected_at"],
                            "entered_at": bar["opened_at"].isoformat(),
                            "direction": "LONG" if direction > 0 else "SHORT",
                            "entry_price": str(entry), "stop_price": str(stop),
                            "target_price": str(target),
                            "planned_reward_risk": str(reward / risk),
                            "entry_fee": str(fees_at_entry),
                            "sweep_to_shift_bars": pending_entry["sweep_to_shift_bars"],
                            "fvg_lower": pending_entry["fvg_lower"],
                            "fvg_upper": pending_entry["fvg_upper"],
                            "atr_at_setup": pending_entry["atr"]}
                        active_episode["pretrade_evidence"] = gate
                        active_episode["top_down_verified"] = pending_entry["top_down_verified"]
                else:
                    pretrade_holds += 1
                    reasons = gate["failure_reasons"] or ["MINIMUM_REWARD_FRACTION"]
                    for reason in reasons:
                        pretrade_hold_reasons[reason] = pretrade_hold_reasons.get(reason, 0) + 1
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
                    reference = min(opened, stop) if quantity > 0 else max(opened, stop)
                    close_position(reference, "PRECOMMITTED_STOP", bar["closed_at"].isoformat())
                elif targeted:
                    close_position(target, "OPPOSING_LIQUIDITY_TARGET", bar["closed_at"].isoformat())
                elif holding >= configuration.maximum_hold_bars:
                    close_position(close, "MAXIMUM_HOLD_EXIT", bar["closed_at"].isoformat())
            mark(close)
            if clock is not None:
                clock.mark(bar["closed_at"], balance=balance, quantity=quantity, entry=entry,
                           close=close, adverse=adverse, fee_ratio=fee_ratio)
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
                    if pending_setup["age"] < configuration.minimum_shift_confirmation_bars:
                        pending_setup = None
                        continue
                    atr = decimal(pending_setup["atr"])
                    extreme = decimal(pending_setup["sweep_extreme"])
                    buffer = atr * decimal(configuration.stop_buffer_atr)
                    pending_entry = {"direction": direction,
                        "stop": _bounded_text(extreme - buffer if direction > 0 else extreme + buffer),
                        "target": pending_setup["target"],
                        "detected_at": pending_setup["detected_at"],
                        "sweep_to_shift_bars": pending_setup["age"],
                        "fvg_lower": pending_setup["fvg_lower"],
                        "fvg_upper": pending_setup["fvg_upper"],
                        "atr": pending_setup["atr"],
                        "top_down_verified": pending_setup["top_down_verified"]}
                    pending_setup = None; signals += 1
                continue
            pending_setup = detect_sweep(history, configuration,
                top_down_direction=top_down[bar_index])
        if quantity:
            close_position(decimal(bars[-1]["close"]), "PARTITION_END_EXIT",
                           bars[-1]["closed_at"].isoformat())
        result = {"schema": VERSION, "configuration": asdict(configuration),
            "costs": asdict(costs), "round_trip_cost_bps": str(costs.round_trip_bps),
            "initial_equity": str(initial), "final_equity": str(balance),
            "net_pnl": str(balance - initial), "return_fraction": str((balance - initial) / initial),
            "maximum_drawdown_fraction": str(maximum_drawdown),
            "closed_cycles": closed, "wins": wins, "losses": losses,
            "win_rate": str(Decimal(wins) / Decimal(closed)) if closed else None,
            "fill_count": fills, "signal_count": signals, "fees": str(fees),
            "funding": str(funding), "exit_reasons": exits,
            "episodes": episodes,
            "pretrade_holds": pretrade_holds,
            "pretrade_hold_reasons": pretrade_hold_reasons,
            "evidence_sha256": evidence.hexdigest(), "frozen_until_cycles": 100,
            "network_orders": False, "trading_authority": False,
            "automatic_strategy_mutation": False}
        if clock is not None:
            result["clock_window_returns"] = clock.report()
        return result
