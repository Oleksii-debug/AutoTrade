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
    BASE, KLINE_BASE, MAX_BYTES, MAX_CANDLES, WS, PublicDataError,
    WhiteBitPublicClient, _NoRedirect, _kline_query, _public_url, decode_public_json,
    parse_klines, parse_market_activity, parse_orderbook, parse_spot_markets,
    plan_kline_windows,
)

NOW = datetime(2026, 10, 17, 20, 0, 0, tzinfo=timezone.utc)
BOOK = {"ticker_id": "BTC_USDT", "timestamp": int(NOW.timestamp()),
        "bids": [["100", "2"], ["99", "3"]], "asks": [["101", "4"], ["102", "5"]]}
MARKET = {"name": "BTC_USDT", "stock": "BTC", "money": "USDT", "stockPrec": "8",
          "moneyPrec": "2", "minAmount": "0.00001", "minTotal": "5",
          "type": "spot", "tradesEnabled": True, "delistedAt": None}
TICKER = {"base_id": 1, "quote_id": 825, "last_price": "9164.09",
          "quote_volume": "43341942.90416876", "base_volume": "4723.286463",
          "isFrozen": False, "change": "-0.57"}


def candle(hour, opening="100", closing="101", high="102", low="99",
           base_volume="2", quote_volume="201"):
    return [int(NOW.replace(hour=hour).timestamp()), opening, closing, high, low,
            base_volume, quote_volume]


def kline_raw(rows, *, success=True, message=None):
    return raw({"success": success, "message": message, "result": rows if success else None})

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


class ActivityTests(unittest.TestCase):
    def test_valid_activity_is_canonical_and_provenanced(self):
        payload = raw({"ETH_USDT": dict(TICKER, base_id=1027), "BTC_USDT": TICKER})
        rows = parse_market_activity(payload, NOW)
        self.assertEqual([row.market for row in rows], ["BTC_USDT", "ETH_USDT"])
        self.assertEqual(rows[0].change_percent, "-0.57")
        self.assertEqual(rows[0].last_price, "9164.09")
        self.assertEqual(rows[0].provenance["source_uri"], BASE + "ticker")
        self.assertTrue(rows[0].provenance["sha256"].startswith("sha256:"))

    def test_duplicate_market_key_rejected(self):
        body = b'{"BTC_USDT":' + raw(TICKER) + b',"BTC_USDT":' + raw(TICKER) + b'}'
        with self.assertRaisesRegex(PublicDataError, "DUPLICATE_JSON_KEY"):
            parse_market_activity(body, NOW)

    def test_invalid_market_or_record_rejected(self):
        for payload in ({"btc_usdt": TICKER}, {"BTC_USDT": []}):
            with self.subTest(payload=payload), self.assertRaises(PublicDataError):
                parse_market_activity(raw(payload), NOW)

    def test_nonfinite_and_invalid_ticker_values_rejected(self):
        for field, value in (("last_price", "NaN"), ("change", "Infinity"),
                             ("base_volume", "-1"), ("isFrozen", 0),
                             ("base_id", True)):
            with self.subTest(field=field, value=value), self.assertRaises(PublicDataError):
                parse_market_activity(raw({"BTC_USDT": dict(TICKER, **{field: value})}), NOW)

    def test_error_body_rejected_even_with_http_200_shape(self):
        body = {"success": False, "message": {"market": ["bad"]}, "result": None}
        with self.assertRaisesRegex(PublicDataError, "ERROR_BODY"):
            parse_market_activity(raw(body), NOW)


