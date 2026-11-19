import argparse
import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path
from random import Random
from statistics import median

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from tools.screen_whitebit_trend_regime import authenticated_bars
from tools.whitebit_historical_klines import collect_whitebit_history

from .accounts import PROTOCOL, Account, configuration

DAY = timedelta(days=1)


def simulate(series, first, last, arm, slip, config):
    fee = D(str(config["fee_each_side"]))
    stake = D(str(config["stake_at_entry"]))
    account = Account(D(config["capital"]))
    peak = account.cash
    drawdown = D(0)
    entered = None
    trades, marks = [], []
    day = first
    while day <= last:
        row = series[day]
        price = D(row["open"])
        if day.weekday() == 0:
            # Saturday's close was available for an entire day before this Monday.
            if arm == "cash" or day == last:
                active = False
            elif arm == "passive":
                active = True
            else:
                recent, prior = (series[day - n * DAY] for n in (2, 9))
                if min(D(recent["volume"]), D(prior["volume"])) <= 0:
                    raise ValueError("Missing signal activity")
                move = D(recent["close"]) - D(prior["close"])
                active = move > 0 if arm == "momentum" else move < 0
            was_active = bool(account.quantity)
            if active != was_active and D(row["volume"]) <= 0:
                raise ValueError("Missing execution activity")
            if active and not was_active:
                entry_cash = account.cash
                entered = day
            account.update(active, price, fee, slip, stake)
            if was_active and not active:
                trades.append(
                    {
                        "entry": str(entered),
                        "exit": str(day),
                        "net": str(account.cash - entry_cash),
                    }
                )
        equity = account.liquidation(price, fee, slip)
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
        if day.weekday() == 0:
            marks.append(equity if day > first else D(config["capital"]))
        day += DAY
    returns = [b / a - 1 for a, b in itertools.pairwise(marks)]
    assert not account.quantity and len(returns) == (last - first).days // 7
    return {
        "net": str(account.cash / D(config["capital"]) - 1),
        "fees": str(account.fees),
        "drawdown_daily_marks": str(drawdown),
        "weeks": len(returns),
        "median_week": str(median(returns)),
        "weeks_3pct": sum(x >= D(".03") for x in returns),
        "weeks_5pct": sum(x >= D(".05") for x in returns),
        "trades": trades,
        "weekly_fractions": list(map(str, returns)),
    }


def uncertainty(arms):
    # Same 4-week blocks for each arm preserve paired market exposure in each sample.
    from math import expm1, log1p

    logs = {
        arm: [log1p(float(x)) for x in arms[arm]["weekly_fractions"]]
        for arm in ("momentum", "passive")
    }
    n = len(logs["momentum"])
    rng = Random(20261120)
    net, excess = [], []
    for _ in range(5000):
        indices = []
        while len(indices) < n:
            block = rng.randrange(n)
            indices.extend((block + k) % n for k in range(4))
        m, p = (expm1(sum(values[j] for j in indices[:n])) for values in logs.values())
        net.append(m)
        excess.append(m - p)
    return {
        key: [a[124], a[4874]]
        for key, a in (
            ("net_interval", sorted(net)),
            ("excess_vs_passive_interval", sorted(excess)),
        )
    }


def main(output):
    cfg = configuration()
    cuts = [
        [date.fromisoformat(value) for value in cut]
        for cut in cfg["partitions_monday_to_monday_utc"]
    ]
    start, end = [
        int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
        for day in (cuts[0][0] - 9 * DAY, cuts[-1][-1])
    ]
    store = output / "history"
    manifest = (
        store
        / "spot"
        / cfg["market"]
        / cfg["interval"]
        / f"{start}-{end}.manifest.json"
    )
    if not manifest.exists():
        collect_whitebit_history(
            market=cfg["market"],
            interval=cfg["interval"],
            start=start,
            end=end,
            output=store,
            page_limit=1000,
            max_attempts=2,
        )
    bars, ident, meta = authenticated_bars(manifest)
    if meta["market"] != cfg["market"] or len(bars) != (end - start) // 86400 + 1:
        raise ValueError("Source does not match the registered sample")
    series = {row["opened_at"].date(): row for row in bars}
    out = {
        "protocol_sha256": PROTOCOL,
        "dataset_sha256": ident,
        "bars": len(bars),
        "source_manifest": str(manifest),
        "partitions": {},
        "orders": 0,
    }
    for first, last in cuts:
        group = out["partitions"][str(first)] = {}
        for case in ("base", "stress"):
            slip = D(str(cfg["adverse_price_each_side_" + case]))
            arms = group[case] = {
                arm: simulate(series, first, last, arm, slip, cfg)
                for arm in ("momentum", "passive", "opposite", "cash")
            }
            arms["passes_comparators"] = all(
                D(arms["momentum"]["net"]) > D(arms[x]["net"])
                for x in ("cash", "passive", "opposite")
            )
            arms["paired_four_week_bootstrap"] = uncertainty(arms)
    atomic_write_json(output / "receipt.json", out)
    for key, p in out["partitions"].items():
        for case, a in p.items():
            print(
                key,
                case,
                {
                    arm: round(100 * D(a[arm]["net"]), 3)
                    for arm in ("momentum", "passive", "opposite")
                },
                len(a["momentum"]["trades"]),
                a["momentum"]["weeks_3pct"],
                a["momentum"]["weeks"],
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    with localcontext() as ctx:
        ctx.prec = 36
        main(parser.parse_args().output.resolve())
