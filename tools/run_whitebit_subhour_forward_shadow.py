#!/usr/bin/env python3
"""Prospective 30–60 minute SOL shadow with technical/news/L2 gates."""
from __future__ import annotations

import argparse
from contextlib import closing
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
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.market_news_context import (
    MarketContextError, collect_market_context,
)
from research.autotrade_research.integrations.whitebit_public import (
    PublicDataError, WhiteBitPublicClient,
)
from research.autotrade_research.learning.observed_quote_learning import (
    HORIZON_SECONDS, propose_from_observed_outcomes, review_observed_predictions,
)
from research.autotrade_research.strategies.pretrade_evidence import (
    PreTradeThesis, evaluate_pretrade,
)
from research.autotrade_research.strategies.technical_confluence import (
    HOURLY_WINDOWS, TechnicalCosts, summarize_hour_windows, technical_signal,
)
from tools.run_whitebit_observed_portfolio import (
    SCHEMA as PORTFOLIO_SCHEMA, advance as advance_portfolio, new_state as new_portfolio_state,
)
from tools.run_whitebit_subhour_supervisor import FROZEN_STRATEGY_ID, configurations

BTC_ETH_MARKETS = ("BTC_PERP", "ETH_PERP")


SCHEMA = "autotrade-whitebit-subhour-forward-shadow-v1"
MARKET = "SOL_PERP"
LEARNING_MARKETS = ("BTC_PERP", "ETH_PERP")
COST_EVIDENCE_MARKETS = ("BCH_PERP", "NEAR_PERP", "SUI_PERP")
STEP_SECONDS = 900
MAX_ENTRY_DELAY = timedelta(seconds=90)
PROSPECTIVE_WAVE_CYCLES = 5
MAX_HOURLY_SAMPLES = 7 * 24 + 1
MAX_RECENT_OUTCOMES = 96
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
    quotes = {}
    point_in_time_futures = {}
    for row in client.futures_markets():
        if row.market not in (MARKET, *LEARNING_MARKETS, *COST_EVIDENCE_MARKETS):
            continue
        bid, ask = Decimal(row.bid), Decimal(row.ask)
        if bid <= 0 or ask <= bid:
            if row.market == MARKET:
                raise PublicDataError("INVALID_LIVE_FUTURES_QUOTE")
            continue
        if row.market in quotes:
            raise PublicDataError("DUPLICATE_LIVE_FUTURES_QUOTE")
        received_at = row.received_at.isoformat()
        quotes[row.market] = {"bid": bid, "ask": ask,
            "mid": (bid + ask) / Decimal(2), "received_at": received_at,
            "source_sha256": row.digest}
        if row.market in (MARKET, *LEARNING_MARKETS):
            point_in_time_futures[row.market] = {
                "open_interest": row.open_interest,
                "mark_price": row.mark_price,
                "index_price": row.index_price,
                "received_at": received_at,
                "source_sha256": row.digest,
            }
    if MARKET not in quotes:
        raise PublicDataError("FUTURES_MARKET_NOT_AVAILABLE")
    cost_quotes = {}
    for market in COST_EVIDENCE_MARKETS:
        if market not in quotes:
            continue
        quote = quotes[market]
        cost_quotes[market] = {
            "bid": str(quote["bid"]), "ask": str(quote["ask"]),
            "spread_bps": str((quote["ask"] - quote["bid"]) / quote["mid"] * 10000),
            "received_at": quote["received_at"], "source_sha256": quote["source_sha256"],
        }
    return {**quotes[MARKET], "observed_cost_quotes": cost_quotes,
            "point_in_time_futures": point_in_time_futures,
            "learning_quotes": {market: quotes[market] for market in LEARNING_MARKETS
                                if market in quotes}}


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
    state["last_hold_reason"] = reason
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


