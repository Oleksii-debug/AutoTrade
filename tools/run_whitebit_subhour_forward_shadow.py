#!/usr/bin/env python3
"""Prospective 30–60 minute SOL shadow with technical/news/L2 gates."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
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
    HOURLY_WINDOWS, TechnicalCosts, summarize_hour_windows, technical_signal,
)
from tools.run_whitebit_subhour_supervisor import FROZEN_STRATEGY_ID, configurations


SCHEMA = "autotrade-whitebit-subhour-forward-shadow-v1"
MARKET = "SOL_PERP"
STEP_SECONDS = 900
MAX_ENTRY_DELAY = timedelta(seconds=90)
PROSPECTIVE_WAVE_CYCLES = 5
MAX_HOURLY_SAMPLES = 7 * 24 + 1
MAX_DECISION_JOURNAL_BYTES = 16 * 1024 * 1024


def _bars(client: WhiteBitPublicClient, now: datetime, limit: int = 250) -> list[dict]:
    # WhiteBIT's end boundary is exclusive at the aligned candle open.
    boundary = int(now.timestamp()) // STEP_SECONDS * STEP_SECONDS
    batch = client.klines(MARKET, interval="15m", end=boundary, limit=limit, training=True)
    result = []
    for row in batch.candles:
        result.append({"opened_at": row.open_time,
            "closed_at": row.open_time + timedelta(seconds=STEP_SECONDS),
            "open": row.open, "high": row.high, "low": row.low,
            "close": row.close, "volume": row.base_volume})
    if len(result) < 100:
        raise ValueError("INSUFFICIENT_LIVE_TECHNICAL_HISTORY")
    if int(result[-1]["closed_at"].timestamp()) != boundary:
        raise PublicDataError("LATEST_COMPLETED_CANDLE_NOT_AVAILABLE")
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
        "episodes": [], "wave_limit": PROSPECTIVE_WAVE_CYCLES,
        "technical_analyses": 0, "news_analyses": 0, "l2_analyses": 0,
        "holds": {}, "entries_by_utc_day": {}, "fees": "0", "funding": "0",
        "network_orders": False, "trading_authority": False,
        "paid_model_calls": 0}


def _hold(state: dict, reason: str) -> None:
    state["holds"][reason] = state["holds"].get(reason, 0) + 1


def _exit_values(position: dict, quote: dict, now: datetime,
                 costs: TechnicalCosts) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    quantity = Decimal(position["quantity"])
    slippage = Decimal(costs.slippage_bps_per_fill) / Decimal(10000)
    fill = (quote["bid"] * (1 - slippage) if quantity > 0
            else quote["ask"] * (1 + slippage))
    fee = abs(quantity) * fill * Decimal(costs.fee_bps_per_fill) / Decimal(10000)
    elapsed = max(Decimal(0), Decimal(str((now - datetime.fromisoformat(
        position["opened_at"])).total_seconds())))
    funding = (abs(quantity) * fill * Decimal(costs.funding_bps_per_day)
               / Decimal(10000) * elapsed / Decimal(86400))
    return fill, fee, funding, elapsed


def _record_clock_sample(state: dict, *, boundary: datetime, quote: dict,
                         now: datetime, contiguous: bool) -> dict | None:
    """Mark shadow equity at observed near-boundary quotes and skip data gaps."""
    metrics = state.setdefault("clock_metrics", {"samples": [], "segment": 0})
    if not contiguous:
        metrics["segment"] += 1
    boundary = boundary.astimezone(UTC)
    if boundary.minute or boundary.second or boundary.microsecond:
        return None
    if not timedelta(0) <= now - boundary <= MAX_ENTRY_DELAY:
        return None
    balance = Decimal(state["balance"])
    position = state["position"]
    if position is not None:
        fill, fee, funding, _ = _exit_values(position, quote, now, TechnicalCosts())
        balance += Decimal(position["quantity"]) * (fill - Decimal(position["entry_price"]))
        balance -= fee + funding
    epoch = int(boundary.timestamp())
    samples = metrics["samples"]
    if samples and epoch <= samples[-1]["boundary_epoch"]:
        return None
    sample = {"boundary_epoch": epoch, "observed_at": now.isoformat(),
              "equity": str(balance), "segment": metrics["segment"],
              "quote_sha256": quote.get("source_sha256")}
    samples.append(sample)
    samples[:] = samples[-MAX_HOURLY_SAMPLES:]
    by_epoch = {item["boundary_epoch"]: item for item in samples}
    windows = {}
    for hours in HOURLY_WINDOWS:
        span = 3600 * hours
        returns = []
        for item in samples:
            end = item["boundary_epoch"]
            prior = by_epoch.get(end - span)
            if (end % span or prior is None or prior["segment"] != item["segment"]
                    or Decimal(prior["equity"]) <= 0):
                continue
            returns.append(Decimal(item["equity"]) / Decimal(prior["equity"]) - 1)
        windows[str(hours)] = summarize_hour_windows(returns, hours)
    metrics["windows"] = windows
    metrics["quote_basis"] = "OBSERVED_BID_ASK_WITH_ESTIMATED_EXIT_COSTS"
    return sample


def _review_wave(state: dict) -> None:
    cycles = state["closed_cycles"]
    if cycles % PROSPECTIVE_WAVE_CYCLES:
        return
    episodes = state["episodes"][-PROSPECTIVE_WAVE_CYCLES:]
    if len(episodes) != PROSPECTIVE_WAVE_CYCLES:
        return
    outcomes = [Decimal(item["net_pnl"]) for item in episodes]
    total = sum(outcomes)
    exit_counts: dict[str, int] = {}
    for episode in episodes:
        reason = episode.get("exit_reason", "UNKNOWN")
        exit_counts[reason] = exit_counts.get(reason, 0) + 1
    state["wave_analysis"] = {
        "wave_number": cycles // PROSPECTIVE_WAVE_CYCLES,
        "closed_through_cycle": cycles,
        "status": "POSITIVE_AFTER_COSTS" if total > 0 else "NOT_POSITIVE_AFTER_COSTS",
        "closed_cycles": len(episodes),
        "positive_cycles": sum(outcome > 0 for outcome in outcomes),
        "net_pnl": str(total),
        "best_episode_net_pnl": str(max(outcomes)),
        "worst_episode_net_pnl": str(min(outcomes)),
        "exit_reasons": exit_counts,
        "strategy_mutated": False,
        "next_action": "CONTINUE_FROZEN_SHADOW_ON_NEXT_COMPLETED_BAR",
    }


def _append_decision(path: Path, value: dict) -> None:
    """Append one bounded decision record and retain one rotated predecessor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise ValueError("decision journal cannot be a symlink")
    if path.exists() and path.stat().st_size >= MAX_DECISION_JOURNAL_BYTES:
        rotated = path.with_suffix(path.suffix + ".1")
        if rotated.exists():
            rotated.unlink()
        os.replace(path, rotated)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False) + "\n").encode()
    with path.open("ab") as stream:
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())


