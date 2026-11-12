"""Bounded cost/depth/activity gate for WhiteBIT maker-shadow discovery."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json

from ..integrations.whitebit_public import (
    PublicBook,
    PublicInstrument,
    PublicTicker,
    PublicTradeBatch,
)


SCHEMA = "autotrade-whitebit-maker-universe-screen-v1"


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} is invalid") from None
    if not result.is_finite():
        raise ValueError(f"{name} is invalid")
    return result


def evaluate_market(
    instrument: PublicInstrument,
    ticker: PublicTicker,
    book: PublicBook,
    trades: PublicTradeBatch,
    *,
    now: datetime,
    notional_quote: str = "100",
    adverse_selection_reserve_bps: str = "5",
    activity_window_seconds: int = 60,
    minimum_recent_trades: int = 3,
    minimum_recent_quote_volume: str = "100",
    maximum_absolute_flow_imbalance: str = "0.20",
) -> dict[str, object]:
    """Evaluate one market without treating displayed spread as executable P/L."""
    if not all((
        type(instrument) is PublicInstrument,
        type(ticker) is PublicTicker,
        type(book) is PublicBook,
        type(trades) is PublicTradeBatch,
    )):
        raise TypeError("canonical WhiteBIT public inputs are required")
    if not (instrument.market == ticker.market == book.market == trades.market):
        raise ValueError("maker-universe markets differ")
    if instrument.instrument_type != "spot" or instrument.quote_asset != "USDT":
        raise ValueError("maker-universe requires a USDT spot instrument")
    if not instrument.trades_enabled or ticker.is_frozen:
        raise ValueError("maker-universe instrument is not currently enabled")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("maker-universe clock must be timezone-aware")
    now = now.astimezone(timezone.utc)
    book.assert_fresh(now, maximum_age_seconds=30)
    if trades.received_at > now or ticker.received_at > now:
        raise ValueError("maker-universe input is from the future")
    if type(activity_window_seconds) is not int or not 10 <= activity_window_seconds <= 300:
        raise ValueError("activity window must be 10..300 seconds")
    if type(minimum_recent_trades) is not int or not 1 <= minimum_recent_trades <= 100:
        raise ValueError("minimum recent trades must be 1..100")

    notional = _decimal(notional_quote, "notional_quote")
    reserve = _decimal(adverse_selection_reserve_bps, "reserve_bps")
    minimum_flow = _decimal(minimum_recent_quote_volume, "minimum_recent_quote_volume")
    maximum_imbalance = _decimal(maximum_absolute_flow_imbalance, "maximum_flow_imbalance")
    if notional <= 0 or reserve < 0 or minimum_flow < 0 or not 0 <= maximum_imbalance <= 1:
        raise ValueError("maker-universe economics are invalid")

    with localcontext() as context:
        context.prec = 80
        bid, bid_quantity = map(Decimal, book.bids[0])
        ask, ask_quantity = map(Decimal, book.asks[0])
        midpoint = (bid + ask) / 2
        spread_bps = (ask - bid) / midpoint * Decimal(10000)
        maker_fee_bps = _decimal(instrument.maker_fee_rate, "maker_fee_rate") * Decimal(10000)
        fee_hurdle_bps = maker_fee_bps * 2
        net_spread_bps = spread_bps - fee_hurdle_bps - reserve
        bid_capacity = bid * bid_quantity
        ask_capacity = ask * ask_quantity
        required_quantity = notional / midpoint

        recent = tuple(
            trade for trade in trades.trades
            if 0 <= (trades.received_at - trade.trade_at).total_seconds()
            <= activity_window_seconds
        )
        buy = sum((Decimal(row.quote_quantity) for row in recent if row.side == "buy"), Decimal(0))
        sell = sum((Decimal(row.quote_quantity) for row in recent if row.side == "sell"), Decimal(0))
        flow = buy + sell
        imbalance = Decimal(0) if flow == 0 else (buy - sell) / flow

    reasons: list[str] = []
    if Decimal(ticker.quote_volume) < notional * Decimal(1000):
        reasons.append("INSUFFICIENT_24H_QUOTE_VOLUME")
    if bid_capacity < notional or ask_capacity < notional:
        reasons.append("INSUFFICIENT_TOP_DEPTH")
    if Decimal(instrument.minimum_notional) > notional or Decimal(instrument.minimum_quantity) > required_quantity:
        reasons.append("NOTIONAL_BELOW_INSTRUMENT_MINIMUM")
    if len(recent) < minimum_recent_trades or flow < minimum_flow:
        reasons.append("INSUFFICIENT_RECENT_ACTIVITY")
    if abs(imbalance) > maximum_imbalance:
        reasons.append("DIRECTIONAL_RECENT_FLOW")
    if net_spread_bps <= 0:
        reasons.append("NO_SPREAD_AFTER_COST_RESERVE")

    result = {
        "schema": SCHEMA,
        "market": instrument.market,
        "observed_at": now.isoformat(),
        "quote_volume_24h": ticker.quote_volume,
        "notional_quote": format(notional, "f"),
        "bid": format(bid, "f"),
        "ask": format(ask, "f"),
        "bid_top_capacity_quote": format(bid_capacity, "f"),
        "ask_top_capacity_quote": format(ask_capacity, "f"),
        "spread_bps": format(spread_bps, "f"),
        "two_maker_fees_bps": format(fee_hurdle_bps, "f"),
        "adverse_selection_reserve_bps": format(reserve, "f"),
        "net_spread_after_cost_reserve_bps": format(net_spread_bps, "f"),
        "activity_window_seconds": activity_window_seconds,
        "recent_trade_count": len(recent),
        "recent_trade_quote_volume": format(flow, "f"),
        "recent_flow_imbalance": format(imbalance, "f"),
        "maximum_absolute_flow_imbalance": format(maximum_imbalance, "f"),
        "candidate": not reasons,
        "rejection_reasons": reasons,
        "book_sha256": book.digest,
        "trade_batch_sha256": trades.digest,
        "automatic_promotion": False,
        "research_only": True,
        "network_orders": False,
        "trading_authority": False,
        "profit_guaranteed": False,
    }
    result["result_sha256"] = sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return result


def rank_candidates(rows: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    if type(rows) is not list or type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("maker-universe ranking configuration is invalid")
    for row in rows:
        if type(row) is not dict or row.get("schema") != SCHEMA:
            raise ValueError("maker-universe result schema differs")
    candidates = [row for row in rows if row.get("candidate") is True]
    candidates.sort(key=lambda row: (
        -_decimal(row["net_spread_after_cost_reserve_bps"], "net_spread"),
        -_decimal(row["recent_trade_quote_volume"], "recent_volume"),
        str(row["market"]),
    ))
    return candidates[:limit]
