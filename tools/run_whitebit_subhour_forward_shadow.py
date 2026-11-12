#!/usr/bin/env python3
"""Prospective 30–60 minute SOL shadow with technical/news/L2 gates."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.integrations.market_news_context import (
    MarketContextError, collect_market_context,
)
from research.autotrade_research.integrations.whitebit_public import (
    PublicDataError, WhiteBitPublicClient,
)
from research.autotrade_research.strategies.pretrade_evidence import (
    PreTradeThesis, evaluate_pretrade,
)
from research.autotrade_research.strategies.technical_confluence import (
    TechnicalCosts, technical_signal,
)
from tools.run_whitebit_subhour_supervisor import FROZEN_STRATEGY_ID, configurations


SCHEMA = "autotrade-whitebit-subhour-forward-shadow-v1"
MARKET = "SOL_PERP"
STEP_SECONDS = 900


def _bars(client: WhiteBitPublicClient, now: datetime, limit: int = 250) -> list[dict]:
    latest = int(now.timestamp()) // STEP_SECONDS * STEP_SECONDS - STEP_SECONDS
    batch = client.klines(MARKET, interval="15m", end=latest, limit=limit, training=True)
    result = []
    for row in batch.candles:
        result.append({"opened_at": row.open_time,
            "closed_at": row.open_time + timedelta(seconds=STEP_SECONDS),
            "open": row.open, "high": row.high, "low": row.low,
            "close": row.close, "volume": row.base_volume})
    if len(result) < 100:
        raise ValueError("INSUFFICIENT_LIVE_TECHNICAL_HISTORY")
    return result


def _quote(client: WhiteBitPublicClient) -> dict:
    matches = [row for row in client.futures_markets() if row.market == MARKET]
    if len(matches) != 1:
        raise PublicDataError("FUTURES_MARKET_NOT_AVAILABLE")
    row = matches[0]
    bid, ask = Decimal(row.bid), Decimal(row.ask)
    if bid <= 0 or ask <= bid:
        raise PublicDataError("INVALID_LIVE_FUTURES_QUOTE")
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / Decimal(2),
        "received_at": row.received_at.isoformat(), "source_sha256": row.digest}


def _l2_confirmation(path: Path, direction: str, now_epoch: int) -> dict:
    if path.is_symlink() or not path.is_file():
        return {"status": "STALE", "reasons": ["DEPTH_TAPE_UNAVAILABLE"]}
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = list(connection.execute(
            "SELECT bucket_epoch,max_spread_bps,bid_depth_top5,ask_depth_top5,"
            "imbalance_top5,order_flow_imbalance FROM buckets WHERE market=? "
            "AND bucket_epoch>=? AND bucket_epoch<=? ORDER BY bucket_epoch",
            (MARKET, now_epoch - 19, now_epoch)))
    finally:
        connection.close()
    base = {"sample_count": len(rows), "direction": direction}
    if len(rows) < 8 or now_epoch - rows[-1][0] > 3:
        return {**base, "status": "STALE", "reasons": ["DEPTH_NOT_FRESH"]}
    if max(Decimal(row[1]) for row in rows) > Decimal("8"):
        return {**base, "status": "CONFLICT", "reasons": ["SPREAD_TOO_WIDE"]}
    sign = Decimal(1) if direction == "LONG" else Decimal(-1)
    aligned = []
    for _, _, bid, ask, imbalance, ofi in rows:
        total = Decimal(bid) + Decimal(ask)
        normalized = Decimal(0) if total <= 0 else max(Decimal(-1), min(
            Decimal(1), Decimal(ofi) / total))
        aligned.append(((Decimal(imbalance) + normalized) / Decimal(2)) * sign)
    persistence = sum(value >= Decimal("0.05") for value in aligned) / Decimal(len(aligned))
    mean = sum(aligned, Decimal(0)) / Decimal(len(aligned))
    confirmed = persistence >= Decimal("0.60") and mean >= Decimal("0.05")
    return {**base, "status": "CONFIRMED" if confirmed else "CONFLICT",
        "reasons": [] if confirmed else ["L2_PRESSURE_NOT_PERSISTENT"],
        "directional_mean_score": format(mean, "f"),
        "directional_persistence": format(persistence, "f")}


def _initial(last_bar_epoch: int, now: datetime) -> dict:
    return {"schema": SCHEMA, "strategy_id": FROZEN_STRATEGY_ID,
        "strategy_mutation_allowed": False, "started_at": now.isoformat(),
        "last_bar_open_epoch": last_bar_epoch, "balance": "1000",
        "position": None, "closed_cycles": 0, "net_pnl": "0",
        "technical_analyses": 0, "news_analyses": 0, "l2_analyses": 0,
        "holds": {}, "entries_by_utc_day": {}, "fees": "0", "funding": "0",
        "network_orders": False, "trading_authority": False,
        "paid_model_calls": 0}


def _hold(state: dict, reason: str) -> None:
    state["holds"][reason] = state["holds"].get(reason, 0) + 1


def process_completed_bar(state: dict, history: list[dict], *, quote: dict,
                          news: dict, l2: dict, now: datetime) -> dict:
    """Advance exactly one new completed bar; never revises the strategy."""
    config = configurations()[0]; costs = TechnicalCosts()
    bar = history[-1]; epoch = int(bar["opened_at"].timestamp())
    if epoch <= state["last_bar_open_epoch"]:
        return state
    if epoch != state["last_bar_open_epoch"] + STEP_SECONDS:
        _hold(state, "BAR_GAP_REANCHORED")
        state["last_bar_open_epoch"] = epoch
        return state
    state["last_bar_open_epoch"] = epoch
    state["technical_analyses"] += 1; state["news_analyses"] += 1
    state["l2_analyses"] += 1
    position = state["position"]
    if position is not None:
        position["holding_bars"] += 1
        close = Decimal(bar["close"]); entry = Decimal(position["entry_price"])
        direction = Decimal(position["direction"])
        move = (close - entry) * direction
        atr = Decimal(position["entry_atr"]); reason = None
        if position["holding_bars"] >= config.maximum_hold_bars:
            reason = "MAX_HOLD_EXIT"
        elif position["holding_bars"] >= config.minimum_hold_bars:
            if move <= -Decimal(config.stop_atr) * atr:
                reason = "ATR_STOP_EXIT"
            elif move >= Decimal(config.take_profit_atr) * atr:
                reason = "ATR_TAKE_PROFIT_EXIT"
            else:
                features = technical_signal(history, config)["features"]
                middle = Decimal(features["band_mid"])
                if (direction > 0 and close >= middle) or (direction < 0 and close <= middle):
                    reason = "BOLLINGER_MEAN_EXIT"
        if reason:
            slippage = Decimal(costs.slippage_bps_per_fill) / Decimal(10000)
            fill = (quote["bid"] * (Decimal(1) - slippage) if direction > 0
                    else quote["ask"] * (Decimal(1) + slippage))
            quantity = Decimal(position["quantity"])
            fee = abs(quantity) * fill * Decimal(costs.fee_bps_per_fill) / Decimal(10000)
            elapsed = max(Decimal(0), Decimal(str((now - datetime.fromisoformat(
                position["opened_at"])).total_seconds())))
            funding = (abs(quantity) * fill * Decimal(costs.funding_bps_per_day)
                       / Decimal(10000) * elapsed / Decimal(86400))
            balance = Decimal(state["balance"]) + quantity * (fill - entry) - fee - funding
            state["balance"] = format(balance, "f")
            state["fees"] = format(Decimal(state["fees"]) + fee, "f")
            state["funding"] = format(Decimal(state["funding"]) + funding, "f")
            state["net_pnl"] = format(balance - Decimal(costs.initial_equity), "f")
            state["closed_cycles"] += 1
            state["last_episode"] = {"opened_at": position["opened_at"],
                "closed_at": now.isoformat(), "direction": int(direction),
                "entry_price": position["entry_price"], "exit_price": format(fill, "f"),
                "exit_reason": reason, "net_equity": state["balance"],
                "technical_configuration_frozen": True}
            state["position"] = None
        return state
    signal = technical_signal(history, config)
    state["last_technical_signal"] = signal
    day = now.date().isoformat()
    if state["entries_by_utc_day"].get(day, 0) >= config.maximum_entries_per_utc_day:
        _hold(state, "DAILY_ENTRY_LIMIT")
        return state
    if signal["direction"] == 0:
        _hold(state, signal["reason"]); return state
    direction = "LONG" if signal["direction"] > 0 else "SHORT"
    if news["decision"] != "ALLOW":
        _hold(state, "OFFICIAL_NEWS_CONTEXT_BLOCK"); return state
    if l2.get("status") != "CONFIRMED":
        _hold(state, "L2_NOT_CONFIRMED"); return state
    slippage = Decimal(costs.slippage_bps_per_fill) / Decimal(10000)
    fill = (quote["ask"] * (Decimal(1) + slippage) if signal["direction"] > 0
            else quote["bid"] * (Decimal(1) - slippage))
    atr = Decimal(signal["features"]["atr"])
    stop = fill - atr if signal["direction"] > 0 else fill + atr
    target = fill + Decimal(4) * atr if signal["direction"] > 0 else fill - Decimal(4) * atr
    thesis = evaluate_pretrade(PreTradeThesis(direction=direction, regime="RANGE",
        causal_data_complete=True, regime_confirmed=True, location_confirmed=True,
        trigger_confirmed=True, entry_price=fill, invalidation_price=stop,
        target_price=target, round_trip_cost_bps=costs.round_trip_bps,
        minimum_reward_risk="2", microstructure_status="CONFIRMED"))
    state["last_pretrade_thesis"] = thesis
    if thesis["decision"] != "ENTER":
        _hold(state, "PRETRADE_THESIS_REJECTED"); return state
    balance = Decimal(state["balance"])
    notional = balance * Decimal(costs.capital_fraction)
    quantity = notional / fill * Decimal(signal["direction"])
    fee = notional * Decimal(costs.fee_bps_per_fill) / Decimal(10000)
    state["balance"] = format(balance - fee, "f")
    state["fees"] = format(Decimal(state["fees"]) + fee, "f")
    state["entries_by_utc_day"][day] = state["entries_by_utc_day"].get(day, 0) + 1
    state["position"] = {"direction": signal["direction"],
        "quantity": format(quantity, "f"), "entry_price": format(fill, "f"),
        "entry_atr": format(atr, "f"), "opened_at": now.isoformat(),
        "holding_bars": 0, "news_evidence_sha256": news["evidence_sha256"],
        "l2_confirmation": l2, "technical_signal": signal}
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--depth-database", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 15 <= args.poll_seconds <= 300:
        parser.error("poll-seconds must be 15..300")
    state_dir = args.state_dir.resolve(strict=False); state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = state_dir / "checkpoint.json"; stop = args.stop_file.resolve(strict=False)
    try: stop.relative_to(state_dir)
    except ValueError: parser.error("stop-file must remain inside state-dir")
    stop.unlink(missing_ok=True); client = WhiteBitPublicClient(); state = None
    if checkpoint.is_file():
        state = json.loads(checkpoint.read_bytes()).get("state")
    while not stop.exists():
        now = datetime.now(UTC); started = monotonic(); error = None
        try:
            history = _bars(client, now)
            if state is None:
                state = _initial(int(history[-1]["opened_at"].timestamp()), now)
                status = "OBSERVING_FROM_NEXT_COMPLETED_BAR"
            elif int(history[-1]["opened_at"].timestamp()) > state["last_bar_open_epoch"]:
                quote = _quote(client)
                try:
                    news = collect_market_context(now=now)
                except (MarketContextError, ValueError, TypeError) as exc:
                    news = {"decision": "BLOCK", "reasons": [type(exc).__name__],
                        "evidence_sha256": "UNAVAILABLE", "trading_authority": False}
                direction = technical_signal(history, configurations()[0])["direction"]
                l2 = (_l2_confirmation(args.depth_database, "LONG" if direction >= 0 else "SHORT",
                                        int(now.timestamp())) if direction else
                      {"status": "NOT_REQUESTED", "reasons": ["NO_TECHNICAL_SETUP"]})
                state = process_completed_bar(state, history, quote=quote, news=news,
                    l2=l2, now=now)
                state["last_news_context"] = news; state["last_l2_context"] = l2
                status = "RUNNING"
            else:
                status = "WAITING_FOR_COMPLETED_15M_BAR"
        except (PublicDataError, OSError, ValueError, TypeError, KeyError,
                sqlite3.Error) as exc:
            error = type(exc).__name__; status = "RECOVERY"
        value = {"schema": SCHEMA, "status": status, "updated_at": datetime.now(UTC).isoformat(),
            "last_cycle_seconds": monotonic() - started, "last_error_class": error,
            "state": state, "network_orders": False, "trading_authority": False,
            "paid_model_calls": 0}
        atomic_write_json(checkpoint, value)
        if args.once: return 0 if error is None else 2
        deadline = monotonic() + args.poll_seconds
        while not stop.exists() and monotonic() < deadline:
            sleep(min(5, max(1, deadline - monotonic())))
    atomic_write_json(checkpoint, {**value, "status": "STOPPED",
        "updated_at": datetime.now(UTC).isoformat()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
