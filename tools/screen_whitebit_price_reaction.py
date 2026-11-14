"""Screen slower ETH/SOL reactions to BTC shocks on authenticated WhiteBIT M15."""

from argparse import ArgumentParser
from collections import Counter
from datetime import timedelta
from decimal import Decimal, localcontext
import json
from pathlib import Path

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.strategies.fast_screen import decimal, digest
from research.autotrade_research.strategies.intraday_absorption import Candle, Signal, _replay
from research.autotrade_research.strategies.two_agent_weekly import (
    BASE_ADVERSE, FUNDING_RESERVE_PER_DAY, MAX_DRAWDOWN, STRESS_ADVERSE,
    STRESS_FUNDING_RESERVE_PER_DAY, _aligned_week_cuts, prepare,
)
from tools.screen_whitebit_trend_regime import authenticated_bars


PROTOCOL_SHA256 = "16f589236cd5909607a617419ed8b62af80a31559d20df890adf0a874d83ce2c"
BTC = "BTC_PERP"
ETH = "ETH_PERP"
SOL = "SOL_PERP"
MARKETS = (BTC, ETH, SOL)
HOLDS = (30, 90, 240)
STRESS = (STRESS_ADVERSE, STRESS_FUNDING_RESERVE_PER_DAY)
BASE = (BASE_ADVERSE, FUNDING_RESERVE_PER_DAY)


def select_follower(btc_move, historical_rms, eth_move, sol_move):
    if abs(btc_move) < max(Decimal("0.01"), Decimal("2.5") * historical_rms):
        return None, "NO_BTC_SHOCK"
    side = 1 if btc_move > 0 else -1
    moves = {ETH: side * eth_move, SOL: side * sol_move}
    lagger, faster = sorted(moves, key=moves.get)
    if moves[lagger] <= 0:
        return None, "FOLLOWERS_DISAGREE"
    if moves[lagger] > abs(btc_move) / 2 or moves[lagger] * 3 > 2 * moves[faster]:
        return None, "NO_PARTIAL_REACTION"
    return (lagger, side), "SIGNAL"


def prior_atr(bars, shock_start):
    lookback = bars[shock_start - 14:shock_start + 1]
    return sum(max(right.high - right.low, abs(right.high - left.close), abs(right.low - left.close))
               for left, right in zip(lookback, lookback[1:])) / 14


def entries_by_clock(markets, begin, end):
    counts = Counter()
    entries = {}
    for entry in range(begin + 70, end):
        latest = entry - 2
        shock_start = latest - 4
        if any(markets[name][latest].volume <= 0 for name in MARKETS):
            counts["ZERO_SIGNAL_VOLUME"] += 1
            continue
        btc = markets[BTC]
        btc_return = btc[latest].close / btc[shock_start].close - 1
        if abs(btc_return) < Decimal("0.01"):
            counts["NO_BTC_SHOCK"] += 1
            continue
        trailing = (btc[clock].close / btc[clock - 4].close - 1
                    for clock in range(shock_start - 60, shock_start + 1, 4))
        rms = (sum(ret * ret for ret in trailing) / 16).sqrt()
        follower, reason = select_follower(
            btc_return, rms,
            markets[ETH][latest].close / markets[ETH][shock_start].close - 1,
            markets[SOL][latest].close / markets[SOL][shock_start].close - 1,
        )
        if follower is None:
            counts[reason] += 1
            continue
        lagger, side = follower
        lag_atr, btc_atr = prior_atr(markets[lagger], shock_start), prior_atr(btc, shock_start)
        if (lag_atr <= 0 or btc_atr <= 0 or markets[lagger][entry].opened <= 2 * lag_atr
                or btc[entry].opened <= 2 * btc_atr):
            counts["INVALID_STOP_PRICE"] += 1
            continue
        lag_close = markets[lagger][latest].close
        entries[entry] = {
            "candidate": (lagger, Signal(side, lag_close, lag_atr)),
            "follower_opposite": (lagger, Signal(-side, lag_close, lag_atr)),
            "btc_continuation": (BTC, Signal(side, btc[latest].close, btc_atr)),
        }
        counts["SIGNAL"] += 1
    return entries, dict(counts)


