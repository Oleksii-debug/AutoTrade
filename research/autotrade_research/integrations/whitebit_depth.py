"""Public WhiteBIT L2 depth stream with exact gap recovery.

The official channel publishes incremental deltas about every 100 ms.  A
delta is admitted only when ``past_update_id`` equals the locally applied
``update_id``.  Any mismatch forces a reconnect and a fresh full snapshot.
No credential or order surface is reachable from this module.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json

from .whitebit_public import PublicDataError, _decimal, _symbol, decode_public_json
from .whitebit_websocket import (
    MAX_WEBSOCKET_MESSAGE_BYTES,
    WhiteBitPublicWebSocketTransport,
    WhiteBitWebSocketControl,
    _epoch_time,
    _raw_bytes,
)


SCHEMA = "autotrade-whitebit-public-depth-v1"
SOURCE_URI = "wss://wss.whitebit.com/ws#depth"
MAX_DEPTH_CLOCK_SKEW = timedelta(seconds=5)
# WhiteBIT sends a snapshot after 10 seconds without an incremental update.
DEPTH_UPDATE_TIMEOUT_SECONDS = 15


class DepthSequenceGap(PublicDataError):
    """The incremental chain no longer connects to the local snapshot."""


@dataclass(frozen=True, slots=True)
class WhiteBitDepthUpdate:
    market: str
    snapshot: bool
    transaction_time: datetime
    message_time: datetime
    received_at: datetime
    update_id: int
    past_update_id: int | None
    asks: tuple[tuple[str, str], ...]
    bids: tuple[tuple[str, str], ...]
    raw: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.raw).hexdigest()


@dataclass(frozen=True, slots=True)
class DepthBookObservation:
    market: str
    received_at: datetime
    generation: int
    update_id: int
    past_update_id: int | None
    snapshot: bool
    bid_price: str
    ask_price: str
    mid_price: str
    spread_bps: str
    bid_depth_top5: str
    ask_depth_top5: str
    imbalance_top5: str
    bid_quantity_change: str
    ask_quantity_change: str
    order_flow_imbalance: str
    source_sha256: str
    bid_quantity: str | None = None
    ask_quantity: str | None = None
    message_time: datetime | None = None


def _levels(value: object, side: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if type(value) is not list or len(value) > 100:
        raise PublicDataError("INVALID_DEPTH_LEVELS")
    result = []
    prices = set()
    for level in value:
        if type(level) is not list or len(level) != 2:
            raise PublicDataError("INVALID_DEPTH_LEVEL")
        price = _decimal(level[0], positive=True)
        amount = _decimal(level[1], positive=False)
        if amount < 0 or price in prices:
            raise PublicDataError("INVALID_DEPTH_LEVEL")
        prices.add(price)
        result.append((format(price, "f"), format(amount, "f")))
    ordered = sorted(result, key=lambda row: Decimal(row[0]),
                     reverse=side == "bid")
    if ordered != result:
        raise PublicDataError("UNSORTED_DEPTH_LEVELS")
    return tuple(result)


def parse_depth_message(raw: str | bytes, *, market: str,
                        received_at: datetime) -> WhiteBitDepthUpdate | WhiteBitWebSocketControl:
    expected_market = _symbol(market)
    if type(received_at) is not datetime or type(received_at.tzinfo) is not timezone:
        raise PublicDataError("UTC_TIMESTAMP_REQUIRED")
    raw_value = _raw_bytes(raw)
    message = decode_public_json(raw_value)
    if type(message) is not dict:
        raise PublicDataError("INVALID_WEBSOCKET_SCHEMA")
    if set(message) == {"id", "result", "error"}:
        request_id = message["id"]
        if type(request_id) is not int or request_id < 0 or message["error"] is not None:
            raise PublicDataError("WEBSOCKET_PROVIDER_ERROR")
        if message["result"] == "pong":
            return WhiteBitWebSocketControl("PONG", request_id)
        if message["result"] == {"status": "success"}:
            return WhiteBitWebSocketControl("SUBSCRIBED", request_id)
        raise PublicDataError("INVALID_WEBSOCKET_CONTROL_RESULT")
    if set(message) != {"id", "method", "params"} or message["id"] is not None:
        raise PublicDataError("INVALID_WEBSOCKET_SCHEMA")
    if message["method"] != "depth_update":
        raise PublicDataError("UNEXPECTED_WEBSOCKET_METHOD")
    params = message["params"]
    if type(params) is not list:
        raise PublicDataError("INVALID_DEPTH_PARAMS")
    if len(params) == 3 and type(params[0]) is bool:
        snapshot, data, provider_market = params
    elif len(params) == 2 and type(params[0]) is dict:
        data, provider_market = params
        snapshot = data.get("past_update_id") is None
    else:
        raise PublicDataError("INVALID_DEPTH_PARAMS")
    if provider_market != expected_market or type(data) is not dict:
        raise PublicDataError("WEBSOCKET_MARKET_MISMATCH")
    required = {"timestamp", "event_time", "update_id"}
    if not required.issubset(data) or not set(data).issubset(
            required | {"past_update_id", "asks", "bids"}):
        raise PublicDataError("INVALID_DEPTH_OBJECT")
    update_id = data["update_id"]
    past = data.get("past_update_id")
    if type(update_id) is not int or update_id < 0:
        raise PublicDataError("INVALID_DEPTH_UPDATE_ID")
    if past is not None and (type(past) is not int or past < 0 or past >= update_id):
        raise PublicDataError("INVALID_DEPTH_PAST_UPDATE_ID")
    if snapshot != (past is None):
        raise PublicDataError("DEPTH_SNAPSHOT_FLAG_CONFLICT")
    # Provider clocks can lead the receiving host by well below one second.
    # Availability remains the local receive time; only admit a bounded skew.
    transaction_time = _epoch_time(
        data["timestamp"], received_at=received_at,
        future_tolerance=MAX_DEPTH_CLOCK_SKEW,
    )
    message_time = _epoch_time(
        data["event_time"], received_at=received_at,
        future_tolerance=MAX_DEPTH_CLOCK_SKEW,
    )
    return WhiteBitDepthUpdate(expected_market, snapshot, transaction_time,
        message_time, received_at, update_id, past,
        _levels(data.get("asks", []), "ask"), _levels(data.get("bids", []), "bid"),
        raw_value)


class DepthLocalBook:
    def __init__(self, market: str, *, limit: int = 20) -> None:
        if type(limit) is not int or limit not in {1, 5, 10, 20, 50, 100}:
            raise ValueError("depth limit is unsupported")
        self.market = _symbol(market); self.limit = limit
        self.asks: dict[Decimal, Decimal] = {}; self.bids: dict[Decimal, Decimal] = {}
        self.update_id: int | None = None; self.generation: int | None = None

    @staticmethod
    def _apply_side(book: dict[Decimal, Decimal], rows) -> Decimal:
        change = Decimal(0)
        for price_text, amount_text in rows:
            price, amount = Decimal(price_text), Decimal(amount_text)
            previous = book.get(price, Decimal(0))
            change += amount - previous
            if amount == 0:
                book.pop(price, None)
            else:
                book[price] = amount
        return change

    def apply(self, update: WhiteBitDepthUpdate, *, generation: int) -> DepthBookObservation:
        if update.market != self.market or type(generation) is not int or generation < 1:
            raise ValueError("depth update binding is invalid")
        previous_asks = sorted(self.asks.items())[:1]
        previous_bids = sorted(self.bids.items(), reverse=True)[:1]
        if update.snapshot:
            self.asks = {Decimal(p): Decimal(q) for p, q in update.asks}
            self.bids = {Decimal(p): Decimal(q) for p, q in update.bids}
            bid_change = ask_change = Decimal(0)
        else:
            if (self.generation != generation or self.update_id is None
                    or update.past_update_id != self.update_id):
                raise DepthSequenceGap("DEPTH_SEQUENCE_GAP")
            ask_change = self._apply_side(self.asks, update.asks)
            bid_change = self._apply_side(self.bids, update.bids)
        self.generation = generation; self.update_id = update.update_id
        asks = sorted(self.asks.items())[:self.limit]
        bids = sorted(self.bids.items(), reverse=True)[:self.limit]
        self.asks = dict(asks); self.bids = dict(bids)
        if not asks or not bids or bids[0][0] >= asks[0][0]:
            raise PublicDataError("INVALID_LOCAL_DEPTH_BOOK")
        bid, ask = bids[0][0], asks[0][0]
        bid_quantity, ask_quantity = bids[0][1], asks[0][1]
        if update.snapshot or not previous_bids or not previous_asks:
            order_flow_imbalance = Decimal(0)
        else:
            previous_bid, previous_bid_quantity = previous_bids[0]
            previous_ask, previous_ask_quantity = previous_asks[0]
            # Cont-Kukanov-Stoikov event-level OFI at the best quotes.
            order_flow_imbalance = (
                (bid_quantity if bid >= previous_bid else Decimal(0))
                - (previous_bid_quantity if bid <= previous_bid else Decimal(0))
                - (ask_quantity if ask <= previous_ask else Decimal(0))
                + (previous_ask_quantity if ask >= previous_ask else Decimal(0)))
        mid = (bid + ask) / Decimal(2)
        bid_depth = sum((q for _, q in bids[:5]), Decimal(0))
        ask_depth = sum((q for _, q in asks[:5]), Decimal(0))
        total = bid_depth + ask_depth
        imbalance = Decimal(0) if total == 0 else (bid_depth - ask_depth) / total
        return DepthBookObservation(self.market, update.received_at, generation,
            update.update_id, update.past_update_id, update.snapshot,
            format(bid, "f"), format(ask, "f"), format(mid, "f"),
            format((ask - bid) / mid * Decimal(10000), "f"),
            format(bid_depth, "f"), format(ask_depth, "f"), format(imbalance, "f"),
            format(bid_change, "f"), format(ask_change, "f"),
            format(order_flow_imbalance, "f"), update.digest,
            bid_quantity=format(bid_quantity, "f"), ask_quantity=format(ask_quantity, "f"),
            message_time=update.message_time)


class WhiteBitDepthWebSocketTransport(WhiteBitPublicWebSocketTransport):
    def __init__(self, *, market: str, limit: int = 20, **kwargs) -> None:
        super().__init__(market=market, **kwargs)
        self.limit = limit
        DepthLocalBook(market, limit=limit)

    async def observations(self, stop: asyncio.Event) -> AsyncIterator[DepthBookObservation]:
        if type(stop) is not asyncio.Event:
            raise TypeError("stop must be an exact asyncio.Event")
        loop = asyncio.get_running_loop()
        attempt = generation = 0; backoff = self.initial_backoff_seconds
        while not stop.is_set():
            attempt += 1; subscribed = False
            failure_code = "CONNECTION_LOST"
            heartbeat = None
            self._emit("CONNECTING", attempt=attempt, generation=generation)
            try:
                context = await self._open()
                async with context as socket:
                    generation += 1; book = DepthLocalBook(self.market, limit=self.limit)
                    request_id = self._next_request_id()
                    await socket.send(json.dumps({"id": request_id,
                        "method": "depth_subscribe",
                        "params": [self.market, self.limit, "0", True]},
                        separators=(",", ":")))
                    heartbeat = asyncio.create_task(self._heartbeat(socket, stop))
                    deadline = loop.time() + self.acknowledgement_timeout_seconds
                    while not stop.is_set():
                        if loop.time() >= deadline:
                            if not subscribed:
                                raise PublicDataError("WEBSOCKET_SUBSCRIBE_TIMEOUT")
                            failure_code = "DEPTH_UPDATE_TIMEOUT"
                            break
                        read_timeout = asyncio.timeout_at(deadline)
                        try:
                            async with read_timeout:
                                message = await self._receive(socket, stop, heartbeat)
                        except TimeoutError:
                            if subscribed and read_timeout.expired():
                                failure_code = "DEPTH_UPDATE_TIMEOUT"
                            raise
                        if message is None: break
                        parsed = parse_depth_message(message, market=self.market,
                            received_at=datetime.now(timezone.utc))
                        if type(parsed) is WhiteBitWebSocketControl:
                            if parsed.code == "SUBSCRIBED":
                                if parsed.request_id != request_id or subscribed:
                                    raise PublicDataError("UNEXPECTED_WEBSOCKET_SUBSCRIPTION_ACK")
                                subscribed = True
                                deadline = loop.time() + DEPTH_UPDATE_TIMEOUT_SECONDS
                                self._emit("SUBSCRIBED", attempt=attempt, generation=generation)
                            continue
                        if not subscribed:
                            raise PublicDataError("DEPTH_BEFORE_SUBSCRIPTION_ACK")
                        try:
                            observation = book.apply(parsed, generation=generation)
                        except DepthSequenceGap:
                            raise PublicDataError("DEPTH_SEQUENCE_GAP") from None
                        backoff = self.initial_backoff_seconds
                        deadline = loop.time() + DEPTH_UPDATE_TIMEOUT_SECONDS
                        yield observation
                    if subscribed and stop.is_set():
                        try:
                            unsubscribe_id = self._next_request_id()
                            await asyncio.wait_for(socket.send(json.dumps({
                                "id": unsubscribe_id,
                                "method": "depth_unsubscribe",
                                "params": [self.market],
                            }, separators=(",", ":"))), timeout=1)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except PublicDataError:
                failure_code = "SCHEMA_REJECTED"
            except Exception:
                pass
            finally:
                if heartbeat is not None:
                    heartbeat.cancel(); await asyncio.gather(heartbeat, return_exceptions=True)
            if stop.is_set(): break
            self._emit(failure_code, attempt=attempt, generation=generation)
            self._emit("RECONNECT_WAIT", attempt=attempt, generation=generation, retry=backoff)
            try: await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError: pass
            backoff = min(self.maximum_backoff_seconds, backoff * 2)
        self._emit("STOPPED", attempt=attempt, generation=generation)


