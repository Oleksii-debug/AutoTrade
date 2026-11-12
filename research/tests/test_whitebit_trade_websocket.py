import asyncio
from datetime import datetime, timezone
import json
import unittest

from autotrade_research.integrations.whitebit_public import PublicDataError
from autotrade_research.integrations.whitebit_trade_websocket import (
    TradeStreamControl,
    WhiteBitPublicTradeWebSocketTransport,
    parse_trade_stream_message,
)


NOW = datetime(2026, 11, 12, 5, 0, 10, tzinfo=timezone.utc)
MARKETS = ("BTC_USDT", "ETH_USDT")


def encoded(value):
    return json.dumps(value, separators=(",", ":"))


def update(market="BTC_USDT", identity=12, seconds=None):
    return encoded({
        "method": "trades_update",
        "params": [market, [{
            "id": identity,
            "time": (NOW.timestamp() - 1 if seconds is None else seconds),
            "price": "100.25",
            "amount": "0.2",
            "type": "buy",
            "rpi": False,
        }]],
    })


class ParserTests(unittest.TestCase):
    def test_ack_and_trade_are_canonical(self):
        control = parse_trade_stream_message(
            encoded({"error": None, "result": {"status": "success"}, "id": 7}),
            markets=MARKETS, received_at=NOW,
        )
        self.assertEqual(control, TradeStreamControl("SUBSCRIBED", 7))
        batch = parse_trade_stream_message(
            update(), markets=MARKETS, received_at=NOW,
        )
        self.assertEqual(batch.market, "BTC_USDT")
        self.assertEqual(batch.trades[0].base_quantity, "0.2")
        self.assertEqual(batch.trades[0].quote_quantity, "20.050")
        self.assertEqual(batch.trades[0].side, "buy")
        self.assertTrue(batch.source_uri.endswith("#trades_update"))

    def test_snapshot_is_normalized_newest_first_and_missing_rpi_is_false(self):
        document = json.loads(update())
        older = dict(document["params"][1][0], id=11, time=NOW.timestamp() - 2)
        older.pop("rpi")
        document["params"][1].insert(0, older)
        batch = parse_trade_stream_message(
            encoded(document), markets=MARKETS, received_at=NOW,
        )
        self.assertEqual([trade.trade_id for trade in batch.trades], [12, 11])
        self.assertFalse(batch.trades[1].rpi)

    def test_rejects_unsubscribed_future_duplicate_and_unknown_fields(self):
        duplicate = json.loads(update())
        duplicate["params"][1].append(dict(duplicate["params"][1][0]))
        unknown = json.loads(update())
        unknown["params"][1][0]["secret"] = "not allowed"
        variants = [
            update("SOL_USDT"),
            update(seconds=NOW.timestamp() + 1),
            encoded(duplicate),
            encoded(unknown),
        ]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(PublicDataError):
                parse_trade_stream_message(value, markets=MARKETS, received_at=NOW)


class Disconnect(Exception):
    pass


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.messages:
            value = self.messages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        await asyncio.Future()


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
    async def test_one_subscription_carries_multiple_markets_and_unsubscribes(self):
        ack = encoded({"error": None, "result": {"status": "success"}, "id": 1})
        socket = FakeSocket([ack, update()])
        connector = Connector([socket])
        events = []
        transport = WhiteBitPublicTradeWebSocketTransport(
            markets=MARKETS, connector=connector, diagnostic=events.append,
            heartbeat_seconds=0.05, initial_backoff_seconds=0.01,
            maximum_backoff_seconds=0.01,
        )
        stop = asyncio.Event()
        observed = []
        async for batch in transport.batches(stop):
            observed.append(batch)
            stop.set()
        self.assertEqual(len(observed), 1)
        self.assertEqual(socket.sent[0]["method"], "trades_subscribe")
        self.assertEqual(socket.sent[0]["params"], list(MARKETS))
        self.assertIn("trades_unsubscribe", [item["method"] for item in socket.sent])
        self.assertEqual(events, ["SUBSCRIBED"])

    async def test_schema_failure_reconnects_without_error_text(self):
        ack1 = encoded({"error": None, "result": {"status": "success"}, "id": 1})
        ack2 = encoded({"error": None, "result": {"status": "success"}, "id": 2})
        bad = FakeSocket([ack1, encoded({"error": {"message": "api secret"}, "result": None, "id": 9})])
        good = FakeSocket([ack2, update(identity=13)])
        events = []
        transport = WhiteBitPublicTradeWebSocketTransport(
            markets=MARKETS, connector=Connector([bad, good]),
            diagnostic=events.append, heartbeat_seconds=0.05,
            initial_backoff_seconds=0.01, maximum_backoff_seconds=0.01,
        )
        stop = asyncio.Event()
        async for _ in transport.batches(stop):
            stop.set()
        self.assertIn("SCHEMA_REJECTED", events)
        self.assertNotIn("api secret", repr(events))


if __name__ == "__main__":
    unittest.main()