def partition(markets, start, begin, end, horizons):
    entries, counts = entries_by_clock(markets, begin, end)
    results = {}
    for hold in horizons:
        next_entry = begin
        scheduled = {name: {} for name in ("candidate", "follower_opposite", "btc_continuation")}
        for clock, arms in entries.items():
            exit_clock = clock + hold // 15
            if clock < next_entry or exit_clock >= end:
                continue
            for name, entry in arms.items():
                scheduled[name][clock] = entry
            next_entry = exit_clock + 1
        results[hold] = {
            cost: {name: _replay(markets, start, begin, end, arm, hold, adverse, funding)
                   for name, arm in scheduled.items()}
            for cost, (adverse, funding) in (("base", BASE), ("stress", STRESS))
        }
    return {"filter_counts": counts, "horizons": results}


def rejection_reasons(costs, minimum_count):
    reasons = []
    for name, arms in costs.items():
        candidate = arms["candidate"]
        net = Decimal(candidate["net_return"])
        if candidate["trades"] < minimum_count:
            reasons.append(f"{name}:INSUFFICIENT_TRADES")
        if net <= 0:
            reasons.append(f"{name}:NONPOSITIVE_NET")
        if any(net <= Decimal(arms[control]["net_return"])
               for control in ("follower_opposite", "btc_continuation")):
            reasons.append(f"{name}:NO_MATCHED_CONTROL_EDGE")
        if Decimal(candidate["maximum_drawdown"]) > MAX_DRAWDOWN:
            reasons.append(f"{name}:DRAWDOWN_LIMIT")
        if any(arm["zero_volume_fill_legs"] for arm in arms.values()):
            reasons.append(f"{name}:ZERO_VOLUME_PROXY_LEG")
    return reasons


def evaluate(inputs, sources):
    start, _ = prepare(inputs, step=15)
    markets = {name: [Candle(*(decimal(bar[key]) for key in ("open", "high", "low", "close", "volume")))
                      for bar in inputs[name]] for name in MARKETS}
    first, train_end, dev_end, terminal = _aligned_week_cuts(start, len(markets[BTC]), 15)
    with localcontext() as context:
        context.prec = 36
        train = partition(markets, start, first, train_end, HOLDS)
        train["rejections"] = {hold: rejection_reasons(costs, 24)
                               for hold, costs in train["horizons"].items()}
        selected = max((hold for hold, reasons in train["rejections"].items() if not reasons),
                       key=lambda hold: Decimal(train["horizons"][hold]["stress"]["candidate"]["net_return"]),
                       default=None)
        development = final = None
        if selected is not None:
            development = partition(markets, start, train_end, dev_end, (selected,))
            development["rejections"] = rejection_reasons(development["horizons"][selected], 8)
            if not development["rejections"]:
                final = partition(markets, start, dev_end, terminal, (selected,))
                final["rejections"] = rejection_reasons(final["horizons"][selected], 8)
    result = {
        "schema": "autotrade-cross-asset-price-reaction-v1", "protocol_sha256": PROTOCOL_SHA256,
        "sources": sources, "hold_minutes": HOLDS,
        "cuts_utc": [(start + i * timedelta(minutes=15)).isoformat()
                     for i in (first, train_end, dev_end, terminal)],
        "train": train, "selected_hold_minutes": selected, "development": development, "final": final,
        "execution": "M15_OHLCV_PROXY", "cash_weekly_return": "0",
        "automatic_promotion": False, "order_requests": 0, "paid_model_calls": 0,
    }
    # Normalize integer horizon keys to their JSON representation before hashing.
    result = json.loads(json.dumps(result))
    result["result_sha256"] = digest(result)
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", nargs=3, required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    inputs, sources = {}, {}
    for manifest in args.manifest:
        rows, identity, meta = authenticated_bars(manifest)
        name = meta["market"]
        if meta["interval"] != "15m" or name not in MARKETS or name in inputs:
            parser.error("expected three distinct M15 perpetual market archives")
        inputs[name] = rows
        sources[name] = identity
    result = evaluate(inputs, sources)
    output = args.output_dir / f"{result['result_sha256']}.json"
    atomic_write_json(output, result)
    print(json.dumps({"file": str(output), "result_sha256": result["result_sha256"],
                      "selected_hold_minutes": result["selected_hold_minutes"],
                      "final_evaluated": result["final"] is not None}))


if __name__ == "__main__":
    main()
