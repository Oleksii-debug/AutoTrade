"""WhiteBIT production-public data; NO account, credentials or order sender.

Public market data and execution environments are deliberately separate.
The existing AutoTrade market normalizer, evidence store and simulated provider
remain authoritative downstream. Network permission belongs to the collector,
not the model worker. Sources: docs.whitebit.com/api-reference/market-data/.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import re
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

BASE = "https://whitebit.com/api/v4/public/"
KLINE_BASE = "https://whitebit.com/api/v1/public/"
WS = "wss://wss.whitebit.com/ws"
ADAPTER_VERSION = "whitebit-public-rest-v1"
ACTIVITY_KLINE_ADAPTER_VERSION = "whitebit-public-activity-kline-v1"
MAX_BYTES = 1_048_576
MAX_DEPTH = 16
MAX_CANDLES = 1440
SYMBOL = re.compile(r"[A-Z0-9]{1,20}_[A-Z0-9]{1,20}\Z")
NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,35})(?:\.[0-9]{1,24})?\Z")
SIGNED_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,35})(?:\.[0-9]{1,24})?\Z")
INTERVAL_SECONDS = {
    "1s": 1, "10s": 10, "30s": 30,
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "8h": 28800, "12h": 43200, "1d": 86400, "3d": 259200,
    "1w": 604800,
}
INTERVALS = frozenset((*INTERVAL_SECONDS, "1M"))


class PublicDataError(ValueError):
    """A bounded, non-sensitive error safe for an accessible status surface."""


def _symbol(value: str) -> str:
    if type(value) is not str or SYMBOL.fullmatch(value) is None:
        raise PublicDataError("INVALID_MARKET")
    return value


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise PublicDataError("UTC_TIMESTAMP_REQUIRED")
    return value.astimezone(timezone.utc)


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    # Provider decimals are strings. Reject float/bool/exponents and oversized input.
    if type(value) is not str or NUMBER.fullmatch(value) is None:
        raise PublicDataError("INVALID_DECIMAL")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PublicDataError("INVALID_DECIMAL") from None
    if not result.is_finite() or (positive and result <= 0):
        raise PublicDataError("NON_POSITIVE_DECIMAL")
    return result


def _signed_decimal(value: Any) -> Decimal:
    if type(value) is not str or SIGNED_NUMBER.fullmatch(value) is None:
        raise PublicDataError("INVALID_DECIMAL")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PublicDataError("INVALID_DECIMAL") from None
    if not result.is_finite():
        raise PublicDataError("INVALID_DECIMAL")
    return result


def _epoch_seconds(value: Any, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1_000_000_000
    if type(value) is not int or not minimum <= value <= 9_999_999_999:
        raise PublicDataError("EXPECTED_EPOCH_SECONDS")
    return value


def _interval(value: Any) -> str:
    if type(value) is not str or value not in INTERVALS:
        raise PublicDataError("INVALID_KLINE_INTERVAL")
    return value


def decode_public_json(raw: bytes) -> Any:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise PublicDataError("RESPONSE_SIZE_LIMIT")
    # Pre-scan before json.loads to bound nesting, including hostile JSON.
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                raise PublicDataError("JSON_DEPTH_LIMIT")
        elif char in (93, 125):
            depth -= 1
            if depth < 0:
                raise PublicDataError("INVALID_JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PublicDataError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def integer(token):
        if len(token) > 16:
            raise PublicDataError("INTEGER_SIZE_LIMIT")
        return int(token)

    def number(token):
        if len(token) > 64:
            raise PublicDataError("DECIMAL_SIZE_LIMIT")
        result = Decimal(token)
        if not result.is_finite() or abs(result.adjusted()) > 48:
            raise PublicDataError("INVALID_JSON_NUMBER")
        return result

    def constant(_):
        raise PublicDataError("NON_FINITE_JSON")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_int=integer, parse_float=number, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, InvalidOperation, RecursionError):
        raise PublicDataError("INVALID_JSON") from None


@dataclass(frozen=True)
class PublicTicker:
    market: str
    received_at: datetime
    last_price: str
    quote_volume: str
    base_volume: str
    change_percent: str
    is_frozen: bool
    base_id: int
    quote_id: int
    raw: bytes = field(repr=False)
    source_uri: str

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.raw).hexdigest()

    @property
    def provenance(self) -> dict[str, str]:
        return {
            "provider": "WHITEBIT",
            "source_uri": self.source_uri,
            "received_at": self.received_at.isoformat().replace("+00:00", "Z"),
            "sha256": self.digest,
            "adapter_version": ACTIVITY_KLINE_ADAPTER_VERSION,
        }


@dataclass(frozen=True)
class PublicCandle:
    open_time: datetime
    open: str
    close: str
    high: str
    low: str
    base_volume: str
    quote_volume: str
    complete: bool


@dataclass(frozen=True)
class PublicKlines:
    market: str
    interval: str
    received_at: datetime
    candles: tuple[PublicCandle, ...]
    raw: bytes = field(repr=False)
    source_uri: str

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.raw).hexdigest()

    @property
    def provenance(self) -> dict[str, str]:
        return {
            "provider": "WHITEBIT",
            "source_uri": self.source_uri,
            "received_at": self.received_at.isoformat().replace("+00:00", "Z"),
            "sha256": self.digest,
            "adapter_version": ACTIVITY_KLINE_ADAPTER_VERSION,
        }


@dataclass(frozen=True)
class KlineWindow:
    start: int
    end: int
    limit: int


@dataclass(frozen=True)
class PublicBook:
    market: str
    source_time: datetime
    received_at: datetime
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    raw: bytes = field(repr=False)
    source_uri: str

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.raw).hexdigest()

    def assert_fresh(self, now: datetime, maximum_age_seconds: int = 10) -> None:
        now = _utc(now)
        if type(maximum_age_seconds) is not int or not 1 <= maximum_age_seconds <= 300:
            raise PublicDataError("INVALID_FRESHNESS_LIMIT")
        if now < self.received_at or now < self.source_time:
            raise PublicDataError("CLOCK_MOVED_BACKWARD")
        if (now - self.source_time).total_seconds() > maximum_age_seconds:
            raise PublicDataError("STALE_MARKET_DATA")

    def accessible_status(self, *, now: datetime) -> dict[str, Any]:
        self.assert_fresh(now)
        with localcontext() as context:
            context.prec = 80
            spread = Decimal(self.asks[0][0]) - Decimal(self.bids[0][0])
        return {
            "provider": "WhiteBIT", "market": self.market,
            "market_environment": "PRODUCTION_PUBLIC",
            "execution_environment": "SIMULATION_ONLY",
            "bid": self.bids[0][0], "ask": self.asks[0][0],
            "bid_amount": self.bids[0][1], "ask_amount": self.asks[0][1],
            "spread": format(spread, "f"),
            "source_time": self.source_time.isoformat(),
            "received_at": self.received_at.isoformat(), "source_digest": self.digest,
            "private_account_connected": False, "real_orders_enabled": False,
            "liquidity_limitations": "PUBLIC_BOOK_EXCLUDES_RPI; NO_FILL_GUARANTEE",
        }

    def to_raw_market_update(self, evidence_ref: dict[str, Any], *, now: datetime):
        """Use the EXISTING normalizer contract after actual raw-artifact persistence.

        A matching hash is not a substitute for storage: the calling collector
        must first commit raw bytes through AutoTrade's existing ArtifactStore.
        No synthetic receipt is created here.
        """
        self.assert_fresh(now)
        if (type(evidence_ref) is not dict or evidence_ref.get("sha256") != self.digest
                or evidence_ref.get("source_uri") != self.source_uri):
            raise PublicDataError("RAW_EVIDENCE_BINDING_MISMATCH")
        observed = evidence_ref.get("observed_at")
        expected = self.received_at.isoformat().replace("+00:00", "Z")
        if observed != expected:
            raise PublicDataError("RAW_EVIDENCE_TIME_MISMATCH")
        from mvp.autotrade_mvp.market_data import RawMarketUpdate
        return RawMarketUpdate(
            provider_id="WHITEBIT", venue_id="WHITEBIT_SPOT",
            provider_symbol=self.market, adapter_version=ADAPTER_VERSION,
            kind="BOOK_SNAPSHOT", source_event_at=self.source_time,
            available_at=self.received_at, ingested_at=_utc(now),
            availability_basis="OBSERVED_PUBLIC_REST_RESPONSE", revision=0,
            payload={"bids": [list(x) for x in self.bids], "asks": [list(x) for x in self.asks]},
            raw_evidence_ref=evidence_ref,
        )


def parse_orderbook(raw: bytes, market: str, received_at: datetime) -> PublicBook:
    market, received_at = _symbol(market), _utc(received_at)
    data = decode_public_json(raw)
    if type(data) is not dict or data.get("ticker_id") != market:
        raise PublicDataError("MARKET_ID_MISMATCH")
    seconds = data.get("timestamp")
    if type(seconds) is not int or not 1_000_000_000 <= seconds <= 9_999_999_999:
        raise PublicDataError("EXPECTED_EPOCH_SECONDS")
    try:
        source = datetime.fromtimestamp(seconds, timezone.utc)
    except (OSError, ValueError, OverflowError):
        raise PublicDataError("INVALID_SOURCE_TIME") from None
    if source > received_at:
        raise PublicDataError("FUTURE_MARKET_TIMESTAMP")

    def levels(name: str, descending: bool):
        rows = data.get(name)
        if type(rows) is not list or not 1 <= len(rows) <= 100:
            raise PublicDataError("EMPTY_OR_OVERSIZED_ORDERBOOK")
        result, previous = [], None
        for row in rows:
            if type(row) is not list or len(row) != 2:
                raise PublicDataError("INVALID_PRICE_LEVEL")
            price, amount = _decimal(row[0], positive=True), _decimal(row[1], positive=True)
            if previous is not None and ((descending and price >= previous) or
                                         (not descending and price <= previous)):
                raise PublicDataError("UNSORTED_OR_DUPLICATE_PRICE_LEVEL")
            previous = price
            result.append((format(price, "f"), format(amount, "f")))
        return tuple(result)

    bids, asks = levels("bids", True), levels("asks", False)
    if Decimal(bids[0][0]) >= Decimal(asks[0][0]):
        raise PublicDataError("CROSSED_OR_LOCKED_ORDERBOOK")
    result = PublicBook(market, source, received_at, bids, asks, raw,
                        BASE + "orderbook/" + market + "?limit=100&level=0")
    result.assert_fresh(received_at)
    return result


def parse_spot_markets(raw: bytes) -> tuple[dict[str, Any], ...]:
    data = decode_public_json(raw)
    if type(data) is not list or not 1 <= len(data) <= 10000:
        raise PublicDataError("INVALID_MARKET_LIST")
    result, seen = [], set()
    for row in data:
        if type(row) is not dict:
            raise PublicDataError("INVALID_MARKET_RECORD")
        # This first integration is SPOT only. Derivatives need lifecycle qualification.
        if row.get("type") != "spot":
            continue
        market = _symbol(row.get("name"))
        if market in seen:
            raise PublicDataError("DUPLICATE_MARKET")
        seen.add(market)
        base, quote = market.split("_")
        if row.get("stock") != base or row.get("money") != quote:
            raise PublicDataError("ASSET_ID_MISMATCH")
        if type(row.get("tradesEnabled")) is not bool:
            raise PublicDataError("UNKNOWN_TRADING_STATUS")
        if not row["tradesEnabled"] or row.get("delistedAt") is not None:
            continue
        quantity_precision, quote_precision = row.get("stockPrec"), row.get("moneyPrec")
        if any(type(x) is not str or not re.fullmatch(r"[0-9]{1,2}", x) or int(x) > 24
               for x in (quantity_precision, quote_precision)):
            raise PublicDataError("INVALID_PRECISION")
        result.append({
            "market": market, "base_asset": base, "quote_asset": quote,
            "minimum_quantity": format(_decimal(row.get("minAmount"), positive=True), "f"),
            "minimum_notional": format(_decimal(row.get("minTotal"), positive=True), "f"),
            "quantity_precision": int(quantity_precision), "quote_precision": int(quote_precision),
            # Quote precision is NOT assumed to be a independently qualified price tick.
            "price_tick": None, "account_permission": "UNKNOWN",
            "market_environment": "PRODUCTION_PUBLIC", "execution_environment": "SIMULATION_ONLY",
        })
    return tuple(result)


def parse_market_activity(raw: bytes, received_at: datetime) -> tuple[PublicTicker, ...]:
    received_at = _utc(received_at)
    data = decode_public_json(raw)
    if type(data) is not dict or not 1 <= len(data) <= 10000:
        raise PublicDataError("INVALID_TICKER_LIST")
    if data.get("success") is False:
        raise PublicDataError("PUBLIC_API_ERROR_BODY")
    result = []
    for market, row in data.items():
        market = _symbol(market)
        if type(row) is not dict:
            raise PublicDataError("INVALID_TICKER_RECORD")
        base_id, quote_id = row.get("base_id"), row.get("quote_id")
        if (type(base_id) is not int or type(quote_id) is not int
                or not 0 <= base_id <= 9_999_999_999
                or not 0 <= quote_id <= 9_999_999_999):
            raise PublicDataError("INVALID_ASSET_NUMERIC_ID")
        frozen = row.get("isFrozen")
        if type(frozen) is not bool:
            raise PublicDataError("INVALID_TICKER_STATUS")
        result.append(PublicTicker(
            market=market,
            received_at=received_at,
            last_price=format(_decimal(row.get("last_price"), positive=True), "f"),
            quote_volume=format(_decimal(row.get("quote_volume")), "f"),
            base_volume=format(_decimal(row.get("base_volume")), "f"),
            change_percent=format(_signed_decimal(row.get("change")), "f"),
            is_frozen=frozen,
            base_id=base_id,
            quote_id=quote_id,
            raw=raw,
            source_uri=BASE + "ticker",
        ))
    return tuple(sorted(result, key=lambda item: item.market))


def _candle_close_time(open_time: datetime, interval: str) -> datetime:
    interval = _interval(interval)
    if interval != "1M":
        from datetime import timedelta
        return open_time + timedelta(seconds=INTERVAL_SECONDS[interval])
    if (open_time.day, open_time.hour, open_time.minute,
            open_time.second, open_time.microsecond) != (1, 0, 0, 0, 0):
        raise PublicDataError("INVALID_MONTHLY_CANDLE_ALIGNMENT")
    year, month = open_time.year, open_time.month + 1
    if month == 13:
        year, month = year + 1, 1
    try:
        return datetime(year, month, 1, tzinfo=timezone.utc)
    except ValueError:
        raise PublicDataError("INVALID_SOURCE_TIME") from None


def parse_klines(
    raw: bytes,
    market: str,
    interval: str,
    received_at: datetime,
    *,
    source_uri: str,
    requested_limit: int = MAX_CANDLES,
    start: int | None = None,
    end: int | None = None,
    training: bool = False,
) -> PublicKlines:
    market, interval, received_at = _symbol(market), _interval(interval), _utc(received_at)
    if type(requested_limit) is not int or not 1 <= requested_limit <= MAX_CANDLES:
        raise PublicDataError("INVALID_KLINE_LIMIT")
    if type(training) is not bool:
        raise PublicDataError("INVALID_TRAINING_FLAG")
    if start is not None:
        start = _epoch_seconds(start, allow_zero=True)
    if end is not None:
        end = _epoch_seconds(end, allow_zero=True)
    if start is not None and end is not None and end <= start:
        raise PublicDataError("INVALID_KLINE_RANGE")
    if end is not None and end > int(received_at.timestamp()):
        raise PublicDataError("FUTURE_KLINE_RANGE")
    expected_uri = _public_url(
        "kline", _kline_query(market, interval, requested_limit, start=start, end=end))
    if source_uri != expected_uri:
        raise PublicDataError("INVALID_SOURCE_URI")

    data = decode_public_json(raw)
    if type(data) is not dict:
        raise PublicDataError("INVALID_KLINE_RESPONSE")
    if data.get("success") is False:
        raise PublicDataError("PUBLIC_API_ERROR_BODY")
    if data.get("success") is not True or data.get("message") not in (None, ""):
        raise PublicDataError("INVALID_KLINE_RESPONSE")
    rows = data.get("result")
    if type(rows) is not list:
        raise PublicDataError("INVALID_KLINE_RESPONSE")
    if len(rows) > requested_limit or len(rows) > MAX_CANDLES:
        raise PublicDataError("OVERSIZED_KLINE_RESPONSE")

    candles, previous = [], None
    for row in rows:
        if type(row) is not list or len(row) != 7:
            raise PublicDataError("INVALID_KLINE_RECORD")
        seconds = _epoch_seconds(row[0])
        if previous is not None and seconds <= previous:
            raise PublicDataError("OUT_OF_ORDER_OR_DUPLICATE_KLINE")
        if start is not None and seconds < start:
            raise PublicDataError("KLINE_OUTSIDE_REQUEST_RANGE")
        if end is not None and seconds > end:
            raise PublicDataError("KLINE_OUTSIDE_REQUEST_RANGE")
        previous = seconds
        try:
            open_time = datetime.fromtimestamp(seconds, timezone.utc)
        except (OSError, ValueError, OverflowError):
            raise PublicDataError("INVALID_SOURCE_TIME") from None
        if open_time > received_at:
            raise PublicDataError("FUTURE_MARKET_TIMESTAMP")
        opening = _decimal(row[1], positive=True)
        closing = _decimal(row[2], positive=True)
        high = _decimal(row[3], positive=True)
        low = _decimal(row[4], positive=True)
        base_volume = _decimal(row[5])
        quote_volume = _decimal(row[6])
        if high < max(opening, closing) or low > min(opening, closing) or low > high:
            raise PublicDataError("INVALID_KLINE_PRICE_RANGE")
        complete = _candle_close_time(open_time, interval) <= received_at
        if training and not complete:
            raise PublicDataError("INCOMPLETE_TRAINING_CANDLE")
        candles.append(PublicCandle(
            open_time=open_time,
            open=format(opening, "f"), close=format(closing, "f"),
            high=format(high, "f"), low=format(low, "f"),
            base_volume=format(base_volume, "f"), quote_volume=format(quote_volume, "f"),
            complete=complete,
        ))
    return PublicKlines(market, interval, received_at, tuple(candles), raw, source_uri)


def plan_kline_windows(
    *, start: int, end: int, interval: str, page_limit: int = MAX_CANDLES,
) -> tuple[KlineWindow, ...]:
    """Return deterministic, non-overlapping inclusive query windows.

    The calendar-month interval deliberately has no fixed-width pagination plan;
    callers may still request one bounded 1M page directly.
    """
    start = _epoch_seconds(start, allow_zero=True)
    end = _epoch_seconds(end, allow_zero=True)
    interval = _interval(interval)
    if end <= start:
        raise PublicDataError("INVALID_KLINE_RANGE")
    if type(page_limit) is not int or not 1 <= page_limit <= MAX_CANDLES:
        raise PublicDataError("INVALID_KLINE_LIMIT")
    if interval == "1M":
        raise PublicDataError("PAGINATION_INTERVAL_UNSUPPORTED")
    seconds = INTERVAL_SECONDS[interval]
    result, cursor = [], start
    while cursor <= end:
        page_end = min(end, cursor + seconds * page_limit - 1)
        count = min(page_limit, max(1, (page_end - cursor + seconds) // seconds))
        result.append(KlineWindow(cursor, page_end, count))
        cursor = page_end + 1
    return tuple(result)


def _public_url(endpoint: str, query: tuple[tuple[str, str], ...] = ()) -> str:
    if type(endpoint) is not str or type(query) is not tuple:
        raise PublicDataError("PUBLIC_ENDPOINT_NOT_ALLOWED")
    if endpoint in ("markets", "ticker"):
        allowed, root = (), BASE
    elif re.fullmatch(r"orderbook/[A-Z0-9]{1,20}_[A-Z0-9]{1,20}", endpoint):
        allowed, root = ("limit", "level"), BASE
    elif endpoint == "kline":
        allowed, root = ("market", "start", "end", "interval", "limit"), KLINE_BASE
    else:
        raise PublicDataError("PUBLIC_ENDPOINT_NOT_ALLOWED")
    if any(type(item) is not tuple or len(item) != 2
           or type(item[0]) is not str or type(item[1]) is not str for item in query):
        raise PublicDataError("PUBLIC_QUERY_NOT_ALLOWED")
    if any(not 1 <= len(value) <= 64 or any(ord(char) < 32 or ord(char) > 126 for char in value)
           for _, value in query):
        raise PublicDataError("PUBLIC_QUERY_NOT_ALLOWED")
    keys = tuple(item[0] for item in query)
    if len(set(keys)) != len(keys) or any(key not in allowed for key in keys):
        raise PublicDataError("PUBLIC_QUERY_NOT_ALLOWED")
    if keys != tuple(key for key in allowed if key in keys):
        raise PublicDataError("NON_CANONICAL_PUBLIC_QUERY")
    if endpoint in ("markets", "ticker") and query:
        raise PublicDataError("PUBLIC_QUERY_NOT_ALLOWED")
    if endpoint.startswith("orderbook/") and query != (("limit", "100"), ("level", "0")):
        raise PublicDataError("PUBLIC_QUERY_NOT_ALLOWED")
    suffix = "?" + urlencode(query) if query else ""
    return root + endpoint + suffix


def _kline_query(
    market: str, interval: str, limit: int,
    *, start: int | None = None, end: int | None = None,
) -> tuple[tuple[str, str], ...]:
    market, interval = _symbol(market), _interval(interval)
    if type(limit) is not int or not 1 <= limit <= MAX_CANDLES:
        raise PublicDataError("INVALID_KLINE_LIMIT")
    if start is not None:
        start = _epoch_seconds(start, allow_zero=True)
    if end is not None:
        end = _epoch_seconds(end, allow_zero=True)
    if start is not None and end is not None and end <= start:
        raise PublicDataError("INVALID_KLINE_RANGE")
    values = {"market": market, "start": start, "end": end,
              "interval": interval, "limit": limit}
    return tuple((key, str(values[key])) for key in ("market", "start", "end", "interval", "limit")
                 if values[key] is not None)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublicDataError("PUBLIC_API_REDIRECT_REJECTED")


class WhiteBitPublicClient:
    """Bounded GET-only collector. No base URL override, credentials or retries."""
    def __init__(self):
        self._opener = build_opener(ProxyHandler({}), _NoRedirect(),
                                    HTTPSHandler(context=ssl.create_default_context()))
        self._lock = threading.Lock()
        self._next_request = 0.0

    def _read(
        self, endpoint: str, query: tuple[tuple[str, str], ...] = (),
    ) -> tuple[bytes, datetime]:
        url = _public_url(endpoint, query)
        with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + 1.0
            request = Request(url, method="GET",
                              headers={"Accept": "application/json", "User-Agent": "AutoTrade-PublicReader/1"})
            try:
                with self._opener.open(request, timeout=10) as response:
                    if response.status != 200:
                        raise PublicDataError("PUBLIC_API_NON_SUCCESS")
                    if response.headers.get_content_type() != "application/json":
                        raise PublicDataError("PUBLIC_API_CONTENT_TYPE")
                    raw = response.read(MAX_BYTES + 1)
            except HTTPError as error:
                code = error.code
                error.close()
                raise PublicDataError("PUBLIC_API_RATE_LIMIT" if code == 429 else "PUBLIC_API_HTTP_ERROR") from None
            except (URLError, OSError, TimeoutError):
                raise PublicDataError("PUBLIC_API_NETWORK_UNAVAILABLE") from None
            if not 0 < len(raw) <= MAX_BYTES:
                raise PublicDataError("RESPONSE_SIZE_LIMIT")
            return raw, datetime.now(timezone.utc)

    def markets(self) -> tuple[dict[str, Any], ...]:
        raw, _ = self._read("markets")
        return parse_spot_markets(raw)

    def market_activity(self, market: str) -> PublicTicker:
        market = _symbol(market)
        raw, received = self._read("ticker")
        matches = [item for item in parse_market_activity(raw, received)
                   if item.market == market]
        if len(matches) != 1:
            raise PublicDataError("MARKET_ACTIVITY_NOT_FOUND")
        return matches[0]

    def klines(
        self,
        market: str,
        *,
        interval: str = "1h",
        start: int | None = None,
        end: int | None = None,
        limit: int = MAX_CANDLES,
        training: bool = False,
    ) -> PublicKlines:
        if type(training) is not bool:
            raise PublicDataError("INVALID_TRAINING_FLAG")
        if training and end is None:
            raise PublicDataError("TRAINING_REQUIRES_EXPLICIT_END")
        query = _kline_query(market, interval, limit, start=start, end=end)
        source_uri = _public_url("kline", query)
        raw, received = self._read("kline", query)
        return parse_klines(
            raw, market, interval, received, source_uri=source_uri,
            requested_limit=limit, start=start, end=end, training=training,
        )

    def orderbook(self, market: str) -> PublicBook:
        market = _symbol(market)
        # A caller cannot route _PERP or unknown instruments into the spot connector.
        available = {item["market"] for item in self.markets()}
        if market not in available:
            raise PublicDataError("SPOT_MARKET_NOT_AVAILABLE")
        query = (("limit", "100"), ("level", "0"))
        raw, received = self._read("orderbook/" + market, query)
        return parse_orderbook(raw, market, received)