def _observe_execution_outcomes(
    state: dict, *, boundary: datetime, quote: dict, now: datetime,
    contiguous: bool, policy_action: str, market: str = MARKET,
) -> list[dict]:
    """Label earlier observations with the next observed 30-minute BBO outcome."""
    research = state.setdefault("execution_outcomes", {
        "pending": [], "recent": [], "settled_count": 0,
    })
    epoch = int(boundary.timestamp())
    prior_epoch = research.get("last_boundary_epoch")
    if prior_epoch is not None and epoch <= prior_epoch:
        return []
    research["last_boundary_epoch"] = epoch
    if not contiguous or (prior_epoch is not None and epoch - prior_epoch != STEP_SECONDS):
        research["pending"] = []
    received = datetime.fromisoformat(quote["received_at"])
    if (not timedelta(0) <= now - received <= timedelta(seconds=5)
            or not timedelta(0) <= received - boundary <= MAX_ENTRY_DELAY):
        research["pending"] = [
            row for row in research["pending"]
            if row["entry_boundary_epoch"] + HORIZON_SECONDS > epoch
        ]
        return []

    costs = TechnicalCosts()
    starting_equity = Decimal(costs.initial_equity)
    notional = starting_equity * Decimal(costs.capital_fraction)
    fee_ratio = Decimal(costs.fee_bps_per_fill) / Decimal(10000)
    slippage = Decimal(costs.slippage_bps_per_fill) / Decimal(10000)
    settled = []
    pending = []
    for entry in research["pending"]:
        due = entry["entry_boundary_epoch"] + HORIZON_SECONDS
        if epoch < due:
            pending.append(entry)
            continue
        if epoch != due:
            continue
        outcomes = {}
        entry_bid, entry_ask = Decimal(entry["bid"]), Decimal(entry["ask"])
        exit_bid, exit_ask = quote["bid"], quote["ask"]
        midpoint_move = ((exit_bid + exit_ask) - (entry_bid + entry_ask)) / 2
        for side, label in ((1, "LONG"), (-1, "SHORT")):
            entry_touch, exit_touch = (entry_ask, exit_bid) if side > 0 else (entry_bid, exit_ask)
            entry_fill = entry_touch * (1 + side * slippage)
            quantity = side * notional / entry_fill
            fill, fee, funding, elapsed = _exit_values(
                {"quantity": str(quantity), "opened_at": entry["quote_received_at"]},
                quote, received, costs)
            executed_pnl = quantity * (fill - entry_fill)
            midpoint_pnl = quantity * midpoint_move
            touch_pnl = quantity * (exit_touch - entry_touch)
            entry_fee = notional * fee_ratio
            pnl = executed_pnl - entry_fee - fee - funding
            outcomes[label] = {"entry_price": str(entry_fill), "exit_price": str(fill),
                "gross_midpoint_equity_return": str(midpoint_pnl / starting_equity),
                "spread_equity_cost": str((midpoint_pnl - touch_pnl) / starting_equity),
                "slippage_equity_cost": str((touch_pnl - executed_pnl) / starting_equity),
                "fee_equity_cost": str((entry_fee + fee) / starting_equity),
                "funding_equity_cost": str(funding / starting_equity),
                "net_equity_return": str(pnl / starting_equity)}
        settled.append({
            "outcome_id": f"{market}:M15:{entry['entry_boundary_epoch']}:{HORIZON_SECONDS}",
            **entry, "exit_boundary_epoch": epoch, "exit_quote_received_at": received.isoformat(),
            "exit_quote_sha256": quote["source_sha256"], "elapsed_seconds": str(elapsed),
            "exit_bid": str(exit_bid), "exit_ask": str(exit_ask),
            "kind": "OBSERVED_FIXED_TIME_EXECUTION_OUTCOME", "outcomes": outcomes,
        })
    research["recent"] = (research["recent"] + settled)[-MAX_RECENT_OUTCOMES:]
    proposal = propose_from_observed_outcomes(
        research["recent"], entry_epoch=epoch, quote_received_at=received,
        technical=state.get("last_technical_signal"))
    pending.append({
        "entry_boundary_epoch": epoch, "quote_received_at": received.isoformat(),
        "entry_quote_sha256": quote["source_sha256"], "bid": str(quote["bid"]),
        "ask": str(quote["ask"]), "policy_action": policy_action,
        "technical": state.get("last_technical_signal"),
        "news_decision": state.get("last_news_context", {}).get("decision"),
        "l2_status": state.get("last_l2_context", {}).get("status"),
        "learner_proposal": proposal,
    })
    research["pending"] = pending
    research["settled_count"] += len(settled)
    if settled:
        research["learner_evaluation"] = review_observed_predictions(research["recent"])
    return settled


