"""Walk completed hourly candles with prior-day entry decisions and resting profit orders."""

import json
from datetime import UTC, datetime
from itertools import pairwise
from math import prod
from statistics import median
from hashlib import sha256
from source import CODE, ROOT, load_venue, stamp

HOUR = 3600
DAY = HOUR * 24
WEEK = DAY * 7
MONDAY = stamp("2024-01-01T01:00")
CONFIG = json.loads((CODE / "protocol.json").read_bytes())


def when(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


def windows(bars, first, last, warmup=60):
    times = sorted(bars)
    split = [a for a, b in pairwise(times) if b - a != HOUR]
    starts = [times[0]] + [b for a, b in pairwise(times) if b - a != HOUR]
    for lo, hi in zip(starts, split + [times[-1]], strict=True):
        lo = max(first, lo + warmup * DAY)
        hi = min(last, hi)
        start = lo + (MONDAY - lo) % WEEK
        end = hi - (hi - MONDAY) % WEEK
        if start < end:
            yield start, end


def replay(bars, lo, hi, arm, slip):
    name = arm["name"]
    fee = CONFIG["costs"]["fee_each_side"]
    cash = peak = float(CONFIG["capital"])
    worst = qty = 0.0
    armed = True
    exited = lo
    trades, values = [], []
    decision_days = 0
    for t in range(lo, hi + 1, HOUR):
        op, high, _, _, volume = bars[t]
        equity = cash + qty * op * (1 - slip) * (1 - fee)
        peak = max(peak, equity)
        worst = max(worst, 1 - equity / peak)
        if (t - lo) % WEEK == 0:
            values.append(equity)
        scheduled = t % DAY == HOUR
        exit_price, reason = None, None
        if qty:
            max_days = arm.get("max_days")
            if t == hi:
                exit_price, reason = op, "terminal"
            elif scheduled and max_days and t - entry >= max_days * DAY:
                exit_price, reason = op, "time"
            elif name != "passive" and volume > 0 and high > target * (1 + slip):
                exit_price, reason = target, "target"
        if exit_price is not None:
            if volume <= 0:
                raise ValueError("Unobservable market liquidation")
            cash = qty * exit_price * (1 - slip) * (1 - fee)
            qty = 0.0
            exited = t + (HOUR if reason == "target" else 0)
            trades.append(
                dict(
                    entry=when(entry),
                    exit=when(exited),
                    entry_fill=paid,
                    exit_fill=exit_price * (1 - slip),
                    reason=reason,
                    net=cash / cost - 1,
                    days=(exited - entry) / DAY,
                    wait_days=wait,
                )
            )
            armed = False
            continue
        if not scheduled or qty or t == hi:
            continue
        if name == "passive":
            enter = t == lo
        else:
            # At 01:00, the 23:00 bar from yesterday closed one hour ago.
            prior = [bars[t - 2 * HOUR - i * DAY][3] for i in range(60)]
            drop = 1 - prior[0] / max(prior[1:15])
            if drop < arm["drop"]:
                armed = True
            deep = drop >= arm["drop"]
            enter = (
                armed
                and deep
                and (
                    not arm["trend"]
                    or (prior[0] > sum(prior) / 60 and prior[0] > prior[1])
                )
            )
            decision_days += 1
        if enter:
            if volume <= 0:
                raise ValueError("Unobservable market entry")
            entry, cost, paid, wait = t, cash, op * (1 + slip), (t - exited) / DAY
            qty = cash / (paid * (1 + fee))
            cash = 0.0
            target = paid * (1 + arm.get("target", 0.0))
    assert not qty and abs(values[-1] - cash) < 1e-8
    return dict(
        start=when(lo),
        end=when(hi),
        trades=trades,
        drawdown=worst,
        weekly=[b / a - 1 for a, b in pairwise(values)],
        decision_days=decision_days,
    )


def stats(segments):
    weekly = [w for s in segments for w in s["weekly"]]
    trades = [t for s in segments for t in s["trades"]]
    if not weekly:
        return None
    factor = prod(1 + w for w in weekly)
    assert abs(factor - prod(1 + t["net"] for t in trades)) < 1e-7 * max(1.0, factor)
    return dict(
        weeks=len(weekly),
        net=factor - 1,
        geo_week=factor ** (1 / len(weekly)) - 1,
        median_week=median(weekly),
        weeks_hit=sum(w >= 0.02 for w in weekly),
        worst_week=min(weekly),
        max_segment_drawdown=max(s["drawdown"] for s in segments),
        trades=len(trades),
        targets=sum(t["reason"] == "target" for t in trades),
        profitable=sum(t["net"] > 0 for t in trades),
        longest_days=max((t["days"] for t in trades), default=0),
        median_invested_net=median(t["net"] for t in trades) if trades else None,
        median_wait_days=median(t["wait_days"] for t in trades) if trades else None,
    )


def main():
    phases = [stamp(x + "T01:00") for x in CONFIG["phase_boundaries_monday_utc"]]
    out = {
        "protocol_sha256": sha256((CODE / "protocol.json").read_bytes()).hexdigest(),
        "network_orders": 0,
        "model_api_calls": 0,
        "venues": {},
    }
    for venue in ("Bybit", "WhiteBIT"):
        bars = load_venue(venue)
        result = out["venues"][venue] = {"hourly_candles": len(bars), "phases": []}
        for left, right in pairwise(phases):
            samples = list(windows(bars, left, right))
            phase = {
                "calendar_weeks": (right - left) // WEEK,
                "segments": [[when(a), when(b)] for a, b in samples],
                "scores": {},
            }
            result["phases"].append(phase)
            for case in ("base", "stress"):
                slip = CONFIG["costs"][case + "_adverse_each_side"]
                scores = phase["scores"][case] = {}
                for arm in [*CONFIG["arms"], {"name": "passive"}]:
                    segments = [replay(bars, a, b, arm, slip) for a, b in samples]
                    scores[arm["name"]] = {
                        "summary": stats(segments),
                        "segments": segments,
                    }
    bybit = out["venues"]["Bybit"]["phases"]
    train = bybit[0]["scores"]["stress"]
    ranked = sorted(
        (x for x in train if x != "passive" and train[x]["summary"]["trades"] >= 5),
        key=lambda x: (-train[x]["summary"]["geo_week"], x),
    )
    out["selected_on_first_period"] = ranked[0] if ranked else None

    def qualifies(phase):
        if not ranked:
            return False
        scores = phase["scores"]["stress"]
        s = scores[ranked[0]]["summary"]
        return bool(
            s
            and s["weeks"] == phase["calendar_weeks"]
            and s["trades"] >= 5
            and s["geo_week"] >= 0.02
            and s["median_week"] >= 0.02
            and s["max_segment_drawdown"] <= 0.25
            and s["net"] > max(0, scores["passive"]["summary"]["net"])
        )

    out["passed"] = all(qualifies(p) for p in bybit[1:])
    (ROOT / "results.json").write_text(
        json.dumps(out, indent=2, allow_nan=False), encoding="utf-8"
    )
    print("selected", out["selected_on_first_period"], "qualified", out["passed"])
    for venue, r in out["venues"].items():
        for phase in r["phases"]:
            for arm, e in phase["scores"]["stress"].items():
                s = e["summary"]
                if s:
                    print(
                        venue,
                        phase["segments"][0][0][:10],
                        arm,
                        s["weeks"],
                        s["trades"],
                        s["targets"],
                        *(
                            round(100 * s[x], 2)
                            for x in (
                                "net",
                                "geo_week",
                                "median_week",
                                "max_segment_drawdown",
                            )
                        ),
                        s["weeks_hit"],
                        round(s["longest_days"], 1),
                    )


if __name__ == "__main__":
    main()
