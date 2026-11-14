"""WhiteBIT research replay of high-turnover pullbacks with wick rejection."""

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext

from .fast_screen import decimal, digest
from .technical_grid import Candle
from .two_agent_weekly import (
    BASE_ADVERSE,
    FEE,
    FUNDING_RESERVE_PER_DAY,
    INITIAL,
    MAX_DRAWDOWN,
    STRESS_ADVERSE,
    STRESS_FUNDING_RESERVE_PER_DAY,
    _aligned_week_cuts,
    prepare,
    weekly_summary,
)


HOLD_MINUTES = (45, 60, 120, 240, 480, 720)
CADENCE_MINUTES = 15
BARS_PER_DAY = 1440 // CADENCE_MINUTES
LOOKBACK_BARS = 32
CAPITAL_FRACTION = Decimal("0.35")
RISK_FRACTION = Decimal("0.005")
ZERO = Decimal(0)


@dataclass(frozen=True)
class Signal:
    side: int
    close: Decimal
    atr: Decimal


def _pullback(history: list[Candle], *, require_wick: bool = True) -> tuple[Signal | None, str]:
    """The completed M15 reversal must reclaim half the pullback."""
    if len(history) < LOOKBACK_BARS + 1:
        return None, "WARMUP"
    impulse, rejection = history[-2:]
    prior = history[-26:-2]
    if impulse.volume <= 0 or rejection.volume <= 0:
        return None, "NO_SIGNAL_BAR_VOLUME"
    ranges = [
        max(bar.high - bar.low, abs(bar.high - before.close), abs(bar.low - before.close))
        for before, bar in zip(prior[-15:-1], prior[-14:])
    ]
    atr = sum(ranges) / len(ranges)
    range_width = rejection.high - rejection.low
    trend = prior[-1].close - history[-LOOKBACK_BARS - 1].close
    if atr <= 0 or range_width <= 0 or abs(trend) < 4 * atr:
        return None, "NO_TREND_OR_RANGE"
    mean_activity = sum(bar.volume * bar.close for bar in prior) / len(prior)
    if mean_activity <= 0 or impulse.volume * impulse.close < 2 * mean_activity:
        return None, "NO_TURNOVER_SPIKE"
    side = 1 if trend > 0 else -1
    retreat = side * (prior[-1].close - impulse.close)
    reclaim = side * (rejection.close - impulse.close)
    wick = (min(rejection.opened, rejection.close) - rejection.low if side > 0
            else rejection.high - max(rejection.opened, rejection.close))
    if not atr <= retreat <= 3 * atr:
        return None, "NO_OPPOSITE_PULLBACK"
    if not retreat / 2 <= reclaim < retreat or side * (rejection.close - rejection.opened) <= 0:
        return None, "NO_DIRECTIONAL_RECLAIM"
    if require_wick and wick < range_width * Decimal("0.30"):
        return None, "NO_REJECTION_WICK"
    return Signal(side, rejection.close, atr), "SIGNAL"


def pullback_signal(history: list[Candle]) -> Signal | None:
    return _pullback(history)[0]


@dataclass
class Position:
    market: str
    side: int
    start: int
    quantity: Decimal
    reference: Decimal
    entry: Decimal
    stop: Decimal
    balance_at_entry: Decimal
    entry_fee: Decimal


def exit_reference(position: Position, candle: Candle, timed_out: bool) -> tuple[Decimal, str]:
    side = position.side
    if side * (candle.opened - position.stop) <= 0:
        return candle.opened, "STOP_GAP"
    if timed_out:
        return candle.opened, "TIME_EXIT"
    adverse_extreme = candle.low if side > 0 else candle.high
    if candle.volume > 0 and side * (adverse_extreme - position.stop) <= 0:
        return position.stop, "STOP_TOUCH"
    return candle.close, ""