def _midpoint_learning_context(path: Path, market: str, quote: dict) -> dict | None:
    """Use settled five-minute midpoint history without fetching more candles."""
    received = datetime.fromisoformat(quote["received_at"])
    window = 300
    end = (int(received.timestamp()) - 60) // window * window
    start = end - 20 * window
    if not path.is_file() or path.is_symlink():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as con:
            rows = con.execute(
                "SELECT window_epoch,features_json FROM archived_windows "
                "WHERE market=? AND window_epoch>=? AND window_epoch<? ORDER BY window_epoch",
                (market, start, end)).fetchall()
    except sqlite3.Error:
        return None
    if [epoch for epoch, _ in rows] != list(range(start, end, window)):
        return None
    mids = []
    for epoch, payload in rows:
        feature = json.loads(payload)
        # Only the last midpoint matters here. Reconnects inside a window
        # do not invalidate a fresh endpoint on the restored order book.
        if (datetime.fromisoformat(feature["last_received_at"]) > received
                or feature["last_bucket_epoch"] < epoch + window - 10):
            return None
        mids.append(Decimal(feature["close_mid"]))
    changes = [abs(current - previous) for previous, current in zip(mids, mids[1:])]
    return {"kind": "SETTLED_M5_MIDPOINT_CONTEXT", "source_window_end_epoch": end,
        "features": {"close": str(quote["mid"]), "band_mid": str(sum(mids) / len(mids)),
                     "atr": str(sum(changes[-14:]) / 14)}}


def _observe_additional_markets(
    state: dict, *, depth_database: Path, boundary: datetime, quote: dict,
    now: datetime, contiguous: bool,
) -> dict:
    """Run independent research ledgers from the same public quote response."""
    research = state.setdefault("additional_learning", {})
    observations = {}
    for market, market_quote in quote.get("learning_quotes", {}).items():
        ledger = deepcopy(research.get(market, {}))
        try:
            ledger["last_technical_signal"] = _midpoint_learning_context(
                depth_database, market, market_quote)
            settled = _observe_execution_outcomes(
                ledger, boundary=boundary, quote=market_quote, now=now,
                contiguous=contiguous, policy_action="RESEARCH_ONLY", market=market)
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
            observations[market] = {"error_class": type(exc).__name__}
            continue
        research[market] = ledger
        pending = ledger["execution_outcomes"]["pending"]
        current = pending[-1] if pending and pending[-1]["entry_boundary_epoch"] == int(
            boundary.timestamp()) else None
        observations[market] = {"observation": current, "settled": settled}
    return observations


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


def _close_position(state: dict, quote: dict, now: datetime, reason: str,
                    costs: TechnicalCosts) -> None:
    position = state["position"]
    quantity = Decimal(position["quantity"])
    entry = Decimal(position["entry_price"])
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
    episode = {
        "opened_at": position["opened_at"], "closed_at": now.isoformat(),
        "direction": position["direction"], "entry_price": position["entry_price"],
        "exit_price": format(fill, "f"), "exit_reason": reason,
        "net_equity": state["balance"],
        "net_pnl": format(balance - Decimal(starting_equity), "f"),
        "holding_seconds": int(elapsed), "exit_quote_sha256": quote.get("source_sha256"),
        "technical_configuration_frozen": True,
    }
    state["last_episode"] = episode
    state["episodes"] = (state["episodes"] + [episode])[-PROSPECTIVE_WAVE_CYCLES:]
    state["position"] = None
    _review_wave(state)


