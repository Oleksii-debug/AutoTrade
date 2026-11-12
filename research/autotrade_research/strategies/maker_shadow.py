"""Conservative public-data maker fill shadow for one WhiteBIT spot market."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json

from ..integrations.whitebit_public import PublicBook, PublicInstrument, PublicTradeBatch


SCHEMA = "autotrade-whitebit-maker-shadow-v1"


def _decimal(value: object, *, name: str, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} is invalid") from None
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ValueError(f"{name} is invalid")
    return parsed


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("quote timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def open_quote(
    book: PublicBook, instrument: PublicInstrument, batch: PublicTradeBatch,
    *, notional_quote: str = "100",
) -> dict[str, object]:
    if type(book) is not PublicBook or type(instrument) is not PublicInstrument:
        raise TypeError("canonical public book and instrument are required")
    if type(batch) is not PublicTradeBatch or batch.market != book.market:
        raise TypeError("matching canonical public trade batch is required")
    if instrument.market != book.market or not instrument.trades_enabled:
        raise ValueError("enabled instrument and book must match")
    notional = _decimal(notional_quote, name="notional_quote", positive=True)
    bid, bid_queue = map(Decimal, book.bids[0])
    ask, ask_queue = map(Decimal, book.asks[0])
    if min(bid, ask, bid_queue, ask_queue) <= 0 or bid >= ask:
        raise ValueError("top of book is invalid")
    quantity = notional / ((bid + ask) / Decimal(2))
    anchor = max((trade.trade_id for trade in batch.trades), default=0)
    payload = {
        "schema": SCHEMA,
        "market": book.market,
        "placed_at": book.received_at.astimezone(timezone.utc).isoformat(),
        "bid": format(bid, "f"), "ask": format(ask, "f"),
        "bid_queue_ahead": format(bid_queue, "f"),
        "ask_queue_ahead": format(ask_queue, "f"),
        "quantity": format(quantity, "f"),
        "notional_quote": format(notional, "f"),
        "maker_fee_rate": instrument.maker_fee_rate,
        "taker_fee_rate": instrument.taker_fee_rate,
        "anchor_trade_id": anchor,
        "book_sha256": book.digest,
        "trade_batch_sha256": batch.digest,
        "status": "OPEN",
        "network_orders": False,
        "trading_authority": False,
    }
    return {**payload, "quote_sha256": sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()}


def evaluate_quote(
    quote: dict[str, object], book: PublicBook, batch: PublicTradeBatch,
    *, now: datetime, maximum_age_seconds: int = 60,
    forced_exit_impact_bps: str = "5",
) -> dict[str, object]:
    if type(quote) is not dict or quote.get("schema") != SCHEMA:
        raise ValueError("maker quote schema differs")
    if (quote.get("network_orders") is not False
            or quote.get("trading_authority") is not False):
        raise ValueError("maker quote authority differs")
    if type(book) is not PublicBook or type(batch) is not PublicTradeBatch:
        raise TypeError("canonical public inputs are required")
    if book.market != quote.get("market") or batch.market != quote.get("market"):
        raise ValueError("maker quote market differs")
    if type(maximum_age_seconds) is not int or not 10 <= maximum_age_seconds <= 3600:
        raise ValueError("maximum quote age is invalid")
    current = now.astimezone(timezone.utc)
    age = (current - _instant(str(quote["placed_at"]))).total_seconds()
    if age < 0:
        raise ValueError("clock moved backward")
    bid = _decimal(quote["bid"], name="bid", positive=True)
    ask = _decimal(quote["ask"], name="ask", positive=True)
    quantity = _decimal(quote["quantity"], name="quantity", positive=True)
    bid_required = _decimal(quote["bid_queue_ahead"], name="bid_queue") + quantity
    ask_required = _decimal(quote["ask_queue_ahead"], name="ask_queue") + quantity
    anchor = int(quote["anchor_trade_id"])
    eligible = [trade for trade in batch.trades
                if trade.trade_id > anchor and trade.trade_at >= _instant(str(quote["placed_at"]))]
    sell_volume = sum((_decimal(t.base_quantity, name="trade_quantity") for t in eligible
                       if t.side == "sell" and Decimal(t.price) <= bid), Decimal(0))
    buy_volume = sum((_decimal(t.base_quantity, name="trade_quantity") for t in eligible
                      if t.side == "buy" and Decimal(t.price) >= ask), Decimal(0))
    bid_filled = sell_volume >= bid_required
    ask_filled = buy_volume >= ask_required
    expired = age >= maximum_age_seconds
    if not expired and not (bid_filled and ask_filled):
        return {
            **quote, "status": "OPEN", "age_seconds": age,
            "sell_volume_at_or_below_bid": format(sell_volume, "f"),
            "buy_volume_at_or_above_ask": format(buy_volume, "f"),
            "bid_filled": bid_filled, "ask_filled": ask_filled,
            "latest_book_sha256": book.digest,
            "latest_trade_batch_sha256": batch.digest,
        }
    maker = _decimal(quote["maker_fee_rate"], name="maker_fee_rate")
    taker = _decimal(quote["taker_fee_rate"], name="taker_fee_rate")
    impact = _decimal(forced_exit_impact_bps, name="forced_exit_impact_bps") / 10000
    if min(maker, taker, impact) < 0:
        raise ValueError("maker shadow economics are invalid")
    with localcontext() as context:
        context.prec = 80
        if bid_filled and ask_filled:
            gross = quantity * (ask - bid)
            fees = quantity * (bid + ask) * maker
            outcome = "BOTH_SIDES_FILLED"
        elif bid_filled:
            exit_price = Decimal(book.bids[0][0]) * (1 - impact)
            gross = quantity * (exit_price - bid)
            fees = quantity * (bid * maker + exit_price * taker)
            outcome = "BID_ONLY_FORCED_EXIT"
        elif ask_filled:
            exit_price = Decimal(book.asks[0][0]) * (1 + impact)
            gross = quantity * (ask - exit_price)
            fees = quantity * (ask * maker + exit_price * taker)
            outcome = "ASK_ONLY_FORCED_EXIT"
        else:
            gross = fees = Decimal(0)
            outcome = "NO_FILL_EXPIRED"
        net = gross - fees
    result = {
        **quote, "status": "SETTLED", "settled_at": current.isoformat(),
        "age_seconds": age, "outcome": outcome,
        "bid_filled": bid_filled, "ask_filled": ask_filled,
        "sell_volume_at_or_below_bid": format(sell_volume, "f"),
        "buy_volume_at_or_above_ask": format(buy_volume, "f"),
        "gross_pnl_quote": format(gross, "f"),
        "fees_quote": format(fees, "f"),
        "net_pnl_quote": format(net, "f"),
        "forced_exit_impact_bps": format(impact * 10000, "f"),
        "latest_book_sha256": book.digest,
        "latest_trade_batch_sha256": batch.digest,
        "limitations": ["REST_TRADE_SAMPLING", "QUEUE_AHEAD_STATIC_AT_ENTRY",
                        "PUBLIC_BOOK_EXCLUDES_RPI", "SIMULATION_ONLY"],
        "network_orders": False, "trading_authority": False,
    }
    result["result_sha256"] = sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return result

