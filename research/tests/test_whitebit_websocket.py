"""Focused offline tests for the public-only WhiteBIT WebSocket collector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.integrations.whitebit_public import PublicDataError, WS
from autotrade_research.integrations.whitebit_websocket import (
    MAX_WEBSOCKET_MESSAGE_BYTES,
    WhiteBitBookTicker,
    WhiteBitPublicWebSocketTransport,
    WhiteBitWebSocketControl,
    WhiteBitWebSocketMarketFeed,
    _StdlibWebSocket,
    _default_connector,
    parse_book_ticker_message,
)
from mvp.autotrade_mvp.market_data import RawMarketUpdate


NOW = datetime(2026, 10, 18, 6, 0, 1, tzinfo=timezone.utc)


def encoded(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def subscription(request_id: int = 1) -> bytes:
    return encoded(
        {"id": request_id, "result": {"status": "success"}, "error": None}
    )


def ticker(update_id: int = 7, *, market: str = "BTC_USDT") -> bytes:
    return encoded(
        {
            "method": "bookTicker_update",
            "params": [
                [
                    1792303200.1,
                    1792303200.2,
                    market,
                    update_id,
                    "100.00",
                    "2.5",
                    "101.00",
                    "3.5",
                ]
            ],
            "id": None,
        }
    )


class SchemaTests(unittest.TestCase):
    def test_subscription_and_pong_are_strict_control_messages(self):
        control = parse_book_ticker_message(
            subscription(4), market="BTC_USDT", received_at=NOW
        )
        self.assertEqual(control, WhiteBitWebSocketControl("SUBSCRIBED", 4))
        pong = parse_book_ticker_message(
            encoded({"id": 5, "result": "pong", "error": None}),
            market="BTC_USDT",
            received_at=NOW,
        )
        self.assertEqual(pong, WhiteBitWebSocketControl("PONG", 5))

    def test_valid_book_ticker_preserves_exact_frame_and_decimal_values(self):
        raw = ticker()
        parsed = parse_book_ticker_message(
            raw, market="BTC_USDT", received_at=NOW
        )
        self.assertIs(type(parsed), WhiteBitBookTicker)
        self.assertEqual(parsed.raw, raw)
        self.assertEqual(parsed.update_id, 7)
        self.assertEqual(parsed.transaction_time.microsecond, 100000)
        self.assertEqual(
            (parsed.bid_price, parsed.bid_quantity, parsed.ask_price, parsed.ask_quantity),
            ("100.00", "2.5", "101.00", "3.5"),
        )

    def test_live_seven_digit_timestamp_is_truncated_to_canonical_microseconds(self):
        raw = ticker().replace(b"1792303200.2", b"1792303200.2345678")
        parsed = parse_book_ticker_message(
            raw, market="BTC_USDT", received_at=NOW
        )
        self.assertEqual(parsed.message_time.microsecond, 234567)

    def test_more_than_nanosecond_timestamp_precision_is_rejected(self):
        raw = ticker().replace(b"1792303200.2", b"1792303200.2345678910")
        with self.assertRaisesRegex(PublicDataError, "PRECISION"):
            parse_book_ticker_message(raw, market="BTC_USDT", received_at=NOW)

    def test_hostile_or_wrong_schema_is_rejected(self):
        cases = (
            b'{"id":1,"id":1,"result":"pong","error":null}',
            b" " * (MAX_WEBSOCKET_MESSAGE_BYTES + 1),
            encoded({"id": 1, "result": None, "error": {"message": "secret"}}),
            encoded({"id": None, "method": "ordersExecuted_update", "params": []}),
            ticker(market="ETH_USDT"),
        )
        for raw in cases:
            with self.subTest(raw=raw[:50]), self.assertRaises(PublicDataError):
                parse_book_ticker_message(raw, market="BTC_USDT", received_at=NOW)

    def test_crossed_non_string_or_future_data_is_rejected(self):
        base = json.loads(ticker())
        variants = []
        crossed = json.loads(ticker())
        crossed["params"][0][4] = "102"
        variants.append(crossed)
        binary_float = json.loads(ticker())
        binary_float["params"][0][4] = 100.0
        variants.append(binary_float)
        future = json.loads(ticker())
        future["params"][0][1] = 1792303300
        variants.append(future)
        bad_sequence = json.loads(ticker())
        bad_sequence["params"][0][3] = True
        variants.append(bad_sequence)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(PublicDataError):
                parse_book_ticker_message(
                    encoded(value), market="BTC_USDT", received_at=NOW
                )
        self.assertEqual(base["params"][0][2], "BTC_USDT")


class Disconnect(Exception):
    pass


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.receive_count = 0
        self.closed = False
        self.wait_when_empty = False

    async def send(self, message):
        if self.closed:
            raise Disconnect()
        self.sent.append(json.loads(message))

    async def recv(self):
        self.receive_count += 1
        if self.messages:
            value = self.messages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        if self.wait_when_empty:
            await asyncio.Future()
        raise Disconnect()


class FakeContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_):
        self.socket.closed = True


class Connector:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, endpoint, **options):
        self.calls.append((endpoint, options))
        if not self.sockets:
            raise Disconnect()
        return FakeContext(self.sockets.pop(0))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_connector_needs_no_site_package(self):
        with patch.dict("sys.modules", {"websockets": None}):
            connector = _default_connector(
                WS,
                open_timeout=1,
                close_timeout=1,
                ping_interval=None,
                max_size=1024,
                max_queue=1,
            )
        self.assertIs(type(connector), _StdlibWebSocket)

    async def test_stdlib_handshake_and_unmasked_text_frame(self):
        socket = _StdlibWebSocket(
            WS, open_timeout=1, close_timeout=1, max_size=1024
        )
        key = b"dGhlIHNhbXBsZSBub25jZQ=="
        socket._validate_handshake(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n",
            key,
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x81\x02{}")
        socket._reader = reader
        self.assertEqual(await socket.recv(), "{}")

    async def test_stdlib_rejects_masked_server_frame(self):
        socket = _StdlibWebSocket(
            WS, open_timeout=1, close_timeout=1, max_size=1024
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x81\x82\x00\x00\x00\x00{}")
        socket._reader = reader
        with self.assertRaisesRegex(PublicDataError, "FRAME"):
            await socket.recv()

    async def test_fixed_endpoint_subscription_and_clean_unsubscribe(self):
        socket = FakeSocket([subscription(), ticker()])
        socket.wait_when_empty = True
        connector = Connector([socket])
        diagnostics = []
        transport = WhiteBitPublicWebSocketTransport(
            market="BTC_USDT",
            connector=connector,
            diagnostic=diagnostics.append,
            heartbeat_seconds=0.02,
            initial_backoff_seconds=0.01,
            maximum_backoff_seconds=0.02,
        )
        stop = asyncio.Event()
        observed = []
        async for item in transport.observations(stop):
            observed.append(item)
            stop.set()
        self.assertEqual(len(observed), 1)
        self.assertEqual(connector.calls[0][0], WS)
        self.assertEqual(connector.calls[0][1]["max_queue"], 32)
        self.assertIsNone(connector.calls[0][1]["ping_interval"])
        self.assertEqual(socket.sent[0]["method"], "bookTicker_subscribe")
        self.assertEqual(socket.sent[0]["params"], ["BTC_USDT"])
        self.assertIn("bookTicker_unsubscribe", [item["method"] for item in socket.sent])
        self.assertEqual(diagnostics[-1].code, "STOPPED")
        self.assertTrue(all(not hasattr(item, "message") for item in diagnostics))

    async def test_reconnects_with_backoff_and_new_generation(self):
        first = FakeSocket([subscription(), Disconnect("proxy contains a secret")])
        second = FakeSocket([subscription(2), ticker(8)])
        second.wait_when_empty = True
        connector = Connector([first, second])
        diagnostics = []
        transport = WhiteBitPublicWebSocketTransport(
            market="BTC_USDT",
            connector=connector,
            diagnostic=diagnostics.append,
            heartbeat_seconds=0.05,
            initial_backoff_seconds=0.01,
            maximum_backoff_seconds=0.02,
        )
        stop = asyncio.Event()
        observed = []
        async for item in transport.observations(stop):
            observed.append(item)
            stop.set()
        self.assertEqual(observed[0].generation, 2)
        self.assertEqual(len(connector.calls), 2)
        self.assertIn("CONNECTION_LOST", [item.code for item in diagnostics])
        self.assertIn("RECONNECT_WAIT", [item.code for item in diagnostics])
        self.assertNotIn("proxy contains a secret", repr(diagnostics))

    async def test_application_heartbeat_runs_while_receive_waits(self):
        socket = FakeSocket([subscription()])
        socket.wait_when_empty = True
        connector = Connector([socket])
        transport = WhiteBitPublicWebSocketTransport(
            market="BTC_USDT",
            connector=connector,
            heartbeat_seconds=0.01,
            acknowledgement_timeout_seconds=0.1,
            initial_backoff_seconds=0.01,
            maximum_backoff_seconds=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(_drain(transport, stop))
        await asyncio.sleep(0.035)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertIn("ping", [item["method"] for item in socket.sent])

    async def test_schema_failure_reconnects_without_leaking_provider_error(self):
        bad = FakeSocket(
            [subscription(), encoded({"id": 99, "result": None, "error": {"message": "API key abc"}})]
        )
        good = FakeSocket([subscription(2), ticker()])
        good.wait_when_empty = True
        diagnostics = []
        transport = WhiteBitPublicWebSocketTransport(
            market="BTC_USDT",
            connector=Connector([bad, good]),
            diagnostic=diagnostics.append,
            heartbeat_seconds=0.05,
            initial_backoff_seconds=0.01,
            maximum_backoff_seconds=0.01,
        )
        stop = asyncio.Event()
        async for _ in transport.observations(stop):
            stop.set()
        self.assertIn("SCHEMA_REJECTED", [item.code for item in diagnostics])
        self.assertNotIn("API key abc", repr(diagnostics))


async def _drain(transport, stop):
    async for _ in transport.observations(stop):
        pass


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ArtifactStore(Path(self.directory.name) / "artifacts")

    async def test_collector_commits_raw_frame_before_canonical_delivery(self):
        socket = FakeSocket([subscription(), ticker(41)])
        socket.wait_when_empty = True
        transport = WhiteBitPublicWebSocketTransport(
            market="BTC_USDT",
            connector=Connector([socket]),
            heartbeat_seconds=0.05,
            initial_backoff_seconds=0.01,
            maximum_backoff_seconds=0.01,
        )
        feed = WhiteBitWebSocketMarketFeed(
            transport=transport, artifacts=self.store, queue_size=1
        )
        stop = asyncio.Event()
        delivered = []

        def consume(update):
            delivered.append(update)
            stop.set()

        await asyncio.wait_for(feed.run(consume, stop), timeout=1)
        self.assertEqual(len(delivered), 1)
        update = delivered[0]
        self.assertIs(type(update), RawMarketUpdate)
        self.assertEqual(update.kind, "QUOTE")
        self.assertEqual(update.provider_id, "WHITEBIT")
        self.assertEqual(update.source_sequence, 41)
        self.assertEqual(update.stream_generation, 1)
        self.assertEqual(update.sequence_stream, "bookTicker")
        self.assertEqual(update.payload["bid_price"], "100.00")
        self.assertEqual(
            self.store.read_bytes(update.raw_evidence_ref["artifact_id"]), ticker(41)
        )

    async def test_consumer_failure_is_not_retried(self):
        socket = FakeSocket([subscription(), ticker()])
        socket.wait_when_empty = True
        transport = WhiteBitPublicWebSocketTransport(
            market="BTC_USDT",
            connector=Connector([socket]),
            heartbeat_seconds=0.05,
            initial_backoff_seconds=0.01,
            maximum_backoff_seconds=0.01,
        )
        feed = WhiteBitWebSocketMarketFeed(
            transport=transport, artifacts=self.store, queue_size=1
        )
        calls = 0

        def broken(_):
            nonlocal calls
            calls += 1
            raise RuntimeError("durable sink unavailable")

        with self.assertRaisesRegex(RuntimeError, "durable sink unavailable"):
            await asyncio.wait_for(feed.run(broken, asyncio.Event()), timeout=1)
        self.assertEqual(calls, 1)

    async def test_constructor_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            WhiteBitPublicWebSocketTransport(market="BTC_USDT", queue_size=0)
        with self.assertRaises(ValueError):
            WhiteBitWebSocketMarketFeed(
                transport=WhiteBitPublicWebSocketTransport(market="BTC_USDT"),
                artifacts=self.store,
                queue_size=0,
            )
        with self.assertRaises(TypeError):
            WhiteBitPublicWebSocketTransport(market="BTC_USDT", api_key="secret")


if __name__ == "__main__":
    unittest.main()