def protect_open_position(state: dict, quote: dict, now: datetime) -> str | None:
    """Apply an already-fixed stop and deadline to a fresh executable quote."""
    position = state["position"]
    if position is None:
        return None
    quote_age = now - datetime.fromisoformat(quote["received_at"])
    if not timedelta(0) <= quote_age <= timedelta(seconds=5):
        raise PublicDataError("STALE_PROTECTIVE_QUOTE")
    config = configurations()[0]
    elapsed = now - datetime.fromisoformat(position["opened_at"])
    direction = position["direction"]
    stop = (Decimal(position["entry_price"])
            - direction * Decimal(config.stop_atr) * Decimal(position["entry_atr"]))
    executable = quote["bid"] if direction > 0 else quote["ask"]
    if direction * (executable - stop) <= 0:
        reason = "OBSERVED_ATR_PROTECTIVE_STOP"
    elif elapsed >= timedelta(seconds=config.maximum_hold_bars * STEP_SECONDS):
        reason = "ELAPSED_HOLD_LIMIT_EXIT"
    else:
        return None
    _close_position(state, quote, now, reason, TechnicalCosts())
    return reason


def _decision_offset(path: Path) -> int:
    """Locate the end of the last committed record without changing the journal."""
    rotated = path.with_suffix(path.suffix + ".1")
    if path.is_symlink() or rotated.is_symlink():
        raise ValueError("decision journal cannot be a symlink")
    if not path.exists():
        return 0
    size = path.stat().st_size
    if size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            if stream.read() != b"\n":
                raise ValueError("decision journal has an uncommitted legacy suffix")
    return size


def _finish_decision(checkpoint: Path, journal: Path, pending: Path) -> None:
    """Finish an interrupted publication without replaying a market decision."""
    if pending.is_symlink() or journal.is_symlink():
        raise ValueError("decision transaction cannot use symlinks")
    transaction = json.loads(pending.read_bytes())
    value = transaction["checkpoint"]
    event = transaction["event"]
    offset = transaction["offset"]
    if (type(offset) is not int or not 0 <= offset < MAX_DECISION_JOURNAL_BYTES
            or value["schema"] != SCHEMA or event["schema"] != SCHEMA
            or value["trading_authority"] is not False or event["trading_authority"] is not False
            or value["network_orders"] is not False or event["network_orders"] is not False):
        raise ValueError("invalid pending decision transaction")
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False) + "\n").encode()
    if not journal.exists() and offset != 0:
        raise ValueError("decision journal is missing before pending append")
    with journal.open("r+b" if journal.exists() else "x+b") as stream:
        size = stream.seek(0, os.SEEK_END)
        if not offset <= size <= offset + len(payload):
            raise ValueError("unexpected decision journal length")
        if offset:
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                raise ValueError("pending decision has no prior record boundary")
        stream.seek(offset)
        existing = stream.read(size - offset)
        if not payload.startswith(existing):
            raise ValueError("decision journal conflicts with pending append")
        stream.write(payload[len(existing):])
        stream.flush()
        os.fsync(stream.fileno())
    atomic_write_json(checkpoint, value)
    pending.unlink()


def _publish_decision(checkpoint: Path, journal: Path, pending: Path,
                      value: dict, event: dict) -> None:
    if pending.exists() or pending.is_symlink():
        raise ValueError("previous decision transaction is still pending")
    offset = _decision_offset(journal)
    atomic_write_json(pending, {"checkpoint": value, "event": event, "offset": offset})
    _finish_decision(checkpoint, journal, pending)


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
    state.pop("last_hold_reason", None)
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
            _close_position(state, quote, now, reason, costs)
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