def _mark(position, reference, bars_held, adverse, funding_rate):
    exit_price = reference * (1 - position.side * adverse)
    fee = position.quantity * exit_price * FEE
    funding = position.quantity * position.entry * funding_rate * bars_held / BARS_PER_DAY
    gross = position.quantity * position.side * (reference - position.reference)
    slippage = position.quantity * adverse * (reference + position.reference)
    marked = position.balance_at_entry + gross - slippage - position.entry_fee - fee - funding
    return marked, gross, slippage, fee, funding


def _signals(candles, start, end, require_wick):
    candidates = {}
    counts = Counter()
    for index in range(start + LOOKBACK_BARS + 2, end):
        champion = None
        for market, bars in candles.items():
            signal, reason = _pullback(
                bars[index - LOOKBACK_BARS - 2:index - 1], require_wick=require_wick,
            )
            reference = bars[index].opened
            if signal is None:
                counts[reason] += 1
                continue
            if abs(reference - signal.close) > signal.atr or reference <= 2 * signal.atr:
                counts["OPEN_PRICE_OUTSIDE_LIMIT"] += 1
                continue
            counts[reason] += 1
            confidence = signal.atr / signal.close
            if champion is None or confidence > champion[0]:
                champion = (confidence, market, signal)
        if champion:
            candidates[index] = champion[1:]
    return candidates, dict(counts)


def _replay(candles, start, first, end, entries, hold_minutes, adverse, funding_rate):
    hold_bars = hold_minutes // CADENCE_MINUTES
    balance = peak = INITIAL
    drawdown = ZERO
    position = None
    marks, episodes = [], []
    gross_pnl = slippage_paid = fees = funding = ZERO
    bar_duration = timedelta(minutes=CADENCE_MINUTES)
    zero_volume_legs = 0
    for index in range(first, end + 1):
        timestamp = start + index * bar_duration
        if index in entries and position is not None:
            raise ValueError("entry schedule overlaps")
        if position is not None:
            bar = candles[position.market][index]
            open_equity = _mark(position, bar.opened, index - position.start, adverse, funding_rate)[0]
        else:
            open_equity = balance
        if timestamp.weekday() == 0 and timestamp.hour == timestamp.minute == 0:
            marks.append((timestamp.isoformat(), str(open_equity)))
        if index in entries:
            market, signal = entries[index]
            reference = candles[market][index].opened
            stop_distance = signal.atr * 2
            entry = reference * (1 + signal.side * adverse)
            risk = stop_distance + reference * (2 * (adverse + FEE) + funding_rate * hold_bars / BARS_PER_DAY)
            quantity = min(balance * CAPITAL_FRACTION / entry, balance * RISK_FRACTION / risk)
            if quantity > 0:
                position = Position(market, signal.side, index, quantity, reference, entry,
                                    reference - signal.side * stop_distance, balance,
                                    quantity * entry * FEE)
                zero_volume_legs += int(candles[market][index].volume == 0)
        marked = balance
        if position is not None:
            timed_out = index == position.start + hold_bars
            bar = candles[position.market][index]
            reference, reason = exit_reference(position, bar, timed_out)
            # A stop can hit during the current bar. Reserve its full carry.
            elapsed = index - position.start + int(reason not in ("STOP_GAP", "TIME_EXIT"))
            marked, gross, slippage, exit_fee, carry = _mark(position, reference, elapsed, adverse, funding_rate)
            if reason:
                if reason != "STOP_TOUCH":
                    zero_volume_legs += int(bar.volume == 0)
                fees += position.entry_fee + exit_fee
                gross_pnl += gross
                slippage_paid += slippage
                funding += carry
                episodes.append({
                    "market": position.market, "side": position.side,
                    "entry_at": (start + position.start * bar_duration).isoformat(),
                    "exit_bar_open_at": timestamp.isoformat(), "reason": reason,
                    "net_pnl": str(marked - balance),
                })
                balance = marked
                position = None
        peak = max(peak, open_equity, marked)
        drawdown = max(drawdown, 1 - min(open_equity, marked) / peak)
    if position is not None:
        raise ValueError("open position crossed a research partition")
    return {
        "trades": len(episodes), "net_return": str(balance / INITIAL - 1),
        "gross_midpoint_pnl": str(gross_pnl), "fees": str(fees), "adverse_price_cost": str(slippage_paid),
        "funding_reserve": str(funding), "maximum_drawdown": str(drawdown), "weekly": weekly_summary(marks),
        "zero_volume_fill_legs": zero_volume_legs, "episodes": episodes,
    }


