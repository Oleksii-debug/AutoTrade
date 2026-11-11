"""Forward-shadow one qualified trend candidate on a WhiteBIT public BBO feed.

No order sender is imported.  Completed UTC-hour candles are built from the
authenticated public BBO stream, decisions execute on the next hour's first
observed bid/ask, and all accounting is restart-safe simulation state.
"""
import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
import json
import os

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.strategies.fast_screen import Costs, decimal
from research.autotrade_research.strategies.trend_regime import TrendRegime
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "autotrade-whitebit-trend-forward-shadow-v1"


def _read_json(path: Path, *, limit=16 * 1024 * 1024):
    raw = path.read_bytes()
    if not raw or len(raw) > limit:
        raise ValueError("shadow input is empty or unbounded")
    return json.loads(raw)


def _qualification(path: Path):
    document = _read_json(path)
    result = document.get("result")
    if (type(result) is not dict or not result.get("prospective_qualification")
            or result.get("selected_candidate") != "TREND_REGIME_TRAILING"
            or result.get("network_orders") is not False
            or result.get("canonical_qualification") is not False):
        raise ValueError("trend shadow requires one simulation-only qualified result")
    source = document.get("source")
    if type(source) is not dict or source.get("market") != "SOL_USDT":
        raise ValueError("trend shadow currently binds the qualified SOL_USDT result")
    config = result.get("config", {})
    costs = Costs(**config.get("costs", {}))
    strategy = TrendRegime(**config.get("configuration", {}))
    costs.validate()
    strategy.validate()
    digest = sha256(path.read_bytes()).hexdigest()
    return costs, strategy, result, digest


def _seed(manifests, required):
    bars = []
    identities = []
    for path in manifests:
        part, digest, manifest = authenticated_bars(path)
        source_first, source_last = part[0], part[-1]
        if bars:
            part = [bar for bar in part if bar["opened_at"] >= bars[-1]["closed_at"]]
            if part and part[0]["opened_at"] != bars[-1]["closed_at"]:
                raise ValueError("WhiteBIT shadow seed manifests are not continuous")
        bars.extend(part)
        identities.append({"dataset_identity_sha256": digest,
                           "first": source_first["opened_at"].isoformat(),
                           "last": source_last["closed_at"].isoformat(),
                           "market": manifest["market"]})
    if len(bars) < required:
        raise ValueError("WhiteBIT shadow seed is shorter than slow lookback")
    if any(item["market"] != "SOL_USDT" for item in identities):
        raise ValueError("WhiteBIT shadow seed market differs")
    return bars[-required:], identities


def _snapshot(path: Path):
    first = path.read_bytes()
    second = path.read_bytes()
    if first != second or not first or len(first) > 1_000_000:
        raise ValueError("WhiteBIT public snapshot changed during read")
    value = json.loads(first)
    if (value.get("schema") != "autotrade-whitebit-public-supervisor-v1"
            or value.get("market") != "SOL_USDT"
            or value.get("venue") != "WHITEBIT_SPOT"
            or value.get("public_data_only") is not True
            or value.get("trading_authority") is not False):
        raise ValueError("WhiteBIT shadow snapshot authority differs")
    bid, ask = decimal(value.get("bid_price")), decimal(value.get("ask_price"))
    if bid <= 0 or ask < bid:
        raise ValueError("WhiteBIT shadow BBO is invalid")
    observed = datetime.fromisoformat(value["source_event_at"].replace("Z", "+00:00")).astimezone(UTC)
    return value, bid, ask, observed


def _new_state(seed, identities, costs, strategy, qualification_sha):
    last = seed[-1]["closed_at"].astimezone(UTC)
    initial = decimal(costs.initial_cash)
    return {"schema": SCHEMA, "status": "STARTING", "market": "SOL_USDT",
        "network_orders": False, "trading_authority": False,
        "qualification_file_sha256": qualification_sha,
        "seed_identities": identities, "configuration": strategy.__dict__,
        "costs": costs.__dict__, "history_closes": [bar["close"] for bar in seed],
        "last_completed_at": last.isoformat(), "current_candle": None,
        "last_source_event_at": None, "last_update_id": None,
        "initial_equity": str(initial), "current_equity": str(initial),
        "net_pnl": "0", "return_fraction": "0",
        "cash": str(initial), "quantity": "0", "fees": "0",
        "financing_costs": "0", "peak_equity": str(initial),
        "max_drawdown_fraction": "0", "favorable_water": None,
        "holding_bars": 0, "closed_cycles": 0, "long_cycles": 0,
        "short_cycles": 0, "completed_live_bars": 0, "last_action": None,
        "last_error": None, "updated_at": datetime.now(UTC).isoformat()}


