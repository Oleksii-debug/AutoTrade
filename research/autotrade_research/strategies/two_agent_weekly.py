"""Two local numerical specialists with separate, equally funded research sleeves."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from statistics import median

from .fast_screen import decimal, digest, validate_bars


WEEK = timedelta(days=7)
TARGETS = (Decimal("0.03"), Decimal("0.05"))
INITIAL = Decimal(500)
FEE = Decimal("0.00055")
BASE_ADVERSE = Decimal("0.00020")
STRESS_ADVERSE = Decimal("0.00095")
FUNDING_RESERVE_PER_DAY = Decimal("0.0003")
STRESS_FUNDING_RESERVE_PER_DAY = Decimal("0.0006")
MAX_DRAWDOWN = Decimal("0.12")


@dataclass(frozen=True)
class Market:
    symbol: str
    opens: tuple[Decimal, ...]
    closes: tuple[Decimal, ...]
    zero_volume_indices: frozenset[int]


@dataclass(frozen=True)
class Candidate:
    agent: str
    hold_minutes: int
    lookback_minutes: int
    decision_minutes: int

    def signal(self, market: Market, index: int, step: int) -> Decimal:
        # The signal had a full bar to become available before the modeled fill.
        available = index - 2
        close = market.closes[available]
        lookback = self.lookback_minutes // step
        change = close / market.closes[available - lookback] - 1
        if self.agent == "INTRADAY_REVERSAL":
            broad = close / market.closes[available - 3 * lookback] - 1
            return -change if abs(change) >= 2 * abs(broad - change) else Decimal(0)
        return change


INTRADAY = tuple(Candidate("INTRADAY_REVERSAL", minutes, 60, 15)
                 for minutes in (30, 120, 360))
SWING = tuple(Candidate("SWING_MOMENTUM", minutes, 2880, 240)
              for minutes in (1440, 2880, 4320))
MINUTE_SMOKE = tuple(Candidate("INTRADAY_REVERSAL", minutes, 5, 5)
                     for minutes in (5, 10))


def prepare(inputs: dict[str, list[dict]], *, step: int, spot: bool = False) -> tuple[datetime, tuple[Market, ...]]:
    """Require the same gapless executable clock for every market."""
    suffix = "USDT" if spot else "PERP"
    if set(inputs) != {f"{base}_{suffix}" for base in ("BTC", "ETH", "SOL")}:
        raise ValueError("the frozen universe requires BTC, ETH and SOL of one instrument type")
    if step not in ((60,) if spot else (1, 5, 15)):
        raise ValueError("unsupported bar cadence")
    if any(len(bars) < 4 for bars in inputs.values()):
        raise ValueError("complete OHLCV history is required")
    for bars in inputs.values():
        validate_bars(bars, require_ohlcv=True)
    start = next(iter(inputs.values()))[0]["opened_at"].astimezone(UTC)
    delta = timedelta(minutes=step)
    if start.second or start.microsecond or start.minute % step:
        raise ValueError("bar opens must be aligned to the UTC cadence")
    count = len(next(iter(inputs.values())))
    markets = []
    for symbol, bars in sorted(inputs.items()):
        if (len(bars) != count or bars[0]["opened_at"] != start
                or any(row["closed_at"] - row["opened_at"] != delta for row in bars)):
            raise ValueError("market clocks do not match")
        markets.append(Market(symbol, tuple(decimal(row["open"]) for row in bars),
                              tuple(decimal(row["close"]) for row in bars),
                              frozenset(index for index, row in enumerate(bars)
                                        if decimal(row["volume"]) == 0)))
    return start, tuple(markets)


def _liquidate(balance, quantity, price, entry, side, adverse, elapsed_minutes, funding_rate):
    exit_price = price * (1 - side * adverse)
    gross = quantity * side * (exit_price - entry)
    fee = quantity * exit_price * FEE
    carry = quantity * entry * funding_rate * elapsed_minutes / 1440
    return balance + gross - fee - carry, fee, carry


def replay(markets: tuple[Market, ...], start: datetime, step: int, candidate: Candidate,
           first: int, end: int, *, adverse: Decimal, funding_rate: Decimal,
           keep_trades: bool = False, selective: bool = False) -> dict:
    """Enter after one full bar of delay, hold to the fixed exit, and mark every open.

    The last exit uses an open inside the cut. The prior cut never supplies
    signal warmup. Exposure remains capped at one unlevered sleeve notional.
    """
    balance = peak = INITIAL
    drawdown = Decimal(0)
    fees = funding = Decimal(0)
    position = None
    trade_count = zero_volume_fills = 0
    sides = {"LONG": 0, "SHORT": 0}
    episodes = []
    week_marks = []
    last_entry_day = None
    hold = candidate.hold_minutes // step
    warmup = candidate.lookback_minutes // step * (3 if candidate.agent == "INTRADAY_REVERSAL" else 1) + 2
    duration = timedelta(minutes=step)
    signal_floor = 4 * (FEE + BASE_ADVERSE) + (
        FUNDING_RESERVE_PER_DAY * candidate.hold_minutes / 1440)
    allocation = Decimal("0.25") if selective else Decimal(1)
    for index in range(first, end + 1):
        timestamp = start + index * duration
        marked = balance
        if position is not None:
            market, opened, entry, side, quantity, balance_before = position
            elapsed = (index - opened) * step
            marked, exit_fee, carry = _liquidate(
                balance, quantity, market.opens[index], entry, side, adverse, elapsed, funding_rate
            )
            if index == opened + hold:
                balance = marked
                fees += exit_fee
                funding += carry
                trade_count += 1
                zero_volume_fills += int(opened in market.zero_volume_indices)
                zero_volume_fills += int(index in market.zero_volume_indices)
                side_name = "LONG" if side > 0 else "SHORT"
                sides[side_name] += 1
                if keep_trades:
                    episodes.append({
                        "market": market.symbol, "side": side_name,
                        "signal_closed_at": (start + (opened - 1) * duration).isoformat(),
                        "entry_at": (start + opened * duration).isoformat(),
                        "exit_at": timestamp.isoformat(), "pnl": str(balance - balance_before),
                    })
                position = None
        peak = max(peak, marked)
        drawdown = max(drawdown, 1 - marked / peak)
        if timestamp.weekday() == 0 and timestamp.hour == timestamp.minute == 0:
            week_marks.append((timestamp.isoformat(), str(marked)))
        if (position is not None or index < first + warmup or index + hold >= end
                or int(timestamp.timestamp() // 60) % candidate.decision_minutes != 0
                or balance <= 0 or (selective and last_entry_day == timestamp.date())):
            continue
        ranked = [(candidate.signal(market, index, step), market) for market in markets
                  if index - 2 not in market.zero_volume_indices]
        if not ranked:
            continue
        signal, market = max(ranked, key=lambda row: abs(row[0]))
        if abs(signal) < signal_floor * (2 if selective else 1):
            continue
        side = 1 if signal > 0 else -1
        if selective and sum(other * side >= signal_floor for other, _ in ranked) < 2:
            continue
        entry = market.opens[index] * (1 + side * adverse)
        quantity = balance * allocation / (entry * (1 + FEE))
        entry_fee = quantity * entry * FEE
        position = (market, index, entry, side, quantity, balance)
        last_entry_day = timestamp.date()
        balance -= entry_fee
        fees += entry_fee
    return {
        "initial_equity": str(INITIAL), "end_equity": str(balance),
        "net_return": str(balance / INITIAL - 1), "maximum_drawdown": str(drawdown),
        "fees": str(fees), "modeled_funding_reserve": str(funding), "trades": trade_count,
        "entry_or_exit_legs_on_zero_volume_bars": zero_volume_fills,
        "sides": sides, "weekly_marks": week_marks, "weekly": weekly_summary(week_marks),
        **({"episodes": episodes} if keep_trades else {}),
    }


def weekly_summary(marks: list[tuple[str, str]]) -> dict:
    returns = []
    for (_, before), (_, after) in zip(marks, marks[1:]):
        if Decimal(before) > 0:
            returns.append(Decimal(after) / Decimal(before) - 1)
    return {
        "complete_weeks": len(returns), "positive_weeks": sum(value > 0 for value in returns),
        "unscorable_weeks": len(marks) - 1 - len(returns) if marks else 0,
        "median": str(median(returns)) if returns else None,
        "best": str(max(returns)) if returns else None,
        "worst": str(min(returns)) if returns else None,
        "target_hits": {str(target): sum(value >= target for value in returns) for target in TARGETS},
    }


def _paired(market_data, start, step, candidate, first, end, *, keep_trades=False, selective=False):
    return {
        name: replay(market_data, start, step, candidate, first, end,
                     adverse=adverse, funding_rate=funding,
                     keep_trades=keep_trades and name == "base", selective=selective)
        for name, adverse, funding in (
            ("base", BASE_ADVERSE, FUNDING_RESERVE_PER_DAY),
            ("stress", STRESS_ADVERSE, STRESS_FUNDING_RESERVE_PER_DAY),
        )
    }


def _admitted(scores: dict, *, minimum_trades: int) -> bool:
    return all(score["trades"] >= minimum_trades and Decimal(score["net_return"]) > 0
               and Decimal(score["maximum_drawdown"]) <= MAX_DRAWDOWN
               for score in scores.values())


def _aligned_week_cuts(start: datetime, count: int, step: int) -> tuple[int, ...]:
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=midnight.weekday())
    if monday < start:
        monday += WEEK
    first = (monday - start) // timedelta(minutes=step)
    bars_per_week = WEEK // timedelta(minutes=step)
    # The terminal Monday mark requires an actual candle open at the boundary.
    weeks = (count - 1 - first) // bars_per_week
    if weeks < 20:
        raise ValueError("at least 20 complete UTC weeks are required")
    return tuple(first + index * bars_per_week for index in (0, weeks * 3 // 5, weeks * 4 // 5, weeks))


def evaluate(inputs: dict[str, list[dict]], *, step: int, selective: bool = False) -> dict:
    """Lock one horizon per specialist on TRAIN before scoring later partitions."""
    start, markets = prepare(inputs, step=step)
    if selective and step != 15:
        raise ValueError("selective confirmation requires the M15 weekly universe")
    count = len(markets[0].opens)
    with localcontext() as ctx:
        ctx.prec = 36
        if step in (1, 5):
            smoke_markets = tuple(market for market in markets if market.symbol == "SOL_PERP")
            smoke = [
                {"hold_minutes": item.hold_minutes,
                 "results": _paired(smoke_markets, start, step, item, 0, count - 1, keep_trades=True)}
                for item in MINUTE_SMOKE
            ]
            report = {"mode": "SHORT_HISTORY_SMOKE", "traded_universe": ["SOL_PERP"],
                      "candidates": smoke, "final_evaluated": False}
        else:
            first, train_end, development_end, last = _aligned_week_cuts(start, count, step)
            agents = []
            for family in (INTRADAY, SWING):
                train = [(item, _paired(markets, start, step, item, first, train_end,
                                        selective=selective))
                         for item in family]
                eligible = [(item, score) for item, score in train if _admitted(score, minimum_trades=24)]
                selected = max(eligible, key=lambda row: Decimal(row[1]["stress"]["net_return"]),
                               default=None)
                row = {"agent": family[0].agent,
                       "train": [{"hold_minutes": item.hold_minutes, "results": score}
                                 for item, score in train],
                       "selected_hold_minutes": None, "development": None, "final": None}
                if selected:
                    chosen, _ = selected
                    row["selected_hold_minutes"] = chosen.hold_minutes
                    dev = _paired(markets, start, step, chosen, train_end, development_end,
                                  selective=selective)
                    row["development"] = dev
                    if _admitted(dev, minimum_trades=8):
                        row["final"] = _paired(markets, start, step, chosen, development_end, last,
                                               keep_trades=True, selective=selective)
                        row["final_positive_under_stress"] = _admitted(row["final"], minimum_trades=8)
                agents.append(row)
            report = {"mode": "CHRONOLOGICAL_WEEKLY_SCREEN", "agents": agents,
                      "cut_open_utc": [(start + timedelta(minutes=step * i)).isoformat()
                                       for i in (first, train_end, development_end, last)],
                      "final_evaluated": any(row["final"] is not None for row in agents)}
            portfolio = {}
            for scenario in ("base", "stress"):
                finals = [row["final"][scenario] for row in agents if row["final"] is not None]
                if finals:
                    combined = [(marks[0][0], str(sum(Decimal(mark[1]) for mark in marks)
                                                   + INITIAL * (len(agents) - len(finals))))
                                for marks in zip(*(value["weekly_marks"] for value in finals), strict=True)]
                    portfolio[scenario] = {
                        "active_sleeves": len(finals), "weekly": weekly_summary(combined),
                        "net_return": str(Decimal(combined[-1][1]) / (INITIAL * len(agents)) - 1),
                    }
            report["final_portfolio"] = portfolio or None
    report.update({"schema": "autotrade-two-agent-weekly-v1", "bar_minutes": step,
                   "zero_volume_bars": {market.symbol: len(market.zero_volume_indices)
                                        for market in markets},
                   "bars_per_market": count, "fee_per_fill": str(FEE),
                   "base_adverse_per_fill": str(BASE_ADVERSE),
                   "stress_adverse_per_fill": str(STRESS_ADVERSE),
                   "funding_assumption": "ADVERSE_PRO_RATA_RESERVE_BOTH_SIDES_NOT_SETTLED_FUNDING",
                   "funding_reserve_per_day": str(FUNDING_RESERVE_PER_DAY),
                   "stress_funding_reserve_per_day": str(STRESS_FUNDING_RESERVE_PER_DAY),
                   "execution_evidence": "CANDLE_OPEN_PROXY_NO_BOOK_OR_FILL_CONFIRMATION",
                   "zero_volume_signal_bars": "EXCLUDED_USING_COMPLETED_BAR_ONLY",
                   "evidence_class": "RETROSPECTIVE_SIMULATION",
                   "cash_baseline_weekly_return": "0",
                   "paid_model_calls": 0, "network_orders": False, "automatic_promotion": False})
    if selective:
        report["selective_risk_policy"] = {
            "capital_fraction": "0.25", "maximum_entries_per_utc_day": 1,
            "minimum_aligned_markets": 2, "top_signal_minimum_floor_multiple": 2,
        }
    report["result_sha256"] = digest(report)
    return report