def process_completed_bar(state: dict, history: list[dict], *, quote: dict,
                          news: dict, l2: dict, now: datetime) -> dict:
    """Advance exactly one new completed bar; never revises the strategy."""
    config = configurations()[0]; costs = TechnicalCosts()
    bar = history[-1]; epoch = int(bar["opened_at"].timestamp())
    if epoch <= state["last_bar_open_epoch"]:
        return state
    gap = epoch != state["last_bar_open_epoch"] + STEP_SECONDS
    position = state["position"]
    state["last_bar_open_epoch"] = epoch
    state["technical_analyses"] += 1; state["news_analyses"] += 1
    state["l2_analyses"] += int(l2.get("status") != "NOT_REQUESTED")
    state.pop("last_pretrade_thesis", None)
    signal = technical_signal(history, config)
    state["last_technical_signal"] = signal
    if gap and position is None:
        _hold(state, "BAR_GAP_REANCHORED")
        return state
    if position is not None:
        position["holding_bars"] += 1
        close = Decimal(bar["close"]); entry = Decimal(position["entry_price"])
        direction = Decimal(position["direction"])
        move = (close - entry) * direction
        atr = Decimal(position["entry_atr"]); reason = None
        if gap:
            reason = "DATA_GAP_RECOVERY_EXIT"
        elif position["holding_bars"] >= config.maximum_hold_bars:
            reason = "MAX_HOLD_EXIT"
        elif position["holding_bars"] >= config.minimum_hold_bars:
            if move <= -Decimal(config.stop_atr) * atr:
                reason = "ATR_STOP_EXIT"
            elif move >= Decimal(config.take_profit_atr) * atr:
                reason = "ATR_TAKE_PROFIT_EXIT"
            else:
                features = signal["features"]
                middle = Decimal(features["band_mid"])
                if (direction > 0 and close >= middle) or (direction < 0 and close <= middle):
                    reason = "BOLLINGER_MEAN_EXIT"
        if reason:
            quantity = Decimal(position["quantity"])
            fill, fee, funding, elapsed = _exit_values(position, quote, now, costs)
            prior_balance = Decimal(state["balance"])
            balance = prior_balance + quantity * (fill - entry) - fee - funding
            starting_equity = position.get("starting_equity")
            if starting_equity is None:
                entry_fee = abs(quantity) * entry * Decimal(costs.fee_bps_per_fill) / Decimal(10000)
                starting_equity = prior_balance + entry_fee
            state["balance"] = format(balance, "f")
            state["fees"] = format(Decimal(state["fees"]) + fee, "f")
            state["funding"] = format(Decimal(state["funding"]) + funding, "f")
            state["net_pnl"] = format(balance - Decimal(costs.initial_equity), "f")
            state["closed_cycles"] += 1
            episode = {"opened_at": position["opened_at"],
                "closed_at": now.isoformat(), "direction": int(direction),
                "entry_price": position["entry_price"], "exit_price": format(fill, "f"),
                "exit_reason": reason, "net_equity": state["balance"],
                "net_pnl": format(balance - Decimal(starting_equity), "f"),
                "holding_seconds": int(elapsed),
                "technical_configuration_frozen": True}
            state["last_episode"] = episode
            state["episodes"].append(episode)
            state["episodes"] = state["episodes"][-PROSPECTIVE_WAVE_CYCLES:]
            state["position"] = None
            _review_wave(state)
        return state
    if not timedelta(0) <= now - bar["closed_at"] <= MAX_ENTRY_DELAY:
        _hold(state, "COMPLETED_CANDLE_OUTSIDE_ENTRY_WINDOW")
        return state
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
        "starting_equity": format(balance, "f"),
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
        if type(state) is dict:
            state.setdefault("episodes", [])
            state.setdefault("entries_by_utc_day", {})
            state.setdefault("fees", "0")
            state.setdefault("funding", "0")
    while not stop.exists():
        now = datetime.now(UTC); started = monotonic(); error = None
        try:
            expected_epoch = int(now.timestamp()) // STEP_SECONDS * STEP_SECONDS - STEP_SECONDS
            if state is None:
                history = _bars(client, now)
                state = _initial(int(history[-1]["opened_at"].timestamp()), now)
                status = "OBSERVING_FROM_NEXT_COMPLETED_BAR"
            elif expected_epoch > state["last_bar_open_epoch"]:
                history = _bars(client, now)
                previous_epoch = state["last_bar_open_epoch"]
                try:
                    news = collect_market_context()
                except (MarketContextError, ValueError, TypeError) as exc:
                    news = {"decision": "BLOCK", "reasons": [type(exc).__name__],
                        "evidence_sha256": "UNAVAILABLE", "trading_authority": False}
                quote = _quote(client)
                now = datetime.now(UTC)
                direction = technical_signal(history, configurations()[0])["direction"]
                l2 = (_l2_confirmation(args.depth_database, "LONG" if direction >= 0 else "SHORT",
                                        int(now.timestamp())) if direction else
                      {"status": "NOT_REQUESTED", "reasons": ["NO_TECHNICAL_SETUP"]})
                prior_cycles = state["closed_cycles"]
                next_state = process_completed_bar(deepcopy(state), history, quote=quote, news=news,
                    l2=l2, now=now)
                next_state["last_news_context"] = news
                next_state["last_l2_context"] = l2
                clock_sample = _record_clock_sample(
                    next_state, boundary=history[-1]["closed_at"], quote=quote, now=now,
                    contiguous=next_state["last_bar_open_epoch"] == previous_epoch + STEP_SECONDS)
                closed = next_state["closed_cycles"]
                review = next_state.get("wave_analysis")
                _append_decision(state_dir / "decisions.jsonl", {
                    "schema": SCHEMA, "observed_at": now.isoformat(),
                    "previous_bar_open_epoch": previous_epoch,
                    "processed_bar_open_epoch": next_state["last_bar_open_epoch"],
                    "technical": next_state.get("last_technical_signal"),
                    "news": news, "l2": l2,
                    "pretrade": next_state.get("last_pretrade_thesis"),
                    "position_open": next_state["position"] is not None,
                    "closed_cycles": closed,
                    "episode": next_state.get("last_episode") if closed > prior_cycles else None,
                    "wave_analysis": (review if closed > prior_cycles and review
                                      and review.get("closed_through_cycle") == closed else None),
                    "net_pnl": next_state["net_pnl"],
                    "clock_sample": clock_sample,
                    "network_orders": False, "trading_authority": False,
                })
                state = next_state
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