def _mark(state, price, costs):
    cash, quantity = decimal(state["cash"]), decimal(state["quantity"])
    fee = decimal(costs.fee_bps) / 10000
    if quantity >= 0:
        equity = cash + quantity * price * (1-fee)
    else:
        equity = cash + quantity * price * (1+fee)
    peak = max(decimal(state["peak_equity"]), equity)
    drawdown = max(decimal(state["max_drawdown_fraction"]),
                   ((peak-equity)/peak if peak > 0 else Decimal(1)))
    unit = Decimal("1e-18")
    initial = decimal(state["initial_equity"])
    state["current_equity"] = str(equity.quantize(unit))
    state["net_pnl"] = str((equity-initial).quantize(unit))
    state["return_fraction"] = str(((equity-initial)/initial).quantize(unit))
    state["peak_equity"] = str(peak.quantize(unit))
    state["max_drawdown_fraction"] = str(drawdown.quantize(unit))


def _decision(state, strategy):
    closes = [decimal(value) for value in state["history_closes"]]
    if len(closes) < strategy.slow_lookback + 1:
        return None
    fast = (closes[-1]-closes[-1-strategy.fast_lookback]) / closes[-1-strategy.fast_lookback]
    slow = (closes[-1]-closes[-1-strategy.slow_lookback]) / closes[-1-strategy.slow_lookback]
    quantity = decimal(state["quantity"])
    if quantity == 0:
        if fast >= decimal(strategy.entry_threshold) and slow >= decimal(strategy.slow_threshold):
            return "BUY"
        if (strategy.allow_short and fast <= -decimal(strategy.short_entry_threshold)
                and slow <= -decimal(strategy.short_slow_threshold)):
            return "SHORT"
        return None
    if state["holding_bars"] >= strategy.max_hold_bars:
        return "SELL" if quantity > 0 else "COVER"
    if state["holding_bars"] < strategy.min_hold_bars:
        return None
    close, water = closes[-1], decimal(state["favorable_water"])
    if quantity > 0 and (close <= water*(1-decimal(strategy.trailing_stop_fraction))
                         or fast <= -decimal(strategy.exit_threshold)):
        return "SELL"
    if quantity < 0 and (close >= water*(1+decimal(strategy.trailing_stop_fraction))
                         or fast >= decimal(strategy.exit_threshold)):
        return "COVER"
    return None


def _execute(state, action, bid, ask, costs, observed):
    if action is None:
        return None
    with localcontext() as context:
        context.prec = 50
        cash, quantity = decimal(state["cash"]), decimal(state["quantity"])
        fee = decimal(costs.fee_bps) / 10000
        slip = decimal(costs.slippage_bps) / 10000
        fraction = decimal(costs.capital_fraction)
        if action == "BUY" and quantity == 0:
            price = ask*(1+slip); budget = cash*fraction
            opened = (budget/(price*(1+fee))).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
            value=opened*price; charge=value*fee; cash-=value+charge; quantity=opened
        elif action == "SELL" and quantity > 0:
            price=bid*(1-slip); value=quantity*price; charge=value*fee
            cash+=value-charge; quantity=Decimal(0); state["closed_cycles"]+=1; state["long_cycles"]+=1
        elif action == "SHORT" and quantity == 0:
            price=bid*(1-slip); opened=(cash*fraction/price).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
            value=opened*price; charge=value*fee; cash+=value-charge; quantity=-opened
        elif action == "COVER" and quantity < 0:
            price=ask*(1+slip); value=-quantity*price; charge=value*fee
            cash-=value+charge; quantity=Decimal(0); state["closed_cycles"]+=1; state["short_cycles"]+=1
        else:
            return None
        if cash < 0:
            raise ArithmeticError("forward shadow exhausted simulated cash")
        unit = Decimal("1e-18")
        cash, charge = cash.quantize(unit), charge.quantize(unit)
        state["cash"], state["quantity"] = str(cash), str(quantity)
        state["fees"] = str((decimal(state["fees"])+charge).quantize(unit))
        state["favorable_water"] = (None if quantity == 0 else str(price))
        state["holding_bars"] = 0
        event = {"action": action, "at": observed.isoformat(), "price": str(price),
                 "fee": str(charge), "cash": str(cash), "quantity": str(quantity)}
        state["last_action"] = event
        return event


