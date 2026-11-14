"""Fixed technical plans and protective exits for a historical order ladder."""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, localcontext

from .fast_screen import decimal, digest
from .two_agent_weekly import (
    BASE_ADVERSE, FEE, FUNDING_RESERVE_PER_DAY, INITIAL, STRESS_ADVERSE,
    STRESS_FUNDING_RESERVE_PER_DAY, _admitted, _aligned_week_cuts, prepare, weekly_summary,
)


ZERO = Decimal(0)
BARS_PER_HOUR = 4
RISK_FRACTION = Decimal("0.005")
NOTIONAL_CAP = Decimal("0.5")


@dataclass(frozen=True)
class Plan:
    side: int
    levels: tuple[Decimal, ...]
    stop: Decimal
    target: Decimal
    maximum_bars: int


@dataclass(frozen=True)
class Candle:
    opened: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def make_plan(history: list[Candle], family: str, *, step: int = 15) -> Plan | None:
    """Use a trailing range, directional efficiency and 14-bar true range."""
    if family not in ("RANGE_GRID", "TREND_PULLBACK_GRID"):
        raise ValueError("unknown technical grid family")
    hourly = 60 // step
    longest = 48 * hourly + 1
    if len(history) < longest or not history[-1].volume:
        return None
    close = history[-1].close
    recent = history[-12 * hourly:]
    travel = sum(abs(a.close - b.close) for a, b in zip(recent, recent[1:]))
    if not travel:
        return None
    efficiency = abs(recent[0].close - close) / travel
    lower, upper = min(bar.low for bar in recent), max(bar.high for bar in recent)
    width = upper - lower
    atr_period = max(4, 14 * hourly // BARS_PER_HOUR)
    atr_window = history[-atr_period - 1:]
    atr = sum(max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
              for a, b in zip(atr_window, atr_window[1:])) / atr_period
    if atr <= 0:
        return None
    if family == "RANGE_GRID":
        if efficiency > Decimal("0.30") or not 5 * atr <= width <= 16 * atr:
            return None
        mid = (lower + upper) / 2
        side = 1 if close < mid else -1
        edge = lower if side == 1 else upper
        levels = (edge + side * width / 4, edge + side * width / 8)
        stop, target, hold = edge - side * atr / 2, mid, 12 * hourly
    else:
        bias = close - history[-longest].close
        mean = sum(bar.close for bar in history[-6 * hourly:]) / (6 * hourly)
        side = 1 if bias > 0 else -1
        if abs(bias) < 6 * atr or abs(close - mean) > 2 * atr or side * (close - mean) < 0:
            return None
        levels = (close - side * atr, close - side * 2 * atr)
        stop, target, hold = close - side * 3 * atr, close + side * 4 * atr, 72 * hourly
    if min(*levels, stop, target) <= 0 or side * (close - levels[0]) <= 0:
        return None
    return Plan(side, levels, stop, target, hold)


@dataclass
class Ladder:
    plan: Plan
    opened: int
    quantity_per_level: Decimal
    initial_balance: Decimal
    quantity: Decimal = ZERO
    basis: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    filled_levels: int = 0
    last_fill: int | None = None
    step: int = 15
    fee_rate: Decimal = FEE


def size_plan(plan: Plan, balance: Decimal, index: int, adverse: Decimal,
              funding_per_day: Decimal, *, step: int = 15, fee_rate: Decimal = FEE) -> Ladder | None:
    side = plan.side
    stop_fill = plan.stop * (1 - side * adverse)
    # Use taker costs on every leg. Reserve for all adds reaching the stop.
    unit_risk = sum(side * (price - stop_fill) + fee_rate * (price + stop_fill)
                    + funding_per_day * price * plan.maximum_bars / (1440 // step)
                    for price in plan.levels)
    costs = (fee_rate + adverse) * (plan.levels[0] + plan.target)
    if unit_risk <= 0 or side * (plan.target - plan.levels[0]) <= 2 * costs:
        return None
    quantity = min(balance * RISK_FRACTION / unit_risk,
                   balance * NOTIONAL_CAP / sum(plan.levels))
    return Ladder(plan, index, quantity, balance, step=step, fee_rate=fee_rate)


def _settlement(ladder: Ladder, reference: Decimal, adverse: Decimal) -> tuple[Decimal, Decimal]:
    price = reference * (1 - ladder.plan.side * adverse)
    exit_fee = ladder.quantity * price * ladder.fee_rate
    pnl = ladder.plan.side * (ladder.quantity * price - ladder.basis)
    return ladder.initial_balance + pnl - ladder.fees - exit_fee - ladder.funding, exit_fee


def advance(ladder: Ladder, candle: Candle, *, index: int, adverse: Decimal,
            funding_per_day: Decimal) -> tuple[str | None, Decimal]:
    """Conservative OHLC ordering: gap stop, adds, intrabar stop, then target."""
    plan = ladder.plan
    side = plan.side
    if side * (candle.opened - plan.stop) <= 0:
        reason = "STOP_GAP" if ladder.quantity else "CANCELLED_GAP"
        return reason, candle.opened
    if index - ladder.opened >= plan.maximum_bars:
        return "TIME_EXIT", candle.opened
    if not candle.volume:
        ladder.funding += ladder.basis * funding_per_day / (1440 // ladder.step)
        return None, candle.close
    adverse_wick = candle.low if side > 0 else candle.high
    favorable_wick = candle.high if side > 0 else candle.low
    prior_fills = ladder.filled_levels
    minimum_target_bars = 120 // ladder.step
    first_target_index = index + minimum_target_bars + 1  # A fill can arrive at the bar close.
    pending = plan.levels[prior_fills:] if first_target_index <= ladder.opened + plan.maximum_bars else ()
    for level in pending:
        if side * (adverse_wick - level) >= 0:
            break
        ladder.filled_levels += 1
        ladder.last_fill = index
        ladder.quantity += ladder.quantity_per_level
        cost = ladder.quantity_per_level * level
        ladder.basis += cost
        ladder.fees += cost * ladder.fee_rate
    ladder.funding += ladder.basis * funding_per_day / (1440 // ladder.step)
    if side * (adverse_wick - plan.stop) <= 0:
        return "STOP_TOUCH", plan.stop
    if (ladder.last_fill is not None and index - ladder.last_fill > minimum_target_bars
            and side * (favorable_wick - plan.target) > 0):
        return "TARGET", plan.target
    return None, candle.close


def _replay(markets, start, first, end, family, adverse, funding_per_day,
            *, first_level_only: bool, step: int, spot: bool) -> dict:
    balance = peak = INITIAL
    fees = funding = drawdown = ZERO
    marks = []
    episodes = []
    sides = {"LONG": 0, "SHORT": 0}
    reasons: dict[str, int] = {}
    net_by_exit: dict[str, Decimal] = {}
    ladder = None
    symbol = None
    history_size = 48 * (60 // step) + 1
    for index in range(first, end + 1):
        now = start + timedelta(minutes=step * index)
        reference_equity = balance
        if ladder is not None:
            bar = markets[symbol][index]
            reference_equity, _ = _settlement(ladder, bar.opened, adverse)
        if now.weekday() == 0 and now.hour == now.minute == 0:
            marks.append((now.isoformat(), str(reference_equity)))
        if index == end:
            if ladder is not None:
                raise AssertionError("position crossed partition")
            break
        if (ladder is None and balance > 0 and index - first >= history_size + 1
                and now.hour % 8 == 0 and now.minute == 0):
            for name, bars in markets.items():
                plan = make_plan(bars[index - history_size - 1:index - 1], family, step=step)
                if (plan is not None and (not spot or plan.side > 0)
                        and index + plan.maximum_bars < end):
                    if first_level_only:
                        plan = replace(plan, levels=plan.levels[:1])
                    ladder = size_plan(plan, balance, index, adverse, funding_per_day,
                                       step=step, fee_rate=Decimal("0.001") if spot else FEE)
                    if ladder:
                        symbol = name
                        break
        if ladder is not None:
            candle = markets[symbol][index]
            reason, price = advance(ladder, candle, index=index, adverse=adverse,
                                    funding_per_day=funding_per_day)
            marked, exit_fee = _settlement(ladder, price, adverse)
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
                if ladder.quantity:
                    side = "LONG" if ladder.plan.side > 0 else "SHORT"
                    sides[side] += 1
                    fees += ladder.fees + exit_fee
                    funding += ladder.funding
                    net_by_exit[reason] = net_by_exit.get(reason, ZERO) + marked - balance
                    episodes.append({"market": symbol, "side": side, "exit": reason,
                                     "at": now.isoformat(), "filled_levels": ladder.filled_levels,
                                     "net": str(marked - balance)})
                balance = marked
                ladder = None
        else:
            marked = balance
        peak = max(peak, reference_equity, marked)
        drawdown = max(drawdown, 1 - min(reference_equity, marked) / peak)
    return {"trades": sum(sides.values()), "sides": sides, "plan_terminal_reasons": reasons,
            "closed_trade_net_by_exit": {reason: str(value) for reason, value in net_by_exit.items()},
            "net_return": str(balance / INITIAL - 1), "maximum_drawdown": str(drawdown),
            "fees": str(fees), "funding_reserve": str(funding), "weekly": weekly_summary(marks),
            "weekly_marks": marks, "episode_sha256": digest(episodes),
            "filled_levels": sum(episode["filled_levels"] for episode in episodes)}


def evaluate_grid(inputs: dict[str, list[dict]], *, first_level_only: bool = False,
                  spot: bool = False) -> dict:
    step = 60 if spot else 15
    start, _ = prepare(inputs, step=step, spot=spot)
    markets = {symbol: [Candle(*(decimal(bar[key]) for key in ("open", "high", "low", "close", "volume")))
                        for bar in bars] for symbol, bars in sorted(inputs.items())}
    first, train_end, dev_end, last = _aligned_week_cuts(start, len(next(iter(markets.values()))), step)
    costs = (("base", BASE_ADVERSE, ZERO if spot else FUNDING_RESERVE_PER_DAY),
             ("stress", STRESS_ADVERSE, ZERO if spot else STRESS_FUNDING_RESERVE_PER_DAY))
    with localcontext() as context:
        context.prec = 36
        agents = {}
        for family in ("RANGE_GRID", "TREND_PULLBACK_GRID"):
            stages = {}
            for stage, begin, end, minimum in (
                ("TRAIN", first, train_end, 24), ("DEVELOPMENT", train_end, dev_end, 8),
                ("FINAL", dev_end, last, 8),
            ):
                scores = {name: _replay(markets, start, begin, end, family, adverse, funding,
                                        first_level_only=first_level_only, step=step, spot=spot)
                          for name, adverse, funding in costs}
                stages[stage] = scores
                if not _admitted(scores, minimum_trades=minimum):
                    break
            agents[family] = stages
    result = {"schema": "autotrade-technical-grid-v1", "mode": "TECHNICAL_GRID_WITH_STOPS",
              "final_evaluated": any("FINAL" in stages for stages in agents.values()), "agents": agents,
              "bar_minutes": step, "initial_sleeve_equity": str(INITIAL),
              "order_levels": 1 if first_level_only else 2,
              "maximum_modeled_risk_fraction": str(RISK_FRACTION),
              "maximum_notional_fraction": str(NOTIONAL_CAP), "cash_weekly_return": "0",
              "modeled_ohlcv_limit_fills": True, "paid_model_calls": 0, "network_orders": False,
              "automatic_promotion": False}
    if spot:
        result["instrument_policy"] = "SPOT_LONG_ONLY_10_BPS_PER_FILL_NO_FUNDING"
    result["result_sha256"] = digest(result)
    return result