def _observed_capacity(depth_database: Path, result: dict, modeled_outcome: dict) -> bool:
    """Check both observed touches against the same modeled base quantity."""
    costs = TechnicalCosts()
    notional = Decimal(costs.initial_equity) * Decimal(costs.capital_fraction)
    is_long = result["action"] == "LONG"
    try:
        entry_price = Decimal(modeled_outcome["entry_price"])
        if not entry_price.is_finite() or entry_price <= 0:
            return False
        required_quantity = notional / entry_price
        with closing(sqlite3.connect(depth_database.resolve().as_uri() + "?mode=ro",
                                    uri=True, timeout=1)) as connection:
            for leg, ask in (("entry", is_long), ("exit", not is_long)):
                at = datetime.fromisoformat(result[f"{leg}_quote_received_at"])
                epoch = int(at.timestamp())
                rows = connection.execute(
                    "SELECT received_at,bid_quantity,ask_quantity"
                    " FROM execution_bbo WHERE market=? AND bucket_epoch BETWEEN ? AND ?"
                    " ORDER BY bucket_epoch DESC",
                    (result["market"], epoch - 2, epoch),
                )
                for received, bid_size, offer_size in rows:
                    if timedelta(0) <= at - datetime.fromisoformat(received) <= timedelta(seconds=2):
                        size = Decimal(offer_size if ask else bid_size)
                        if not size.is_finite() or size < required_quantity:
                            return False
                        break
                else:
                    return False
    except (sqlite3.Error, KeyError, TypeError, ValueError, ArithmeticError):
        return False
    return True


def _record_portfolio_observation(
    state: dict, event: dict, *, now: datetime, depth_database: Path | None = None,
) -> dict:
    """Advance either research account without interrupting the market worker."""
    btc_eth_only = depth_database is not None
    key = "btc_eth_portfolio_observer" if btc_eth_only else "portfolio_observer"
    existing = state.get(key)
    if btc_eth_only and existing is None:
        return {"status": "WAITING_FOR_EXPERIMENT_CHECKPOINT"}
    if existing is not None and (type(existing) is not dict or existing.get("schema") != PORTFOLIO_SCHEMA):
        return {"error_class": "PORTFOLIO_CHECKPOINT_SCHEMA_MISMATCH"}
    try:
        portfolio = deepcopy(existing) if existing is not None else new_portfolio_state(
            datetime.fromisoformat(event["observed_at"]))
        if btc_eth_only:
            changed = advance_portfolio(
                portfolio, event, now=now, markets=BTC_ETH_MARKETS,
                capacity_check=lambda result, outcome: _observed_capacity(depth_database, result, outcome),
            )
        else:
            changed = advance_portfolio(portfolio, event, now=now)
        report = {"queued": portfolio.get("queued"), "pending": portfolio.get("pending"),
                  "settled_count": portfolio["settled_count"], "unmatched_count": portfolio["unmatched_count"],
                  "evaluation": portfolio.get("evaluation")}
        if changed:
            portfolio["updated_at"] = now.isoformat()
            state[key] = portfolio
        return report
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        return {"error_class": type(exc).__name__}


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
    with ResourceLock(state_dir / "forward-shadow.lock"):
        return _run(args, checkpoint, stop)