def advance(state, snapshot, bid, ask, observed, costs, strategy):
    """Advance one BBO observation; returns an hourly action event or None."""
    bucket = observed.replace(minute=0, second=0, microsecond=0)
    mid = (bid+ask)/2
    current = state["current_candle"]
    if current is None:
        if bucket.isoformat() != state["last_completed_at"]:
            raise ValueError("forward shadow has a seed/live continuity gap")
        state["current_candle"] = {"opened_at": bucket.isoformat(), "open": str(mid),
            "high": str(mid), "low": str(mid), "close": str(mid)}
        _mark(state, mid, costs)
        event = None
    else:
        opened = datetime.fromisoformat(current["opened_at"])
        if bucket < opened:
            raise ValueError("forward shadow received reordered hour")
        if bucket == opened:
            current["high"] = str(max(decimal(current["high"]), mid))
            current["low"] = str(min(decimal(current["low"]), mid))
            current["close"] = str(mid)
            _mark(state, mid, costs)
            event = None
        else:
            if bucket != opened + timedelta(hours=1):
                raise ValueError("forward shadow live hourly gap")
            close = decimal(current["close"])
            quantity = decimal(state["quantity"])
            if quantity != 0:
                state["holding_bars"] += 1
                water = decimal(state["favorable_water"])
                state["favorable_water"] = str(max(water, close) if quantity > 0 else min(water, close))
            if quantity < 0:
                carrying = (-quantity*close*decimal(strategy.short_carry_bps_per_day)
                            / Decimal(10000)/Decimal(24))
                unit = Decimal("1e-18")
                carrying = carrying.quantize(unit)
                state["cash"] = str((decimal(state["cash"])-carrying).quantize(unit))
                state["financing_costs"] = str(
                    (decimal(state["financing_costs"])+carrying).quantize(unit))
            state["history_closes"].append(str(close))
            state["history_closes"] = state["history_closes"][-(strategy.slow_lookback+1):]
            state["last_completed_at"] = bucket.isoformat()
            state["completed_live_bars"] += 1
            _mark(state, close, costs)
            event = _execute(state, _decision(state, strategy), bid, ask, costs, observed)
            _mark(state, mid, costs)
            state["current_candle"] = {"opened_at": bucket.isoformat(), "open": str(mid),
                "high": str(mid), "low": str(mid), "close": str(mid)}
    state["last_source_event_at"] = observed.isoformat()
    state["last_update_id"] = snapshot.get("update_id")
    state["status"] = "RUNNING"
    state["last_error"] = None
    state["updated_at"] = datetime.now(UTC).isoformat()
    return event


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-result", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--market-snapshot", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--max-runtime-seconds", type=float, default=604800)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.state_dir / "checkpoint.json"
    waiting_started = monotonic()
    required = [args.qualification_result, *args.manifest]
    while any(not path.is_file() for path in required):
        missing = [str(path) for path in required if not path.is_file()]
        waiting = {"schema": SCHEMA, "status": "WAITING_FOR_QUALIFIED_ARTIFACTS",
            "market": "SOL_USDT", "missing_count": len(missing),
            "network_orders": False, "trading_authority": False,
            "updated_at": datetime.now(UTC).isoformat()}
        atomic_write_json(checkpoint, waiting)
        if (args.once or monotonic()-waiting_started >= args.max_runtime_seconds
                or (args.stop_file and args.stop_file.exists())):
            print(json.dumps({"checkpoint": str(checkpoint), "status": waiting["status"],
                "network_orders": False, "trading_authority": False}))
            return 0
        sleep(args.poll_seconds)
    costs, strategy, _result, qualification_sha = _qualification(args.qualification_result)
    seed, identities = _seed(args.manifest, strategy.slow_lookback+1)
    if checkpoint.exists():
        state = _read_json(checkpoint)
        if state.get("schema") != SCHEMA or state.get("qualification_file_sha256") != qualification_sha:
            raise ValueError("forward shadow checkpoint identity differs")
        initial = str(decimal(costs.initial_cash))
        state.setdefault("initial_equity", initial)
        state.setdefault("current_equity", initial)
        state.setdefault("net_pnl", "0")
        state.setdefault("return_fraction", "0")
    else:
        state = _new_state(seed, identities, costs, strategy, qualification_sha)
    started = monotonic()
    while True:
        try:
            snapshot, bid, ask, observed = _snapshot(args.market_snapshot)
            if (state.get("last_source_event_at") is None
                    or observed > datetime.fromisoformat(state["last_source_event_at"])):
                event = advance(state, snapshot, bid, ask, observed, costs, strategy)
                atomic_write_json(checkpoint, state)
                if event:
                    with (args.state_dir / "actions.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event, sort_keys=True)+"\n")
        except Exception as exc:
            state["status"] = "RECOVERY"
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            state["updated_at"] = datetime.now(UTC).isoformat()
            atomic_write_json(checkpoint, state)
        if args.once or monotonic()-started >= args.max_runtime_seconds:
            break
        if args.stop_file and args.stop_file.exists():
            state["status"] = "STOPPED"
            atomic_write_json(checkpoint, state)
            break
        sleep(args.poll_seconds)
    print(json.dumps({"checkpoint": str(checkpoint), "status": state["status"],
        "completed_live_bars": state["completed_live_bars"],
        "network_orders": False, "trading_authority": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
