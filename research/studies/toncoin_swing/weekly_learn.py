"""Replay the weekly spot choices in vintage order, with a full embargo week."""

from hashlib import sha256
from itertools import pairwise
import json
from statistics import median

from source import ROOT, CODE, load_venue, stamp
from replay import DAY, HOUR, WEEK, when
from tools.whitebit_historical_klines import collect_whitebit_history
from tools.screen_whitebit_trend_regime import authenticated_bars

ACTIONS = ("CASH", "OPEN", "DIP")
FEE = 0.001
CUTS = (stamp("2024-11-18T01:00"), stamp("2025-11-17T01:00"), stamp("2026-11-16T01:00"))


def features(bars, first):
    days = [first - 2 * HOUR - i * DAY for i in range(28)]
    if not all(d in bars for d in days):
        return None
    c = [bars[d][3] for d in days]
    span = max(c) - min(c)
    return [
        10 * (c[0] / c[7] - 1),
        5 * (c[0] / c[-1] - 1),
        (c[0] - min(c)) / span if span else 0.5,
        sum(abs(c[i] / c[i + 1] - 1) for i in range(7)) / (7 * 0.03),
    ]


def simulate(bars, first, action, slip):
    cash, units = 1.0, 0.0
    limit = bars[first][0] * 0.97
    used = False
    curve = []
    reason = "CASH" if action == "CASH" else "NO_FILL"
    for t in range(first, first + WEEK + 1, HOUR):
        op, high, low, _, volume = bars[t]
        curve.append(cash + units * op * (1 - slip) * (1 - FEE))
        if t == first + WEEK:
            if units:
                if volume <= 0:
                    raise ValueError("No volume at scheduled exit")
                cash = curve[-1]
                reason = "DEADLINE"
            break
        if not used and action != "CASH":
            at_open = action == "OPEN" and t == first
            dip = (
                action == "DIP"
                and t < first + 3 * DAY
                and volume > 0
                and low < limit * (1 - slip)
            )
            if at_open or dip:
                if volume <= 0:
                    raise ValueError("No volume at scheduled buy")
                entry = (op if at_open else limit) * (1 + slip)
                entry_time = t
                units = cash / (entry * (1 + FEE))
                cash = 0.0
                used = True
                target, stop = entry * 1.05, entry * 0.97
        if not units or volume <= 0:
            continue
        if low <= stop:
            exit_quote = min(op, stop)
            reason = "STOP"
        elif (action == "OPEN" or t > entry_time) and high > target * (1 + slip):
            exit_quote = target
            reason = "TARGET"
        else:
            continue
        cash = units * exit_quote * (1 - slip) * (1 - FEE)
        units = 0.0
    return dict(net=cash - 1, reason=reason, curve=curve)


def choose(t, f, memory):
    eligible = [
        r for r in memory if t - 104 * WEEK <= r["time"] and r["time"] + WEEK < t
    ]
    if len(eligible) < 24:
        return "CASH", [], {}

    def rank(r):
        return sum(abs(a - b) for a, b in zip(r["features"], f, strict=True)), -r[
            "time"
        ]

    matched = sorted(eligible, key=rank)[:12]
    scores = {}
    for action in ("DIP", "OPEN"):
        values = [r["settled"][action] for r in matched]
        scores[action] = (sum(values) - max(values)) / 13
    best = max(scores, key=scores.get)
    return (
        best if scores[best] > 0.005 else "CASH",
        [r["time"] for r in matched],
        scores,
    )


def summarize(episodes, first, end, case, arm):
    eq = peak = 1.0
    dd = 0.0
    net = []
    for e in episodes:
        if not first <= e["time"] < end:
            continue
        p = e["outcomes"][case][e["choice"] if arm == "learned" else arm]
        for n in p["curve"]:
            mark = eq * n
            peak = max(mark, peak)
            dd = max(dd, 1 - mark / peak)
        eq *= 1 + p["net"]
        net.append(p["net"])
    if not net:
        return None
    return dict(
        weeks=len(net),
        net=eq - 1,
        geo=(eq ** (1 / len(net)) - 1),
        median=median(net),
        hit=sum(r >= 0.02 for r in net),
        worst=min(net),
        drawdown_complete_observations=dd,
        nonzero=sum(r != 0 for r in net),
    )


def evaluate(bars):
    memory = []
    episodes = []
    # Jan 1 2024 was a Monday. Start on or after the earliest archived Monday.
    origin = stamp("2024-01-01T01:00")
    first = min(bars) + (origin - min(bars)) % WEEK
    for t in range(first, CUTS[-1], WEEK):
        feature = features(bars, t)
        if feature is None or not all(x in bars for x in range(t, t + WEEK + 1, HOUR)):
            continue
        choice, matched, scores = choose(t, feature, memory)
        arms = {
            case: {a: simulate(bars, t, a, slip) for a in ACTIONS}
            for case, slip in (("base", 0.0005), ("stress", 0.0015))
        }
        memory.append(
            dict(
                time=t,
                features=feature,
                settled={a: p["net"] for a, p in arms["stress"].items()},
            )
        )
        episodes.append(
            dict(
                time=t,
                features=feature,
                choice=choice,
                training=matched,
                robust_scores=scores,
                outcomes=arms,
            )
        )
    phases = []
    for a, b in pairwise(CUTS):
        data = {
            case: {
                arm: summarize(episodes, a, b, case, arm)
                for arm in ("learned", *ACTIONS)
            }
            for case in ("base", "stress")
        }
        s = data["stress"]["learned"]
        passed = bool(
            s
            and s["weeks"] == (b - a) // WEEK
            and s["geo"] >= 0.02
            and s["median"] >= 0.02
            and s["drawdown_complete_observations"] <= 0.25
            and s["net"] > max(data["stress"][x]["net"] for x in ACTIONS)
        )
        phases.append(dict(begin=when(a), end=when(b), passed=passed, results=data))
    for e in episodes:
        for arms in e["outcomes"].values():
            for outcome in arms.values():
                del outcome["curve"]
    return dict(phases=phases, episodes=episodes)


def main():
    collection = ROOT / "weekly-learning-data"
    start = stamp("2023-10-01")
    end = CUTS[-1]
    path = collection / "spot/XLM_USDT/1h" / f"{start}-{end}.manifest.json"
    if not path.exists():
        collect_whitebit_history(
            market="XLM_USDT",
            interval="1h",
            start=start,
            end=end,
            output=collection,
            page_limit=1000,
            max_attempts=2,
        )
    rows, identity, meta = authenticated_bars(path)
    if meta["market"] != "XLM_USDT":
        raise ValueError("Wrong transfer market")
    xlm = {
        int(r["opened_at"].timestamp()): tuple(
            float(r[k]) for k in ("open", "high", "low", "close", "volume")
        )
        for r in rows
    }
    report = dict(
        protocol=sha256((CODE / "weekly-learning.json").read_bytes()).hexdigest(),
        xlm_dataset=identity,
        markets={},
        exchange_orders=0,
        paid_model_calls=0,
    )
    for name, bars in (
        ("TON_Bybit", load_venue("Bybit")),
        ("TON_WhiteBIT", load_venue("WhiteBIT")),
        ("XLM_WhiteBIT", xlm),
    ):
        r = evaluate(bars)
        report["markets"][name] = r
        print(
            name,
            [(p["results"]["stress"]["learned"], p["passed"]) for p in r["phases"]],
        )
    (ROOT / "weekly-learning-results.json").write_text(
        json.dumps(report, allow_nan=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
