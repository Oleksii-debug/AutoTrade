"""Credential-free WhiteBIT public WebSocket market-data transport.

The transport owns one public ``bookTicker`` subscription and never accepts
credentials or exposes private/trading methods.  The collector persists each
exact provider frame through the existing ArtifactStore before publishing the
existing RawMarketUpdate contract.  No BUY, SELL, transfer, or account route is
reachable from this module.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha1, sha256
import inspect
import json
import secrets
import ssl
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..artifacts.store import ArtifactIntegrityError, ArtifactStore
from .whitebit_public import PublicDataError, WS, _decimal, _symbol, decode_public_json


ADAPTER_VERSION = "whitebit-public-websocket-book-ticker-v1"
MAX_WEBSOCKET_MESSAGE_BYTES = 65_536
SOURCE_URI = WS + "#bookTicker"
_CONTROL_CODES = frozenset({"SUBSCRIBED", "PONG"})
_DIAGNOSTIC_CODES = frozenset(
    {
        "CONNECTING",
        "SUBSCRIBED",
        "CONNECTION_LOST",
        "SCHEMA_REJECTED",
        "RECONNECT_WAIT",
        "STOPPED",
    }
)


@dataclass(frozen=True)
class WhiteBitWebSocketDiagnostic:
    """Bounded, secret-free operational state for logs and status surfaces."""

    code: str
    attempt: int
    generation: int
    retry_in_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTIC_CODES:
            raise ValueError("unsupported WhiteBIT WebSocket diagnostic code")
        for name in ("attempt", "generation"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be an exact non-negative integer")
        if self.retry_in_seconds is not None:
            value = self.retry_in_seconds
            if type(value) not in (int, float) or not 0 <= value <= 300:
                raise ValueError("retry_in_seconds is outside the bounded range")


@dataclass(frozen=True)
class WhiteBitWebSocketControl:
    code: str
    request_id: int

    def __post_init__(self) -> None:
        if self.code not in _CONTROL_CODES:
            raise ValueError("unsupported WhiteBIT WebSocket control code")
        if type(self.request_id) is not int or self.request_id < 0:
            raise ValueError("request_id must be an exact non-negative integer")


@dataclass(frozen=True)
class WhiteBitBookTicker:
    """One validated, self-contained public best-bid/offer observation."""

    market: str
    transaction_time: datetime
    message_time: datetime
    received_at: datetime
    update_id: int
    bid_price: str
    bid_quantity: str
    ask_price: str
    ask_quantity: str
    raw: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.raw).hexdigest()

    def to_raw_market_update(
        self,
        evidence_ref: dict[str, Any],
        *,
        now: datetime,
        stream_generation: int,
    ):
        """Bridge a committed frame to the canonical market update contract."""
        if type(now) is not datetime or type(now.tzinfo) is not timezone:
            raise PublicDataError("UTC_TIMESTAMP_REQUIRED")
        if type(stream_generation) is not int or stream_generation < 1:
            raise PublicDataError("INVALID_STREAM_GENERATION")
        expected_observed = self.received_at.isoformat().replace("+00:00", "Z")
        if (
            type(evidence_ref) is not dict
            or evidence_ref.get("sha256") != self.digest
            or evidence_ref.get("source_uri") != SOURCE_URI
            or evidence_ref.get("observed_at") != expected_observed
        ):
            raise PublicDataError("RAW_EVIDENCE_BINDING_MISMATCH")
        if now < self.received_at:
            raise PublicDataError("CLOCK_MOVED_BACKWARD")

        from mvp.autotrade_mvp.market_data import RawMarketUpdate

        return RawMarketUpdate(
            provider_id="WHITEBIT",
            venue_id="WHITEBIT_SPOT",
            provider_symbol=self.market,
            adapter_version=ADAPTER_VERSION,
            kind="QUOTE",
            source_event_at=self.transaction_time,
            available_at=self.received_at,
            ingested_at=now.astimezone(timezone.utc),
            availability_basis="OBSERVED_PUBLIC_WEBSOCKET_FRAME",
            revision=0,
            payload={
                "bid_price": self.bid_price,
                "bid_quantity": self.bid_quantity,
                "ask_price": self.ask_price,
                "ask_quantity": self.ask_quantity,
            },
            raw_evidence_ref=evidence_ref,
            source_sequence=self.update_id,
            stream_generation=stream_generation,
            sequence_stream="bookTicker",
        )


def _raw_bytes(raw: str | bytes) -> bytes:
    if type(raw) is bytes:
        value = raw
    elif type(raw) is str:
        try:
            value = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise PublicDataError("INVALID_WEBSOCKET_UTF8") from None
    else:
        raise PublicDataError("INVALID_WEBSOCKET_MESSAGE_TYPE")
    if not 0 < len(value) <= MAX_WEBSOCKET_MESSAGE_BYTES:
        raise PublicDataError("WEBSOCKET_MESSAGE_SIZE_LIMIT")
    return value


def _epoch_time(value: object, *, received_at: datetime) -> datetime:
    if type(value) not in (int, Decimal):
        raise PublicDataError("INVALID_WEBSOCKET_TIMESTAMP")
    exact = Decimal(value)
    if not exact.is_finite() or exact < Decimal("1000000000"):
        raise PublicDataError("INVALID_WEBSOCKET_TIMESTAMP")
    # WhiteBIT currently emits seven fractional digits while Python datetime
    # carries microseconds.  Preserve the exact raw frame as evidence and admit
    # at most nanosecond source precision before deterministic truncation to the
    # canonical datetime resolution.
    if exact.as_tuple().exponent < -9:
        raise PublicDataError("WEBSOCKET_TIMESTAMP_PRECISION")
    microseconds = exact * 1_000_000
    try:
        parsed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=int(microseconds)
        )
    except (OverflowError, ValueError):
        raise PublicDataError("INVALID_WEBSOCKET_TIMESTAMP") from None
    if parsed > received_at:
        raise PublicDataError("FUTURE_WEBSOCKET_TIMESTAMP")
    return parsed


def parse_book_ticker_message(
    raw: str | bytes,
    *,
    market: str,
    received_at: datetime,
) -> WhiteBitBookTicker | WhiteBitWebSocketControl:
    """Validate one JSON-RPC control or ``bookTicker_update`` message."""
    expected_market = _symbol(market)
    if type(received_at) is not datetime or type(received_at.tzinfo) is not timezone:
        raise PublicDataError("UTC_TIMESTAMP_REQUIRED")
    raw_value = _raw_bytes(raw)
    message = decode_public_json(raw_value)
    if type(message) is not dict:
        raise PublicDataError("INVALID_WEBSOCKET_SCHEMA")

    if set(message) == {"id", "result", "error"}:
        request_id = message["id"]
        if type(request_id) is not int or request_id < 0:
            raise PublicDataError("INVALID_WEBSOCKET_CONTROL_ID")
        if message["error"] is not None:
            raise PublicDataError("WEBSOCKET_PROVIDER_ERROR")
        if message["result"] == "pong":
            return WhiteBitWebSocketControl("PONG", request_id)
        result = message["result"]
        if type(result) is dict and result == {"status": "success"}:
            return WhiteBitWebSocketControl("SUBSCRIBED", request_id)
        raise PublicDataError("INVALID_WEBSOCKET_CONTROL_RESULT")

    if set(message) != {"id", "method", "params"}:
        raise PublicDataError("INVALID_WEBSOCKET_SCHEMA")
    if message["id"] is not None or message["method"] != "bookTicker_update":
        raise PublicDataError("UNEXPECTED_WEBSOCKET_METHOD")
    params = message["params"]
    if type(params) is not list or len(params) != 1:
        raise PublicDataError("INVALID_BOOK_TICKER_PARAMS")
    row = params[0]
    if type(row) is not list or len(row) != 8:
        raise PublicDataError("INVALID_BOOK_TICKER_ROW")

    transaction_time = _epoch_time(row[0], received_at=received_at)
    message_time = _epoch_time(row[1], received_at=received_at)
    if transaction_time > message_time:
        raise PublicDataError("INVALID_BOOK_TICKER_TIME_ORDER")
    if row[2] != expected_market:
        raise PublicDataError("WEBSOCKET_MARKET_MISMATCH")
    update_id = row[3]
    if type(update_id) is not int or update_id < 0:
        raise PublicDataError("INVALID_BOOK_TICKER_UPDATE_ID")
    bid_price = _decimal(row[4], positive=True)
    bid_quantity = _decimal(row[5], positive=True)
    ask_price = _decimal(row[6], positive=True)
    ask_quantity = _decimal(row[7], positive=True)
    if bid_price > ask_price:
        raise PublicDataError("CROSSED_BOOK_TICKER")

    return WhiteBitBookTicker(
        market=expected_market,
        transaction_time=transaction_time,
        message_time=message_time,
        received_at=received_at,
        update_id=update_id,
        bid_price=format(bid_price, "f"),
        bid_quantity=format(bid_quantity, "f"),
        ask_price=format(ask_price, "f"),
        ask_quantity=format(ask_quantity, "f"),
        raw=raw_value,
    )


@dataclass(frozen=True)
class WhiteBitStreamObservation:
    ticker: WhiteBitBookTicker
    generation: int


class _StdlibWebSocket:
    """Small RFC 6455 text client for the fixed public WhiteBIT endpoint.

    This deliberately implements only the protocol surface the public JSON-RPC
    collector needs.  Keeping it on Python's TLS/asyncio standard library makes
    the source-staged Windows embedded runtime actually runnable without an
    undeclared site-package dependency.
    """

    _GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    _HANDSHAKE_LIMIT = 16_384

    def __init__(
        self,
        endpoint: str,
        *,
        open_timeout: float,
        close_timeout: float,
        max_size: int,
    ) -> None:
        if endpoint != WS:
            raise PublicDataError("UNQUALIFIED_WEBSOCKET_ENDPOINT")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "wss"
            or parsed.hostname is None
            or parsed.port is not None
            or parsed.path != "/ws"
            or parsed.query
            or parsed.fragment
        ):
            raise PublicDataError("UNQUALIFIED_WEBSOCKET_ENDPOINT")
        self._host = parsed.hostname
        self._path = parsed.path
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._max_size = max_size
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self):
        context = ssl.create_default_context()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._host,
                    443,
                    ssl=context,
                    server_hostname=self._host,
                    limit=self._HANDSHAKE_LIMIT,
                ),
                timeout=self._open_timeout,
            )
            key = base64.b64encode(secrets.token_bytes(16))
            request = b"\r\n".join(
                (
                    b"GET " + self._path.encode("ascii") + b" HTTP/1.1",
                    b"Host: " + self._host.encode("ascii"),
                    b"Upgrade: websocket",
                    b"Connection: Upgrade",
                    b"Sec-WebSocket-Key: " + key,
                    b"Sec-WebSocket-Version: 13",
                    b"User-Agent: AutoTrade-PublicReader/1",
                    b"",
                    b"",
                )
            )
            self._writer.write(request)
            await self._writer.drain()
            response = await asyncio.wait_for(
                self._reader.readuntil(b"\r\n\r\n"),
                timeout=self._open_timeout,
            )
            self._validate_handshake(response, key)
            return self
        except BaseException:
            await self._close_stream()
            raise

    def _validate_handshake(self, response: bytes, key: bytes) -> None:
        if len(response) > self._HANDSHAKE_LIMIT:
            raise PublicDataError("WEBSOCKET_HANDSHAKE_SIZE_LIMIT")
        try:
            lines = response[:-4].decode("ascii", errors="strict").split("\r\n")
        except UnicodeDecodeError:
            raise PublicDataError("INVALID_WEBSOCKET_HANDSHAKE") from None
        if not lines or lines[0] != "HTTP/1.1 101 Switching Protocols":
            raise PublicDataError("WEBSOCKET_UPGRADE_REJECTED")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                raise PublicDataError("INVALID_WEBSOCKET_HANDSHAKE")
            name, value = line.split(":", 1)
            name = name.strip().lower()
            value = value.strip()
            if not name or name in headers:
                raise PublicDataError("INVALID_WEBSOCKET_HANDSHAKE")
            headers[name] = value
        connection_tokens = {
            token.strip().lower()
            for token in headers.get("connection", "").split(",")
        }
        expected_accept = base64.b64encode(sha1(key + self._GUID).digest()).decode(
            "ascii"
        )
        if (
            headers.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in connection_tokens
            or not secrets.compare_digest(
                headers.get("sec-websocket-accept", ""), expected_accept
            )
        ):
            raise PublicDataError("INVALID_WEBSOCKET_HANDSHAKE")

    async def _frame(self, opcode: int, payload: bytes) -> None:
        if self._writer is None or self._closed:
            raise ConnectionError("WebSocket is closed")
        if len(payload) > self._max_size:
            raise PublicDataError("WEBSOCKET_MESSAGE_SIZE_LIMIT")
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length <= 125:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 65_535:
            header = bytes((0x80 | opcode, 0xFE)) + length.to_bytes(2, "big")
        else:
            header = bytes((0x80 | opcode, 0xFF)) + length.to_bytes(8, "big")
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        async with self._send_lock:
            self._writer.write(header + mask + masked)
            await self._writer.drain()

    async def send(self, message: str) -> None:
        if type(message) is not str:
            raise TypeError("WebSocket message must be an exact string")
        try:
            payload = message.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise PublicDataError("INVALID_WEBSOCKET_UTF8") from None
        await self._frame(0x1, payload)

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        if self._reader is None:
            raise ConnectionError("WebSocket is not connected")
        header = await self._reader.readexactly(2)
        first, second = header
        if first & 0x70 or second & 0x80:
            raise PublicDataError("INVALID_WEBSOCKET_FRAME")
        final = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(await self._reader.readexactly(2), "big")
            if length < 126:
                raise PublicDataError("INVALID_WEBSOCKET_FRAME")
        elif length == 127:
            encoded = await self._reader.readexactly(8)
            if encoded[0] & 0x80:
                raise PublicDataError("INVALID_WEBSOCKET_FRAME")
            length = int.from_bytes(encoded, "big")
            if length <= 65_535:
                raise PublicDataError("INVALID_WEBSOCKET_FRAME")
        if opcode >= 0x8 and (not final or length > 125):
            raise PublicDataError("INVALID_WEBSOCKET_CONTROL_FRAME")
        if length > self._max_size:
            raise PublicDataError("WEBSOCKET_MESSAGE_SIZE_LIMIT")
        return final, opcode, await self._reader.readexactly(length)

    async def recv(self) -> str:
        fragments = bytearray()
        fragmented = False
        while True:
            final, opcode, payload = await self._read_frame()
            if opcode == 0x8:
                if len(payload) == 1:
                    raise PublicDataError("INVALID_WEBSOCKET_CLOSE_FRAME")
                if not self._closed:
                    await self._frame(0x8, payload[:125])
                self._closed = True
                raise ConnectionError("WebSocket closed by peer")
            if opcode == 0x9:
                await self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x2:
                raise PublicDataError("BINARY_WEBSOCKET_MESSAGE_FORBIDDEN")
            if opcode == 0x1:
                if fragmented:
                    raise PublicDataError("INVALID_WEBSOCKET_FRAGMENT")
                fragments.extend(payload)
                fragmented = not final
            elif opcode == 0x0:
                if not fragmented:
                    raise PublicDataError("INVALID_WEBSOCKET_FRAGMENT")
                fragments.extend(payload)
                fragmented = not final
            else:
                raise PublicDataError("INVALID_WEBSOCKET_OPCODE")
            if len(fragments) > self._max_size:
                raise PublicDataError("WEBSOCKET_MESSAGE_SIZE_LIMIT")
            if not fragmented:
                try:
                    return bytes(fragments).decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise PublicDataError("INVALID_WEBSOCKET_UTF8") from None

    async def _close_stream(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=self._close_timeout
                )
            except Exception:
                pass

    async def __aexit__(self, *_):
        if self._writer is not None and not self._closed:
            try:
                await asyncio.wait_for(self._frame(0x8, b""), timeout=1)
            except Exception:
                pass
        self._closed = True
        await self._close_stream()


def _default_connector(endpoint: str, **options):
    # Keep the connector dependency-free for the isolated embedded Python.
    allowed = {
        "open_timeout",
        "close_timeout",
        "ping_interval",
        "max_size",
        "max_queue",
    }
    if set(options) != allowed or options["ping_interval"] is not None:
        raise PublicDataError("INVALID_WEBSOCKET_CONNECT_OPTIONS")
    # max_queue is enforced by the collector and remains part of the injectable
    # connector contract; this stream implementation performs one bounded frame
    # read at a time and therefore has no hidden receive queue.
    return _StdlibWebSocket(
        endpoint,
        open_timeout=options["open_timeout"],
        close_timeout=options["close_timeout"],
        max_size=options["max_size"],
    )


class WhiteBitPublicWebSocketTransport:
    """Reconnectable public-only WhiteBIT ``bookTicker`` transport."""

    def __init__(
        self,
        *,
        market: str,
        connector: Callable[..., Any] | None = None,
        queue_size: int = 32,
        heartbeat_seconds: float = 45.0,
        acknowledgement_timeout_seconds: float = 10.0,
        initial_backoff_seconds: float = 1.0,
        maximum_backoff_seconds: float = 30.0,
        diagnostic: Callable[[WhiteBitWebSocketDiagnostic], None] | None = None,
    ) -> None:
        self.market = _symbol(market)
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("queue_size must be an exact integer from 1 to 1024")
        self.queue_size = queue_size
        for name, value, low, high in (
            ("heartbeat_seconds", heartbeat_seconds, 0.01, 50.0),
            (
                "acknowledgement_timeout_seconds",
                acknowledgement_timeout_seconds,
                0.01,
                60.0,
            ),
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
        self.acknowledgement_timeout_seconds = float(
            acknowledgement_timeout_seconds
        )
        self.initial_backoff_seconds = float(initial_backoff_seconds)
        self.maximum_backoff_seconds = float(maximum_backoff_seconds)
        self._diagnostic = diagnostic
        self._request_id = 0

    def _emit(
        self,
        code: str,
        *,
        attempt: int,
        generation: int,
        retry: float | None = None,
    ) -> None:
        if self._diagnostic is not None:
            self._diagnostic(
                WhiteBitWebSocketDiagnostic(code, attempt, generation, retry)
            )

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
            max_queue=self.queue_size,
        )
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _heartbeat(self, socket, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
                return
            except TimeoutError:
                pass
            request_id = self._next_request_id()
            await socket.send(
                json.dumps(
                    {"id": request_id, "method": "ping", "params": []},
                    separators=(",", ":"),
                )
            )

    @staticmethod
    async def _receive(socket, stop: asyncio.Event, heartbeat: asyncio.Task):
        receive = asyncio.create_task(socket.recv())
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {receive, stopped, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopped in done:
                return None
            if heartbeat in done:
                exception = heartbeat.exception()
                if exception is not None:
                    raise exception
                return None
            return receive.result()
        finally:
            for task in (receive, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive, stopped, return_exceptions=True)

    async def observations(
        self, stop: asyncio.Event
    ) -> AsyncIterator[WhiteBitStreamObservation]:
        if type(stop) is not asyncio.Event:
            raise TypeError("stop must be an exact asyncio.Event")
        attempt = 0
        generation = 0
        backoff = self.initial_backoff_seconds
        while not stop.is_set():
            attempt += 1
            self._emit("CONNECTING", attempt=attempt, generation=generation)
            schema_failure = False
            subscribed = False
            socket = None
            heartbeat: asyncio.Task | None = None
            try:
                context = await self._open()
                async with context as socket:
                    generation += 1
                    subscribe_id = self._next_request_id()
                    await socket.send(
                        json.dumps(
                            {
                                "id": subscribe_id,
                                "method": "bookTicker_subscribe",
                                "params": [self.market],
                            },
                            separators=(",", ":"),
                        )
                    )
                    heartbeat = asyncio.create_task(self._heartbeat(socket, stop))
                    acknowledgement_deadline = (
                        asyncio.get_running_loop().time()
                        + self.acknowledgement_timeout_seconds
                    )
                    while not stop.is_set():
                        remaining = (
                            acknowledgement_deadline
                            - asyncio.get_running_loop().time()
                        )
                        if not subscribed and remaining <= 0:
                            raise PublicDataError("WEBSOCKET_SUBSCRIBE_TIMEOUT")
                        try:
                            message = await asyncio.wait_for(
                                self._receive(socket, stop, heartbeat),
                                timeout=None if subscribed else remaining,
                            )
                        except TimeoutError:
                            raise PublicDataError(
                                "WEBSOCKET_SUBSCRIBE_TIMEOUT"
                            ) from None
                        if message is None:
                            break
                        parsed = parse_book_ticker_message(
                            message,
                            market=self.market,
                            received_at=datetime.now(timezone.utc),
                        )
                        if type(parsed) is WhiteBitWebSocketControl:
                            if parsed.code == "SUBSCRIBED":
                                if parsed.request_id != subscribe_id or subscribed:
                                    raise PublicDataError(
                                        "UNEXPECTED_WEBSOCKET_SUBSCRIPTION_ACK"
                                    )
                                subscribed = True
                                self._emit(
                                    "SUBSCRIBED",
                                    attempt=attempt,
                                    generation=generation,
                                )
                            continue
                        if not subscribed:
                            raise PublicDataError(
                                "BOOK_TICKER_BEFORE_SUBSCRIPTION_ACK"
                            )
                        backoff = self.initial_backoff_seconds
                        yield WhiteBitStreamObservation(parsed, generation)
                    if subscribed and stop.is_set():
                        try:
                            unsubscribe_id = self._next_request_id()
                            await asyncio.wait_for(
                                socket.send(
                                    json.dumps(
                                        {
                                            "id": unsubscribe_id,
                                            "method": "bookTicker_unsubscribe",
                                            "params": [],
                                        },
                                        separators=(",", ":"),
                                    )
                                ),
                                timeout=1,
                            )
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except PublicDataError:
                schema_failure = True
            except Exception:
                # Provider/library exception text can contain proxy URLs or other
                # local details.  Only the bounded code leaves this boundary.
                schema_failure = False
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

            if stop.is_set():
                break
            self._emit(
                "SCHEMA_REJECTED" if schema_failure else "CONNECTION_LOST",
                attempt=attempt,
                generation=generation,
            )
            self._emit(
                "RECONNECT_WAIT",
                attempt=attempt,
                generation=generation,
                retry=backoff,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(self.maximum_backoff_seconds, backoff * 2)
        self._emit("STOPPED", attempt=attempt, generation=generation)


class WhiteBitWebSocketMarketFeed:
    """Central bounded collector that persists once and publishes sequentially."""

    def __init__(
        self,
        *,
        transport: WhiteBitPublicWebSocketTransport,
        artifacts: ArtifactStore,
        queue_size: int = 32,
    ) -> None:
        if type(transport) is not WhiteBitPublicWebSocketTransport:
            raise TypeError("transport must be WhiteBitPublicWebSocketTransport")
        if type(artifacts) is not ArtifactStore:
            raise TypeError("artifacts must be canonical ArtifactStore")
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("queue_size must be an exact integer from 1 to 1024")
        self.transport = transport
        self.artifacts = artifacts
        self.queue_size = queue_size

    def _commit(self, observation: WhiteBitStreamObservation):
        ticker = observation.ticker
        artifact_id = str(uuid4())
        manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=ticker.raw,
            media_type="application/json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "whitebit-public-websocket-internal-v1",
            },
            source_refs=[SOURCE_URI],
            metadata={
                "provider": "WHITEBIT",
                "market": ticker.market,
                "source_time": ticker.transaction_time.isoformat(),
                "message_time": ticker.message_time.isoformat(),
                "observed_at": ticker.received_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "update_id": str(ticker.update_id),
                "stream_generation": str(observation.generation),
            },
        )
        if (
            manifest["sha256"] != ticker.digest
            or ArtifactStore.read_bytes(self.artifacts, artifact_id) != ticker.raw
        ):
            raise ArtifactIntegrityError("WhiteBIT WebSocket evidence mismatch")
        evidence = {
            "artifact_id": artifact_id,
            "sha256": manifest["sha256"],
            "source_uri": SOURCE_URI,
            "observed_at": ticker.received_at.isoformat().replace("+00:00", "Z"),
            "rights_id": "whitebit-public-websocket-internal-v1",
        }
        return ticker.to_raw_market_update(
            evidence,
            now=datetime.now(timezone.utc),
            stream_generation=observation.generation,
        )

    async def run(
        self,
        consumer: Callable[[Any], object | Awaitable[object]],
        stop: asyncio.Event,
    ) -> None:
        """Deliver in order with backpressure; never drop or retry a consumer."""
        if not callable(consumer) or type(stop) is not asyncio.Event:
            raise TypeError("consumer and an exact asyncio.Event are required")
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        sentinel = object()

        async def produce() -> None:
            async for observation in self.transport.observations(stop):
                await queue.put(self._commit(observation))
            await queue.put(sentinel)

        async def consume() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is sentinel:
                        return
                    outcome = consumer(item)
                    if inspect.isawaitable(outcome):
                        await outcome
                finally:
                    queue.task_done()

        tasks = {asyncio.create_task(produce()), asyncio.create_task(consume())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        error = next(
            (task.exception() for task in done if task.exception() is not None),
            None,
        )
        if error is not None:
            stop.set()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise error
        await asyncio.gather(*pending)


__all__ = [
    "ADAPTER_VERSION",
    "MAX_WEBSOCKET_MESSAGE_BYTES",
    "WhiteBitBookTicker",
    "WhiteBitPublicWebSocketTransport",
    "WhiteBitStreamObservation",
    "WhiteBitWebSocketControl",
    "WhiteBitWebSocketDiagnostic",
    "WhiteBitWebSocketMarketFeed",
    "parse_book_ticker_message",
]
