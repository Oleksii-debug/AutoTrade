"""Predeclared range trades on completed daily signals, evaluated on hourly spot candles."""

from hashlib import sha256
from itertools import pairwise
import json
from statistics import median
from math import prod
from source import CODE, ROOT, load_venue, stamp
from replay import HOUR, DAY, WEEK, windows, when

CONFIG = json.loads((CODE / "range-protocol.json").read_bytes())


def visits(values):
    return sum(now and not before for before, now in zip([False] + values, values))


def signal(closes):
    reference, yesterday = closes[:-1], closes[-1]
    ordered = sorted(reference)
    floor, ceiling = ordered[6], ordered[35]
    width = ceiling - floor
    if (
        not 0.08 <= width / floor <= 0.35
        or abs(median(reference[-14:]) / median(reference[:14]) - 1) >= 0.08
    ):
        return None
    lower = floor + width / 3
    return dict(
        lower=lower,
        ready=(
            floor <= yesterday <= lower
            and yesterday > reference[-1]
            and visits([x <= floor + 0.25 * width for x in reference]) >= 2
            and visits([x >= floor + 0.75 * width for x in reference]) >= 2
        ),
        target=floor + 0.85 * width,
        stop=floor * 0.97,
        floor=floor,
    )


def resting_exit(bar, target, stop, slip):
    op, high, low, _, volume = bar
    if volume <= 0:
        return None
    if low <= stop:
        return min(op, stop), "stop"
    if high > target * (1 + slip):
        return target, "target"
    return None


def sleeve(bars, lo, hi, slip, passive=False):
    cash, quantity, fee = 1000.0, 0.0, 0.001
    history, trades = [], []
    armed = True
    for t in range(lo, hi + 1, HOUR):
        bar = bars[t]
        op, _, _, _, volume = bar
        history.append(cash + quantity * op * (1 - slip) * (1 - fee))
        order = None
        if quantity and (t == hi or (not passive and t - entry >= 14 * DAY)):
            order = op, "terminal" if t == hi else "time"
        elif not quantity and t != hi and t % DAY == HOUR:
            if passive:
                enter = t == lo
            else:
                closes = [bars[t - 2 * HOUR - i * DAY][3] for i in range(42, -1, -1)]
                setup = signal(closes)
                # Only a close outside the old entry zone rearms a closed trade.
                if not armed and closes[-1] > last_lower:
                    armed = True
                enter = bool(
                    armed
                    and setup
                    and setup["ready"]
                    and setup["floor"] <= op <= setup["lower"]
                    and setup["target"] / (op * (1 + slip)) - 1 >= 0.05
                )
            if enter:
                if volume <= 0:
                    raise ValueError("Entry price lacks activity")
                entry, cost = t, cash
                quantity = cash / (op * (1 + slip) * (1 + fee))
                cash = 0.0
                if not passive:
                    target, stop, last_lower = (
                        setup["target"],
                        setup["stop"],
                        setup["lower"],
                    )
        if quantity and not passive and order is None:
            order = resting_exit(bar, target, stop, slip)
        if order:
            if volume <= 0:
                raise ValueError("Exit price lacks activity")
            price, reason = order
            cash = quantity * price * (1 - slip) * (1 - fee)
            trades.append(
                dict(
                    entry=when(entry),
                    bar=when(t),
                    exit_reason=reason,
                    invested_net=cash / cost - 1,
                )
            )
            quantity = 0.0
            armed = False
    return history, trades


def portfolio(panel, lo, hi, slip, passive):
    series, ledger = [], {}
    for market, bars in panel.items():
        marks, ledger[market] = sleeve(bars, lo, hi, slip, passive)
        series.append(marks)
    equity = [sum(m) / len(series) for m in zip(*series, strict=True)]
    peak = equity[0]
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    weekly = [b / a - 1 for a, b in pairwise(equity[::168])]
    net = equity[-1] / equity[0] - 1
    assert abs(prod(1 + r for r in weekly) - 1 - net) < 1e-10
    return dict(
        weeks=len(weekly),
        net=net,
        geo_week=(1 + net) ** (1 / len(weekly)) - 1,
        median_week=median(weekly),
        weeks_hit=sum(r >= 0.02 for r in weekly),
        drawdown=drawdown,
        weekly=weekly,
        trades=sum(map(len, ledger.values())),
        ledger=ledger,
    )


def run(name, panel):
    common = set.intersection(*(set(x) for x in panel.values()))
    dates = [stamp(d + "T01:00") for d in CONFIG["dates"]]
    result = []
    for first, last in pairwise(dates):
        segments = list(windows(common, first, last, warmup=43))
        if not segments:
            result.append(None)
            continue
        lo, hi = max(segments, key=lambda pair: pair[1] - pair[0])
        scores = {
            case: {
                arm: portfolio(panel, lo, hi, slip, arm == "passive")
                for arm in ("swing", "passive")
            }
            for case, slip in (("base", 0.0005), ("stress", 0.0015))
        }
        s = scores["stress"]["swing"]
        passed = (
            lo == first
            and hi == last
            and s["geo_week"] >= 0.02
            and s["median_week"] >= 0.02
            and s["drawdown"] <= 0.25
            and s["net"] > max(0, scores["stress"]["passive"]["net"])
        )
        result.append(dict(start=when(lo), end=when(hi), scores=scores, passed=passed))
        print(
            name,
            when(lo)[:10],
            s["weeks"],
            s["trades"],
            s["weeks_hit"],
            *(
                round(100 * s[k], 2)
                for k in ("net", "median_week", "geo_week", "drawdown")
            ),
            flush=True,
        )
    return result


if __name__ == "__main__":
    panel = {m: load_venue("WhiteBIT", m) for m in CONFIG["primary_markets"]}
    primary = run("basket", panel)
    scores = {
        "primary": primary,
        "passed": all(x and x["passed"] for x in primary[1:]),
        "protocol_sha256": sha256(
            (CODE / "range-protocol.json").read_bytes()
        ).hexdigest(),
        "exploratory_whitebit_ton": run("TON", {"GRAM_USDT": load_venue("WhiteBIT")}),
        "exploratory_bybit_ton": run("TON-Bybit", {"GRAMUSDT": load_venue("Bybit")}),
    }
    (ROOT / "range-results.json").write_text(
        json.dumps(scores, indent=2), encoding="utf-8"
    )
