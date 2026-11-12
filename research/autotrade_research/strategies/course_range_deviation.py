"""Causal private-course range-deviation strategy for research simulation.

The course treats trend and consolidation as different market regimes.  This
module implements only the consolidation family: a completed-bar range is
formed from prior data, price sweeps an external boundary, then closes back
inside with an aggressive reaction.  Entry is at the next bar open, the stop
is beyond the sweep, and the first target is the range equilibrium.  It has no
network or order-provider dependency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
import json

from .fast_screen import decimal, validate_bars
from .technical_confluence import TechnicalCosts


VERSION = "private-course-range-deviation-v1.2-confirmed-boundaries-frozen-100-cycle"


def _text(value: Decimal) -> str:
    return format(value.quantize(Decimal("1e-18")), "f")


@dataclass(frozen=True)
class RangeDeviationConfiguration:
    range_lookback: int = 96
    atr_period: int = 14
    minimum_shift_confirmation_bars: int = 3
    shift_expiry_bars: int = 4
    confirmation_displacement_atr: str = "0.25"
    maximum_directional_efficiency: str = "0.25"
    minimum_boundary_touches: int = 2
    boundary_touch_tolerance_atr: str = "0.50"
    minimum_half_range_overlap: str = "0.50"
    minimum_range_atr: str = "4"
    maximum_range_atr: str = "20"
    minimum_deviation_atr: str = "0.10"
    minimum_reaction_atr: str = "0.75"
    stop_buffer_atr: str = "0.10"
    minimum_reward_risk: str = "2.0"
    minimum_reward_fraction: str = "0.003"
    risk_fraction: str = "0.01"
    maximum_notional_multiple: str = "2"
    maximum_hold_bars: int = 96
    cooldown_bars: int = 16

    def validate(self) -> None:
        if type(self.range_lookback) is not int or not 24 <= self.range_lookback <= 5000:
            raise ValueError("range_lookback is outside its bounded range")
        if type(self.atr_period) is not int or not 2 <= self.atr_period < self.range_lookback:
            raise ValueError("atr_period is outside its bounded range")
        if type(self.minimum_boundary_touches) is not int or not 2 <= self.minimum_boundary_touches <= 20:
            raise ValueError("minimum_boundary_touches is outside its bounded range")
        if (type(self.minimum_shift_confirmation_bars) is not int
                or not 1 <= self.minimum_shift_confirmation_bars <= self.shift_expiry_bars):
            raise ValueError("minimum shift confirmation is invalid")
        if type(self.shift_expiry_bars) is not int or not 1 <= self.shift_expiry_bars <= 500:
            raise ValueError("shift expiry is invalid")
        if type(self.maximum_hold_bars) is not int or not 1 <= self.maximum_hold_bars <= 10000:
            raise ValueError("maximum_hold_bars is outside its bounded range")
        if type(self.cooldown_bars) is not int or not 0 <= self.cooldown_bars <= 10000:
            raise ValueError("cooldown_bars is outside its bounded range")
        for name, low, high in (
                ("maximum_directional_efficiency", "0", "1"),
                ("boundary_touch_tolerance_atr", "0.05", "5"),
                ("minimum_half_range_overlap", "0", "1"),
                ("minimum_range_atr", "1", "100"),
                ("maximum_range_atr", "1", "200"),
                ("minimum_deviation_atr", "0", "10"),
                ("minimum_reaction_atr", "0.1", "20"),
                ("confirmation_displacement_atr", "0.05", "5"),
                ("stop_buffer_atr", "0", "5"),
                ("minimum_reward_risk", "1", "20"),
                ("minimum_reward_fraction", "0", "0.25"),
                ("risk_fraction", "0.0001", "0.05"),
                ("maximum_notional_multiple", "0.1", "10")):
            value = decimal(getattr(self, name))
            if not Decimal(low) <= value <= Decimal(high):
                raise ValueError(f"{name} is outside its bounded range")
        if decimal(self.minimum_range_atr) >= decimal(self.maximum_range_atr):
            raise ValueError("range ATR bounds must be ordered")


def _atr(history: list[dict], period: int) -> float:
    total = 0.0
    for index in range(len(history) - period, len(history)):
        previous = float(history[index - 1]["close"])
        high = float(history[index]["high"])
        low = float(history[index]["low"])
        total += max(high - low, abs(high - previous), abs(low - previous))
    return total / period


def detect_deviation(history: list[dict],
                     configuration: RangeDeviationConfiguration) -> dict | None:
    """Return the latest completed-bar deviation setup without future data."""
    configuration.validate()
    if len(history) < configuration.range_lookback + 1:
        return None
    current = history[-1]
    prior = history[-1-configuration.range_lookback:-1]
    # Signal comparisons use binary64 for throughput.  Fills, fees, funding,
    # risk sizing and equity remain exact Decimal values in ``simulate``.
    closes = [float(row["close"]) for row in prior]
    path = sum((abs(closes[index] - closes[index - 1])
                for index in range(1, len(closes))), 0.0)
    if path == 0:
        return None
    efficiency = abs(closes[-1] - closes[0]) / path
    if efficiency > float(configuration.maximum_directional_efficiency):
        return None
    atr = _atr(history, configuration.atr_period)
    if atr <= 0:
        return None
    lower = min(float(row["low"]) for row in prior)
    upper = max(float(row["high"]) for row in prior)
    width_atr = (upper - lower) / atr
    if not float(configuration.minimum_range_atr) <= width_atr <= float(configuration.maximum_range_atr):
        return None
    tolerance = float(configuration.boundary_touch_tolerance_atr) * atr
    lower_touches = sum(float(row["low"]) <= lower + tolerance for row in prior)
    upper_touches = sum(float(row["high"]) >= upper - tolerance for row in prior)
    if min(lower_touches, upper_touches) < configuration.minimum_boundary_touches:
        return None
    half = len(prior) // 2
    first, second = prior[:half], prior[half:]
    first_low = min(float(row["low"]) for row in first)
    first_high = max(float(row["high"]) for row in first)
    second_low = min(float(row["low"]) for row in second)
    second_high = max(float(row["high"]) for row in second)
    overlap = max(0.0, min(first_high, second_high) - max(first_low, second_low))
    union = max(first_high, second_high) - min(first_low, second_low)
    if union <= 0 or overlap / union < float(configuration.minimum_half_range_overlap):
        return None
    midpoint = (lower + upper) / 2.0
    opened = float(current["open"])
    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])
    reaction = high - low
    minimum_deviation = float(configuration.minimum_deviation_atr) * atr
    if (low <= lower - minimum_deviation and close > lower and close > opened
            and close < midpoint
            and reaction >= float(configuration.minimum_reaction_atr) * atr):
        direction = 1
        extreme = low
    elif (high >= upper + minimum_deviation and close < upper and close < opened
          and close > midpoint
          and reaction >= float(configuration.minimum_reaction_atr) * atr):
        direction = -1
        extreme = high
    else:
        return None
    stop = (extreme - float(configuration.stop_buffer_atr) * atr if direction > 0
            else extreme + float(configuration.stop_buffer_atr) * atr)
    # Confirmation is deliberately later than the sweep candle.  Requiring a
    # break of the whole range would enter at/after the target, so the course's
    # lower-timeframe market shift is represented by additional displacement
    # from the completed re-entry close while price is still in discount or
    # premium.
    displacement = float(configuration.confirmation_displacement_atr) * atr
    shift = close + displacement if direction > 0 else close - displacement
    if (direction > 0 and shift >= midpoint) or (direction < 0 and shift <= midpoint):
        return None
    bounded = lambda value: _text(Decimal(str(value)))
    return {"direction": direction, "range_lower": bounded(lower),
        "range_upper": bounded(upper), "range_midpoint": bounded(midpoint),
        "stop": bounded(stop), "target": bounded(midpoint), "sweep_extreme": bounded(extreme),
        "atr": bounded(atr), "directional_efficiency": bounded(efficiency),
        "range_width_atr": bounded(width_atr), "shift_level": bounded(shift), "age": 0,
        "lower_boundary_touches": lower_touches,
        "upper_boundary_touches": upper_touches,
        "half_range_overlap": bounded(overlap / union),
        "detected_at": current["closed_at"].isoformat()}


def simulate(bars: list[dict], *, costs: TechnicalCosts = TechnicalCosts(),
             configuration: RangeDeviationConfiguration = RangeDeviationConfiguration()) -> dict:
    """Simulate fixed-risk range deviations with conservative next-open fills."""
    validate_bars(bars, require_ohlcv=True)
    costs.validate(); configuration.validate()
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
        cycle_start_fees = cycle_start_funding = Decimal(0)
        pending_setup = pending_entry = active_episode = None
        history: list[dict] = []
        episodes: list[dict] = []
        cooldown = holding = closed = wins = losses = fills = signals = 0
        detected_setups = expired_setups = 0
        fees = funding = maximum_drawdown = Decimal(0)
        exits: dict[str, int] = {}
        evidence = sha256()

        def mark(price: Decimal) -> None:
            nonlocal peak, maximum_drawdown
            equity = balance + quantity * (price - entry)
            peak = max(peak, equity)
            if peak > 0:
                maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)

        def close_position(reference: Decimal, reason: str, at: str) -> None:
            nonlocal balance, quantity, entry, stop, target, fees, fills
            nonlocal closed, wins, losses, holding, cooldown, active_episode
            actual = reference * (Decimal(1) - adverse if quantity > 0 else Decimal(1) + adverse)
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

        for bar in bars:
            opened = decimal(bar["open"])
            if pending_entry is not None and quantity == 0:
                direction = pending_entry["direction"]
                actual = opened * (Decimal(1) + adverse if direction > 0 else Decimal(1) - adverse)
                candidate_stop = decimal(pending_entry["stop"])
                candidate_target = decimal(pending_entry["target"])
                risk = actual - candidate_stop if direction > 0 else candidate_stop - actual
                reward = candidate_target - actual if direction > 0 else actual - candidate_target
                if (risk > 0 and reward > 0
                        and reward / risk >= decimal(configuration.minimum_reward_risk)
                        and reward / actual >= decimal(configuration.minimum_reward_fraction)):
                    risk_budget = max(Decimal(0), balance) * risk_fraction
                    opened_quantity = min(risk_budget / risk,
                        max(Decimal(0), balance) * maximum_notional / actual)
                    opened_quantity = opened_quantity.quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                    if opened_quantity > 0:
                        cycle_start_balance = balance
                        cycle_start_fees = fees; cycle_start_funding = funding
                        quantity = opened_quantity if direction > 0 else -opened_quantity
                        entry, stop, target = actual, candidate_stop, candidate_target
                        charge = abs(quantity) * entry * fee_ratio
                        balance -= charge; fees += charge; fills += 1; holding = 0
                        active_episode = {**pending_entry,
                            "entered_at": bar["opened_at"].isoformat(),
                            "direction": "LONG" if direction > 0 else "SHORT",
                            "entry_price": str(entry), "stop_price": str(stop),
                            "target_price": str(target),
                            "planned_reward_risk": str(reward / risk),
                            "entry_fee": str(charge)}
                pending_entry = None

            high = decimal(bar["high"]); low = decimal(bar["low"])
            close = decimal(bar["close"])
            if quantity:
                holding += 1
                seconds = Decimal(str((bar["closed_at"] - bar["opened_at"]).total_seconds()))
                if not Decimal(0) < seconds <= Decimal(86400 * 7):
                    raise ValueError("bar duration is outside the funding-cost envelope")
                carry = abs(quantity) * close * funding_day * seconds / Decimal(86400)
                balance -= carry; funding += carry
                stopped = low <= stop if quantity > 0 else high >= stop
                targeted = high >= target if quantity > 0 else low <= target
                if stopped:
                    close_position(stop, "PRECOMMITTED_STOP_EXIT", bar["closed_at"].isoformat())
                elif targeted:
                    close_position(target, "RANGE_EQUILIBRIUM_EXIT", bar["closed_at"].isoformat())
                elif holding >= configuration.maximum_hold_bars:
                    close_position(close, "MAX_HOLD_EXIT", bar["closed_at"].isoformat())
            mark(close)
            history.append(bar)
            if quantity or pending_entry is not None:
                continue
            if cooldown:
                cooldown -= 1
                pending_setup = None
                continue
            if pending_setup is not None:
                pending_setup["age"] += 1
                confirmed = (close > decimal(pending_setup["shift_level"])
                    if pending_setup["direction"] > 0
                    else close < decimal(pending_setup["shift_level"]))
                if confirmed:
                    if pending_setup["age"] >= configuration.minimum_shift_confirmation_bars:
                        pending_entry = {**pending_setup,
                            "deviation_to_shift_bars": pending_setup["age"],
                            "shift_confirmed_at": bar["closed_at"].isoformat()}
                        signals += 1
                    # Confirmation age is the first causal crossing.  A setup
                    # that confirms too early cannot be relabelled as a later
                    # confirmation by waiting while price remains beyond it.
                    pending_setup = None
                elif pending_setup["age"] >= configuration.shift_expiry_bars:
                    pending_setup = None; expired_setups += 1
                continue
            setup = detect_deviation(history, configuration)
            if setup is not None:
                pending_setup = setup; detected_setups += 1

        if quantity:
            close_position(decimal(bars[-1]["close"]), "PARTITION_END_EXIT",
                           bars[-1]["closed_at"].isoformat())
        return {"schema": VERSION, "configuration": asdict(configuration),
            "costs": asdict(costs), "round_trip_cost_bps": str(costs.round_trip_bps),
            "initial_equity": str(initial), "final_equity": str(balance),
            "net_pnl": str(balance - initial),
            "return_fraction": str((balance - initial) / initial),
            "maximum_drawdown_fraction": str(maximum_drawdown),
            "closed_cycles": closed, "wins": wins, "losses": losses,
            "win_rate": str(Decimal(wins) / Decimal(closed)) if closed else None,
            "fill_count": fills, "signal_count": signals, "fees": str(fees),
            "detected_setups": detected_setups, "expired_setups": expired_setups,
            "funding": str(funding), "exit_reasons": exits, "episodes": episodes,
            "evidence_sha256": evidence.hexdigest(), "frozen_until_cycles": 100,
            "network_orders": False, "trading_authority": False,
            "automatic_strategy_mutation": False}