class KlineTests(unittest.TestCase):
    uri = KLINE_BASE + "kline?market=BTC_USDT&interval=1h&limit=1440"

    def parse(self, rows, **kwargs):
        limit = kwargs.get("requested_limit", MAX_CANDLES)
        query = _kline_query("BTC_USDT", "1h", limit,
                             start=kwargs.get("start"), end=kwargs.get("end"))
        return parse_klines(kline_raw(rows), "BTC_USDT", "1h", NOW,
                            source_uri=_public_url("kline", query), **kwargs)

    def test_valid_klines_are_exact_and_provenanced(self):
        batch = self.parse([candle(18), candle(19)])
        self.assertEqual(len(batch.candles), 2)
        self.assertTrue(all(item.complete for item in batch.candles))
        self.assertEqual(batch.candles[0].open, "100")
        self.assertEqual(batch.provenance["source_uri"], self.uri)
        self.assertTrue(batch.digest.startswith("sha256:"))

    def test_duplicate_or_out_of_order_rejected(self):
        for rows in ([candle(18), candle(18)], [candle(19), candle(18)]):
            with self.subTest(rows=rows), self.assertRaisesRegex(PublicDataError, "OUT_OF_ORDER"):
                self.parse(rows)

    def test_oversized_response_rejected(self):
        with self.assertRaisesRegex(PublicDataError, "OVERSIZED"):
            self.parse([candle(18), candle(19)], requested_limit=1)
        oversized = [candle(18)] * (MAX_CANDLES + 1)
        with self.assertRaisesRegex(PublicDataError, "OVERSIZED"):
            self.parse(oversized)

    def test_invalid_and_nonfinite_values_rejected(self):
        for index, value in ((1, "NaN"), (2, "1e10"), (5, "-1"), (0, True)):
            row = candle(18)
            row[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(PublicDataError):
                self.parse([row])

    def test_invalid_price_range_rejected(self):
        with self.assertRaisesRegex(PublicDataError, "PRICE_RANGE"):
            self.parse([candle(18, high="100")])

    def test_http_200_error_body_rejected(self):
        body = raw({"success": False, "message": {"end": ["bad"]}, "result": None})
        with self.assertRaisesRegex(PublicDataError, "ERROR_BODY"):
            parse_klines(body, "BTC_USDT", "1h", NOW, source_uri=self.uri)

    def test_source_uri_and_future_range_are_bound(self):
        with self.assertRaisesRegex(PublicDataError, "SOURCE_URI"):
            parse_klines(kline_raw([candle(18)]), "BTC_USDT", "1h", NOW,
                         source_uri=self.uri + "&extra=1")
        future = int(NOW.timestamp()) + 1
        query = _kline_query("BTC_USDT", "1h", MAX_CANDLES, end=future)
        with self.assertRaisesRegex(PublicDataError, "FUTURE_KLINE_RANGE"):
            parse_klines(kline_raw([]), "BTC_USDT", "1h", NOW,
                         source_uri=_public_url("kline", query), end=future)

    def test_incomplete_candle_allowed_for_display_but_rejected_for_training(self):
        self.assertFalse(self.parse([candle(20)]).candles[0].complete)
        with self.assertRaisesRegex(PublicDataError, "INCOMPLETE_TRAINING_CANDLE"):
            self.parse([candle(20)], training=True)

    def test_response_must_stay_inside_requested_range(self):
        with self.assertRaisesRegex(PublicDataError, "OUTSIDE_REQUEST_RANGE"):
            self.parse([candle(18)], start=int(NOW.replace(hour=19).timestamp()))

    def test_deterministic_non_overlapping_pagination(self):
        start = 1_700_000_000
        end = start + 2000 * 3600 - 1
        pages = plan_kline_windows(start=start, end=end, interval="1h")
        self.assertEqual([(page.limit, page.start, page.end) for page in pages], [
            (1440, start, start + 1440 * 3600 - 1),
            (560, start + 1440 * 3600, end),
        ])
        self.assertEqual(pages[0].end + 1, pages[1].start)

    def test_invalid_pagination_inputs_rejected(self):
        for kwargs in (
            {"start": 2, "end": 1, "interval": "1h"},
            {"start": 1, "end": 2, "interval": "7m"},
            {"start": 1, "end": 2, "interval": "1M"},
            {"start": 1, "end": 2, "interval": "1h", "page_limit": 1441},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PublicDataError):
                plan_kline_windows(**kwargs)


class TransportTests(unittest.TestCase):
    def test_documented_websocket_endpoint(self):
        self.assertEqual(WS, "wss://wss.whitebit.com/ws")

    def test_no_private_routes_or_credentials(self):
        client = WhiteBitPublicClient()
        for endpoint in ("/order/new", "https://other.invalid", "../private/balance"):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(PublicDataError, "NOT_ALLOWED"):
                client._read(endpoint)
        with self.assertRaises(TypeError):
            WhiteBitPublicClient(api_key="not-used")
    def test_query_allowlist_canonical_order_and_encoding(self):
        query = (("market", "BTC_USDT&unexpected=1"), ("interval", "1h"), ("limit", "1"))
        url = _public_url("kline", query)
        self.assertIn("market=BTC_USDT%26unexpected%3D1", url)
        self.assertNotIn("&unexpected=", url)
        for bad in (
            (("unknown", "1"),),
            (("limit", "1"), ("market", "BTC_USDT")),
            (("market", "BTC_USDT"), ("market", "ETH_USDT")),
        ):
            with self.subTest(bad=bad), self.assertRaises(PublicDataError):
                _public_url("kline", bad)
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
    def test_activity_and_kline_use_documented_public_urls(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        responses = [raw({"BTC_USDT": TICKER}), kline_raw([candle(18)])]
        requests = []
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def open(self, request, timeout):
                response = Response(responses.pop(0))
                response.headers = headers
                requests.append((request.get_method(), request.full_url))
                return response
        client = WhiteBitPublicClient()
        client._opener = Opener()
        self.assertEqual(client.market_activity("BTC_USDT").market, "BTC_USDT")
        end = int(NOW.timestamp())
        self.assertEqual(len(client.klines("BTC_USDT", interval="1h", end=end,
                                          limit=1, training=True).candles), 1)
        self.assertEqual(requests[0], ("GET", BASE + "ticker"))
        self.assertEqual(requests[1], (
            "GET", KLINE_BASE + "kline?market=BTC_USDT&end=1792267200&interval=1h&limit=1"))
    def test_training_requires_explicit_end_without_network(self):
        client = WhiteBitPublicClient()
        with patch.object(client, "_read") as read:
            with self.assertRaisesRegex(PublicDataError, "EXPLICIT_END"):
                client.klines("BTC_USDT", training=True)
            read.assert_not_called()
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