def _run(args, checkpoint: Path, stop: Path) -> int:
    state_dir = checkpoint.parent
    journal = state_dir / "decisions.jsonl"
    pending = state_dir / "pending-decision.json"
    if pending.exists() or pending.is_symlink():
        _finish_decision(checkpoint, journal, pending)
    stop.unlink(missing_ok=True)
    client = WhiteBitPublicClient()
    state = None
    if checkpoint.is_file():
        state = json.loads(checkpoint.read_bytes()).get("state")
        if type(state) is dict:
            state.setdefault("episodes", [])
            state.setdefault("entries_by_utc_day", {})
            state.setdefault("fees", "0")
            state.setdefault("funding", "0")
    while not stop.exists():
        if _decision_offset(journal) >= MAX_DECISION_JOURNAL_BYTES:
            os.replace(journal, journal.with_suffix(journal.suffix + ".1"))
        now = datetime.now(UTC); started = monotonic(); error = None
        event = None
        try:
            protective_exit = None
            if state is not None and state["position"] is not None:
                quote = _quote(client)
                now = datetime.now(UTC)
                next_state = deepcopy(state)
                protective_exit = protect_open_position(next_state, quote, now)
                if protective_exit is not None:
                    review = next_state.get("wave_analysis")
                    closed = next_state["closed_cycles"]
                    event = {
                        "schema": SCHEMA, "event": "PROTECTIVE_EXIT", "observed_at": now.isoformat(),
                        "position_open": False, "episode": next_state["last_episode"],
                        "wave_analysis": (review if review and review["closed_through_cycle"] == closed
                                          else None),
                        "closed_cycles": closed, "net_pnl": next_state["net_pnl"],
                        "network_orders": False, "trading_authority": False,
                    }
                    state = next_state
            expected_epoch = int(now.timestamp()) // STEP_SECONDS * STEP_SECONDS - STEP_SECONDS
            if protective_exit is not None:
                status = "RUNNING"
            elif state is None:
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
                if state["position"] is not None:
                    action = "POSITION_MANAGEMENT"
                elif next_state["position"] is not None:
                    action = "VIRTUAL_ENTRY"
                else:
                    action = next_state.get("last_hold_reason", "HOLD")
                execution_outcomes = _observe_execution_outcomes(
                    next_state, boundary=history[-1]["closed_at"], quote=quote, now=now,
                    contiguous=next_state["last_bar_open_epoch"] == previous_epoch + STEP_SECONDS,
                    policy_action=action)
                additional_observations = _observe_additional_markets(
                    next_state, depth_database=args.depth_database,
                    boundary=history[-1]["closed_at"], quote=quote, now=now,
                    contiguous=next_state["last_bar_open_epoch"] == previous_epoch + STEP_SECONDS)
                pending_outcomes = next_state["execution_outcomes"]["pending"]
                current_observation = (pending_outcomes[-1] if pending_outcomes and
                    pending_outcomes[-1]["entry_boundary_epoch"] == int(history[-1]["closed_at"].timestamp())
                    else None)
                closed = next_state["closed_cycles"]
                review = next_state.get("wave_analysis")
                event = {
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
                    "execution_outcomes": execution_outcomes,
                    "execution_observation": current_observation,
                    "observed_cost_quotes": quote.get("observed_cost_quotes", {}),
                    "point_in_time_futures": quote.get("point_in_time_futures", {}),
                    "additional_market_research": additional_observations,
                    "network_orders": False, "trading_authority": False,
                }
                event["portfolio_observer"] = _record_portfolio_observation(
                    next_state, event, now=datetime.now(UTC))
                if "btc_eth_portfolio_observer" in next_state:
                    event["btc_eth_portfolio_observer"] = _record_portfolio_observation(
                        next_state, event, now=datetime.now(UTC), depth_database=args.depth_database)
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
        if error is None and event is not None:
            _publish_decision(checkpoint, journal, pending, value, event)
        else:
            atomic_write_json(checkpoint, value)
        if args.once: return 0 if error is None else 2
        delay = args.poll_seconds
        if error is None and state is not None and state["position"] is not None:
            opened = datetime.fromisoformat(state["position"]["opened_at"])
            time_left = (opened + timedelta(seconds=configurations()[0].maximum_hold_bars
                * STEP_SECONDS) - datetime.now(UTC)).total_seconds()
            delay = min(delay, max(1, time_left))
        deadline = monotonic() + delay
        while not stop.exists() and monotonic() < deadline:
            sleep(min(5, max(1, deadline - monotonic())))
    atomic_write_json(checkpoint, {**value, "status": "STOPPED",
        "updated_at": datetime.now(UTC).isoformat()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
