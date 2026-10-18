"""Offline contract and safety tests; never claim live WhiteBIT qualification."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import localcontext
from email.message import Message
import io
import json
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import patch
from urllib.error import URLError

from autotrade_research.integrations.whitebit_public import (
    BASE, MAX_BYTES, PublicDataError, WhiteBitPublicClient,
    decode_public_json, parse_orderbook, parse_spot_markets, _NoRedirect,
)

NOW = datetime(2026, 10, 17, 20, 0, 0, tzinfo=timezone.utc)
BOOK = {"ticker_id": "BTC_USDT", "timestamp": int(NOW.timestamp()),
        "bids": [["100", "2"], ["99", "3"]], "asks": [["101", "4"], ["102", "5"]]}
MARKET = {"name": "BTC_USDT", "stock": "BTC", "money": "USDT", "stockPrec": "8",
          "moneyPrec": "2", "minAmount": "0.00001", "minTotal": "5",
          "type": "spot", "tradesEnabled": True, "delistedAt": None}

def raw(value):
    return json.dumps(value, separators=(",", ":")).encode()


class JsonTests(unittest.TestCase):
    def test_duplicate_keys(self):
        with self.assertRaisesRegex(PublicDataError, "DUPLICATE"):
            decode_public_json(b'{"a":1,"a":2}')
    def test_depth(self):
        with self.assertRaisesRegex(PublicDataError, "DEPTH"):
            decode_public_json(b'[' * 17 + b'0' + b']' * 17)
    def test_size(self):
        with self.assertRaisesRegex(PublicDataError, "SIZE"):
            decode_public_json(b' ' * (MAX_BYTES + 1))
    def test_nonfinite(self):
        for token in (b'NaN', b'Infinity', b'-Infinity', b'1e99999'):
            with self.subTest(token=token), self.assertRaises(PublicDataError):
                decode_public_json(token)
    def test_invalid_utf8(self):
        with self.assertRaisesRegex(PublicDataError, "INVALID_JSON"):
            decode_public_json(b'"\xff"')
    def test_brackets_in_strings_do_not_inflate_depth(self):
        self.assertEqual(decode_public_json(b'{"a":"[[[[[]]]]"}')["a"], "[[[[[]]]]")


class BookTests(unittest.TestCase):
    def parse(self, **changes):
        data = copy.deepcopy(BOOK)
        data.update(changes)
        return parse_orderbook(raw(data), "BTC_USDT", NOW)
    def test_valid_book_and_accessible_state(self):
        status = self.parse().accessible_status(now=NOW)
        self.assertEqual((status["bid"], status["ask"], status["spread"]), ("100", "101", "1"))
        self.assertEqual(status["execution_environment"], "SIMULATION_ONLY")
        self.assertFalse(status["private_account_connected"])
        self.assertFalse(status["real_orders_enabled"])
    def test_crossed_book(self):
        with self.assertRaisesRegex(PublicDataError, "CROSSED"):
            self.parse(asks=[["99", "1"]])
    def test_duplicate_or_unsorted_book(self):
        for bids in ([["99", "1"], ["100", "1"]], [["100", "1"], ["100", "2"]]):
            with self.subTest(bids=bids), self.assertRaisesRegex(PublicDataError, "UNSORTED"):
                self.parse(bids=bids)
    def test_float_bool_negative_zero_price(self):
        for value in (100.0, True, "-1", "0", "1e5", "NaN"):
            with self.subTest(value=value), self.assertRaises(PublicDataError):
                self.parse(bids=[[value, "1"]])
    def test_empty_book(self):
        with self.assertRaisesRegex(PublicDataError, "EMPTY"):
            self.parse(bids=[])
    def test_zero_quantity(self):
        with self.assertRaises(PublicDataError):
            self.parse(bids=[["100", "0"]])
    def test_wrong_market(self):
        with self.assertRaisesRegex(PublicDataError, "MARKET_ID"):
            self.parse(ticker_id="ETH_USDT")
    def test_path_injection(self):
        with self.assertRaisesRegex(PublicDataError, "INVALID_MARKET"):
            parse_orderbook(raw(BOOK), "BTC_USDT/../../order", NOW)
    def test_millisecond_timestamp_refused(self):
        with self.assertRaisesRegex(PublicDataError, "EPOCH_SECONDS"):
            self.parse(timestamp=int(NOW.timestamp()) * 1000)
    def test_future_timestamp(self):
        with self.assertRaisesRegex(PublicDataError, "FUTURE"):
            self.parse(timestamp=int(NOW.timestamp()) + 1)
    def test_stale_snapshot(self):
        with self.assertRaisesRegex(PublicDataError, "STALE"):
            self.parse(timestamp=int(NOW.timestamp()) - 11)
    def test_clock_reversal(self):
        with self.assertRaisesRegex(PublicDataError, "CLOCK"):
            self.parse().assert_fresh(NOW - timedelta(seconds=1))
    def test_independent_decimal_context(self):
        with localcontext() as context:
            context.prec = 1
            self.assertEqual(self.parse().accessible_status(now=NOW)["spread"], "1")
    def test_immutable_book_bytes(self):
        b = self.parse()
        self.assertIsInstance(b.bids, tuple)
        self.assertIsInstance(b.bids[0], tuple)
        self.assertTrue(b.digest.startswith("sha256:"))
    def test_canonical_bridge_requires_matching_evidence(self):
        book = self.parse()
        with self.assertRaisesRegex(PublicDataError, "BINDING"):
            book.to_raw_market_update({"sha256": "wrong"}, now=NOW)
    def test_canonical_bridge_shape_only_not_whole_runtime(self):
        # Explicit contract stub. Full RawMarketUpdate/ArtifactStore integration is a separate gate.
        book = self.parse()
        evidence = {"artifact_id": "00000000-0000-0000-0000-000000000001", "sha256": book.digest,
                    "source_uri": book.source_uri, "observed_at": NOW.isoformat().replace("+00:00", "Z")}
        module = types.ModuleType("mvp.autotrade_mvp.market_data")
        module.RawMarketUpdate = lambda **fields: fields
        with patch.dict(sys.modules, {"mvp.autotrade_mvp.market_data": module}):
            event = book.to_raw_market_update(evidence, now=NOW)
        self.assertEqual(event["kind"], "BOOK_SNAPSHOT")
        self.assertEqual(event["payload"]["bids"][0], ["100", "2"])
        self.assertEqual(event["raw_evidence_ref"], evidence)
    def test_evidence_wrong_time(self):
        b = self.parse()
        with self.assertRaisesRegex(PublicDataError, "TIME_MISMATCH"):
            b.to_raw_market_update({"sha256": b.digest, "source_uri": b.source_uri,
                                    "observed_at": "2020-01-01T00:00:00Z"}, now=NOW)


class MarketTests(unittest.TestCase):
    def test_spot_only_and_no_invented_price_tick(self):
        future = dict(MARKET, name="BTC_PERP", type="futures")
        rows = parse_spot_markets(raw([MARKET, future]))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["price_tick"])
        self.assertEqual(rows[0]["minimum_notional"], "5")
    def test_duplicate_markets(self):
        with self.assertRaisesRegex(PublicDataError, "DUPLICATE_MARKET"):
            parse_spot_markets(raw([MARKET, MARKET]))
    def test_unavailable_and_announced_delisting_excluded(self):
        for changed in (dict(MARKET, tradesEnabled=False), dict(MARKET, delistedAt=1800000000)):
            self.assertEqual(parse_spot_markets(raw([changed])), ())
    def test_asset_mismatch(self):
        with self.assertRaisesRegex(PublicDataError, "ASSET_ID"):
            parse_spot_markets(raw([dict(MARKET, money="USD")]))
    def test_invalid_precision(self):
        with self.assertRaisesRegex(PublicDataError, "PRECISION"):
            parse_spot_markets(raw([dict(MARKET, stockPrec="99")]))


class TransportTests(unittest.TestCase):
    def test_no_private_routes_or_credentials(self):
        client = WhiteBitPublicClient()
        for endpoint in ("/order/new", "https://other.invalid", "../private/balance"):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(PublicDataError, "NOT_ALLOWED"):
                client._read(endpoint)
        with self.assertRaises(TypeError):
            WhiteBitPublicClient(api_key="not-used")
    def test_redirects_blocked(self):
        with self.assertRaisesRegex(PublicDataError, "REDIRECT"):
            _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")
    def test_http_get_only(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        class Response(io.BytesIO):
            status = 200
        response = Response(raw([MARKET]))
        response.headers = headers
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append((request.get_method(), request.full_url, request.headers, timeout))
                return response
        client = WhiteBitPublicClient()
        client._opener = Opener()
        self.assertEqual(client.markets()[0]["market"], "BTC_USDT")
        self.assertEqual(requests[0][0:2], ("GET", BASE + "markets"))
        self.assertNotIn("Authorization", requests[0][2])
    def test_network_error_redacted(self):
        client = WhiteBitPublicClient()
        with patch.object(client._opener, "open", side_effect=URLError("sensitive transport detail")):
            with self.assertRaisesRegex(PublicDataError, "^PUBLIC_API_NETWORK_UNAVAILABLE$"):
                client.markets()
    def test_unknown_market_never_requests_book(self):
        client = WhiteBitPublicClient()
        with patch.object(client, "markets", return_value=()), patch.object(client, "_read") as read:
            with self.assertRaisesRegex(PublicDataError, "NOT_AVAILABLE"):
                client.orderbook("BTC_USDT")
            read.assert_not_called()


class CliTests(unittest.TestCase):
    def test_ukrainian_probe_uses_public_client(self):
        path = Path(__file__).resolve().parents[2] / "tools" / "probe_whitebit_public.py"
        namespace = runpy.run_path(str(path), run_name="probe_test")
        now = datetime.now(timezone.utc)
        book = parse_orderbook(raw(dict(BOOK, timestamp=int(now.timestamp()))), "BTC_USDT", now)
        out = io.StringIO()
        with patch.object(WhiteBitPublicClient, "orderbook", return_value=book), patch("sys.stdout", out):
            self.assertEqual(namespace["main"](["--format", "json"]), 0)
        self.assertFalse(json.loads(out.getvalue())["real_orders_enabled"])


if __name__ == "__main__":
    unittest.main()