def _partition(candles, start, first, end, holds, require_wick):
    signals, counts = _signals(candles, first, end, require_wick)
    rows = {}
    for minutes in holds:
        hold_bars = minutes // CADENCE_MINUTES
        next_entry = first
        entries, reverse = {}, {}
        for index, (market, signal) in signals.items():
            if index < next_entry or index + hold_bars >= end:
                continue
            entries[index] = (market, signal)
            reverse[index] = (market, Signal(-signal.side, signal.close, signal.atr))
            next_entry = index + hold_bars + 1
        rows[minutes] = {
            scenario: {
                "candidate": _replay(candles, start, first, end, entries, minutes, adverse, funding),
                "opposite_same_clocks": _replay(candles, start, first, end, reverse, minutes, adverse, funding),
            } for scenario, adverse, funding in (
                ("base", BASE_ADVERSE, FUNDING_RESERVE_PER_DAY),
                ("stress", STRESS_ADVERSE, STRESS_FUNDING_RESERVE_PER_DAY),
            )
        }
    return rows, counts


def _qualifies(costs, minimum_trades):
    for scores in costs.values():
        result = scores["candidate"]
        if (result["trades"] < minimum_trades or Decimal(result["net_return"]) <= 0
                or Decimal(result["net_return"]) <= Decimal(scores["opposite_same_clocks"]["net_return"])
                or Decimal(result["maximum_drawdown"]) > MAX_DRAWDOWN or result["zero_volume_fill_legs"]):
            return False
    return True


def evaluate_absorption(inputs, *, require_wick=True):
    start, _ = prepare(inputs, step=CADENCE_MINUTES)
    candles = {symbol: [Candle(*(decimal(bar[key]) for key in ("open", "high", "low", "close", "volume")))
                        for bar in bars] for symbol, bars in sorted(inputs.items())}
    first, train_end, development_end, end = _aligned_week_cuts(
        start, len(next(iter(candles.values()))), CADENCE_MINUTES,
    )
    with localcontext() as context:
        context.prec = 36
        train, train_filter_counts = _partition(candles, start, first, train_end, HOLD_MINUTES, require_wick)
        selected = None
        if require_wick:
            selected = max(
                (minutes for minutes, costs in train.items() if _qualifies(costs, 24)),
                key=lambda minutes: Decimal(train[minutes]["stress"]["candidate"]["net_return"]),
                default=None,
            )
        dev = (_partition(candles, start, train_end, development_end, (selected,), require_wick)[0]
               if selected else None)
        final = (_partition(candles, start, development_end, end, (selected,), require_wick)[0]
                 if dev and _qualifies(dev[selected], 8) else None)
    result = {
        "schema": "autotrade-intraday-absorption-v1", "mode": "INTRADAY_ABSORPTION",
        "bar_minutes": CADENCE_MINUTES, "bars_per_market": len(next(iter(candles.values()))),
        "cut_open_utc": [(start + timedelta(minutes=CADENCE_MINUTES * i)).isoformat()
                         for i in (first, train_end, development_end, end)],
        "execution": "OHLCV_PROXY_ONLY", "train": train, "selected_hold_minutes": selected,
        "require_rejection_wick": require_wick, "train_filter_counts": train_filter_counts,
        "development": dev, "final": final, "final_evaluated": final is not None,
        "cash_control_return": "0", "network_orders": False, "paid_model_calls": 0,
        "automatic_promotion": False,
    }
    result["result_sha256"] = digest(result)
    return result
