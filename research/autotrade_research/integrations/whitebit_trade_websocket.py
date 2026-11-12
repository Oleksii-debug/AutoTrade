"""Reconnectable public WhiteBIT trade stream with canonical trade batches."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import inspect
import json
from typing import Any, AsyncIterator, Callable

from .whitebit_public import (
    PublicDataError,
    PublicTrade,
    PublicTradeBatch,
    _decimal,
    _symbol,
    _utc,
)
from .whitebit_websocket import (
    MAX_WEBSOCKET_MESSAGE_BYTES,
    WS,
    _default_connector,
)


MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=5)


@dataclass(frozen=True)
class TradeStreamControl:
    code: str
    request_id: int


def parse_trade_stream_message(
    message: str | bytes,
    *,
    markets: tuple[str, ...],
    received_at: datetime,
) -> TradeStreamControl | PublicTradeBatch:
    """Validate one JSON-RPC response or public ``trades_update`` event."""
    received_at = _utc(received_at)
    allowed = tuple(_symbol(market) for market in markets)
    if not allowed or len(set(allowed)) != len(allowed) or len(allowed) > 100:
        raise PublicDataError("INVALID_TRADE_STREAM_MARKETS")
    if type(message) is str:
        raw = message.encode("utf-8")
    elif type(message) is bytes:
        raw = message
    else:
        raise PublicDataError("INVALID_TRADE_STREAM_MESSAGE")
    if not 0 < len(raw) <= MAX_WEBSOCKET_MESSAGE_BYTES:
        raise PublicDataError("TRADE_STREAM_MESSAGE_SIZE")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicDataError("INVALID_TRADE_STREAM_JSON") from None
    if type(document) is not dict:
        raise PublicDataError("INVALID_TRADE_STREAM_ENVELOPE")

    if set(document) == {"id", "result", "error"}:
        request_id = document["id"]
        if type(request_id) is not int or request_id < 0 or document["error"] is not None:
            raise PublicDataError("TRADE_STREAM_CONTROL_ERROR")
        if document["result"] == {"status": "success"}:
            return TradeStreamControl("SUBSCRIBED", request_id)
        if document["result"] == "pong":
            return TradeStreamControl("PONG", request_id)
        raise PublicDataError("INVALID_TRADE_STREAM_CONTROL")

    if set(document) not in ({"method", "params"}, {"id", "method", "params"}):
        raise PublicDataError("INVALID_TRADE_STREAM_ENVELOPE")
    if document.get("id") is not None or document.get("method") != "trades_update":
        raise PublicDataError("UNEXPECTED_TRADE_STREAM_EVENT")
    params = document.get("params")
    if type(params) is not list or len(params) != 2:
        raise PublicDataError("INVALID_TRADE_STREAM_PARAMS")
    market = _symbol(params[0])
    if market not in allowed:
        raise PublicDataError("UNSUBSCRIBED_TRADE_STREAM_MARKET")
    records = params[1]
    if type(records) is not list or not 1 <= len(records) <= 100:
        raise PublicDataError("INVALID_TRADE_STREAM_BATCH")

    trades: list[PublicTrade] = []
    seen: set[int] = set()
    with localcontext() as context:
        context.prec = 80
        for record in records:
            if type(record) is not dict or not {"id", "time", "price", "amount", "type"} <= set(record):
                raise PublicDataError("INVALID_TRADE_STREAM_RECORD")
            if set(record) - {"id", "time", "price", "amount", "type", "rpi"}:
                raise PublicDataError("INVALID_TRADE_STREAM_RECORD")
            trade_id = record["id"]
            if type(trade_id) is not int or not 1 <= trade_id <= 9_999_999_999_999_999:
                raise PublicDataError("INVALID_TRADE_STREAM_ID")
            if trade_id in seen:
                raise PublicDataError("DUPLICATE_TRADE_STREAM_ID")
            seen.add(trade_id)
            seconds = record["time"]
            if type(seconds) not in (int, float) or not 0 < seconds < 10_000_000_000:
                raise PublicDataError("INVALID_TRADE_STREAM_TIME")
            try:
                trade_at = datetime.fromtimestamp(seconds, timezone.utc)
            except (OSError, OverflowError, ValueError):
                raise PublicDataError("INVALID_TRADE_STREAM_TIME") from None
            # Venue and local wall clocks can differ slightly.  A small fixed
            # tolerance prevents a valid public execution from forcing a full
            # reconnect while still rejecting materially future-dated input.
            if trade_at > received_at + MAX_FUTURE_CLOCK_SKEW:
                raise PublicDataError("FUTURE_TRADE_STREAM_EVENT")
            side = record["type"]
            rpi = record.get("rpi", False)
            if side not in {"buy", "sell"} or type(rpi) is not bool:
                raise PublicDataError("INVALID_TRADE_STREAM_CLASSIFICATION")
            price = _decimal(record["price"], positive=True)
            amount = _decimal(record["amount"], positive=True)
            trades.append(PublicTrade(
                market=market,
                trade_id=trade_id,
                price=format(price, "f"),
                base_quantity=format(amount, "f"),
                quote_quantity=format(price * amount, "f"),
                trade_at=trade_at,
                side=side,
                rpi=rpi,
            ))
    trades.sort(key=lambda trade: trade.trade_id, reverse=True)
    return PublicTradeBatch(
        market=market,
        received_at=received_at,
        trades=tuple(trades),
        raw=raw,
        source_uri=WS + "#trades_update",
    )


class WhiteBitPublicTradeWebSocketTransport:
    """One reconnecting public connection for up to 100 trade markets."""

    def __init__(
        self,
        *,
        markets: tuple[str, ...],
        connector: Callable[..., Any] | None = None,
        heartbeat_seconds: float = 45.0,
        acknowledgement_timeout_seconds: float = 10.0,
        initial_backoff_seconds: float = 1.0,
        maximum_backoff_seconds: float = 30.0,
        diagnostic: Callable[[str], None] | None = None,
    ) -> None:
        self.markets = tuple(_symbol(market) for market in markets)
        if not 1 <= len(self.markets) <= 100 or len(set(self.markets)) != len(self.markets):
            raise ValueError("one to one hundred unique markets are required")
        for name, value, low, high in (
            ("heartbeat_seconds", heartbeat_seconds, 0.01, 50.0),
            ("acknowledgement_timeout_seconds", acknowledgement_timeout_seconds, 0.01, 60.0),
            ("initial_backoff_seconds", initial_backoff_seconds, 0.01, 300.0),
            ("maximum_backoff_seconds", maximum_backoff_seconds, 0.01, 300.0),
        ):
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError(f"{name} is outside the bounded range")
        if initial_backoff_seconds > maximum_backoff_seconds:
            raise ValueError("initial backoff cannot exceed maximum backoff")
        if connector is not None and not callable(connector):
            raise TypeError("connector must be callable")
        if diagnostic is not None and not callable(diagnostic):
            raise TypeError("diagnostic must be callable")
        self._connector = connector or _default_connector
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.acknowledgement_timeout_seconds = float(acknowledgement_timeout_seconds)
        self.initial_backoff_seconds = float(initial_backoff_seconds)
        self.maximum_backoff_seconds = float(maximum_backoff_seconds)
        self._diagnostic = diagnostic
        self._request_id = 0

    def _emit(self, code: str) -> None:
        if self._diagnostic is not None:
            self._diagnostic(code)

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _open(self):
        result = self._connector(
            WS,
            open_timeout=self.acknowledgement_timeout_seconds,
            close_timeout=5,
            ping_interval=None,
            max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
            max_queue=256,
        )
        return await result if inspect.isawaitable(result) else result

    async def _heartbeat(self, socket, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
                return
            except TimeoutError:
                pass
            await socket.send(json.dumps(
                {"id": self._next_request_id(), "method": "ping", "params": []},
                separators=(",", ":"),
            ))

    async def batches(self, stop: asyncio.Event) -> AsyncIterator[PublicTradeBatch]:
        if type(stop) is not asyncio.Event:
            raise TypeError("stop must be an exact asyncio.Event")
        backoff = self.initial_backoff_seconds
        while not stop.is_set():
            subscribed = False
            heartbeat = None
            schema_failure = False
            try:
                context = await self._open()
                async with context as socket:
                    subscribe_id = self._next_request_id()
                    await socket.send(json.dumps({
                        "id": subscribe_id,
                        "method": "trades_subscribe",
                        "params": list(self.markets),
                    }, separators=(",", ":")))
                    heartbeat = asyncio.create_task(self._heartbeat(socket, stop))
                    while not stop.is_set():
                        try:
                            message = await asyncio.wait_for(
                                socket.recv(),
                                timeout=None if subscribed else self.acknowledgement_timeout_seconds,
                            )
                        except TimeoutError:
                            raise PublicDataError("TRADE_STREAM_SUBSCRIBE_TIMEOUT") from None
                        parsed = parse_trade_stream_message(
                            message, markets=self.markets,
                            received_at=datetime.now(timezone.utc),
                        )
                        if type(parsed) is TradeStreamControl:
                            if parsed.code == "SUBSCRIBED":
                                if subscribed or parsed.request_id != subscribe_id:
                                    raise PublicDataError("UNEXPECTED_TRADE_STREAM_ACK")
                                subscribed = True
                                backoff = self.initial_backoff_seconds
                                self._emit("SUBSCRIBED")
                            continue
                        if not subscribed:
                            raise PublicDataError("TRADE_STREAM_BEFORE_ACK")
                        yield parsed
                    if subscribed:
                        try:
                            await asyncio.wait_for(socket.send(json.dumps({
                                "id": self._next_request_id(),
                                "method": "trades_unsubscribe",
                                "params": [],
                            }, separators=(",", ":"))), timeout=1)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except PublicDataError:
                schema_failure = True
            except Exception:
                schema_failure = False
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            if stop.is_set():
                break
            self._emit("SCHEMA_REJECTED" if schema_failure else "CONNECTION_LOST")
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(self.maximum_backoff_seconds, backoff * 2)


__all__ = [
    "TradeStreamControl",
    "WhiteBitPublicTradeWebSocketTransport",
    "parse_trade_stream_message",
]
